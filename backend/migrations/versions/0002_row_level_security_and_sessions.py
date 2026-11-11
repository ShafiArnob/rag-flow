"""Row-level security on the baseline tables, and the sessions table. Part of P01_S03.

Every tenant table gets ENABLE and FORCE ROW LEVEL SECURITY and exactly one ``<table>_tenant``
policy comparing its workspace key with ``app_current_workspace_id()``. FORCE makes the policy
apply to the owner role as well, so nothing but a superuser or BYPASSRLS role sees across
workspaces. ``users`` has no workspace key and stays without row-level security until P01_S04 adds
user-scoped policies.

The application role is granted privileges table by table, and only after the table is protected,
so it never holds a privilege on a table that row-level security does not cover.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from app.database_roles import DATABASE_APP_ROLE

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# The column each tenant policy compares with the workspace setting. A workspace is its own tenant.
TENANT_KEY_COLUMNS = {
    "workspaces": "id",
    "workspace_members": "workspace_id",
    "sessions": "workspace_id",
}

APP_PRIVILEGES = {
    "workspaces": "SELECT, INSERT",
    "users": "SELECT, INSERT, UPDATE",
    "workspace_members": "SELECT, INSERT",
    "sessions": "SELECT, INSERT, UPDATE, DELETE",
}


def upgrade() -> None:
    # NULLIF because a pooled connection that has run a tenant transaction carries '' instead of
    # NULL, and ''::uuid raises. Either way an unset workspace matches no row.
    op.execute(
        """
        CREATE FUNCTION app_current_workspace_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid $$
        """
    )
    op.execute(
        "ALTER TABLE workspace_members"
        " ADD CONSTRAINT workspace_members_role_check CHECK (role IN ('owner'))"
    )
    # The session's key to its membership is composite, so a session can only belong to a member of
    # its own workspace, and removing the member ends the session. token_hash is the SHA-256 of the
    # cookie token; the token itself is never stored.
    op.execute(
        """
        CREATE TABLE sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            user_id uuid NOT NULL,
            token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
            csrf_token text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            FOREIGN KEY (workspace_id, user_id)
                REFERENCES workspace_members (workspace_id, user_id) ON DELETE CASCADE
        )
        """
    )
    for table, key_column in TENANT_KEY_COLUMNS.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} FOR ALL"
            f" USING ({key_column} = app_current_workspace_id())"
            f" WITH CHECK ({key_column} = app_current_workspace_id())"
        )
    for table, privileges in APP_PRIVILEGES.items():
        op.execute(f"GRANT {privileges} ON {table} TO {DATABASE_APP_ROLE}")


def downgrade() -> None:
    for table, privileges in reversed(APP_PRIVILEGES.items()):
        op.execute(f"REVOKE {privileges} ON {table} FROM {DATABASE_APP_ROLE}")
    for table in reversed(TENANT_KEY_COLUMNS):
        op.execute(f"DROP POLICY {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE sessions")
    op.execute("ALTER TABLE workspace_members DROP CONSTRAINT workspace_members_role_check")
    op.execute("DROP FUNCTION app_current_workspace_id()")

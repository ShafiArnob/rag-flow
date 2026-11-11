"""Workflows, workflow versions, secrets, connectors and the audit log. Part of P01_S03.

Every reference between tenant tables is composite, ``(workspace_id, x_id)`` against a
``UNIQUE (workspace_id, id)`` on the parent. Foreign key checks ignore row-level security, so a key
on ``x_id`` alone would let one workspace link to, probe for, or block deletion of another
workspace's rows.

Each table gets row-level security as in 0002 before the application role is granted anything.
``workflow_versions`` is granted no UPDATE or DELETE, so a published version never changes, and
``audit_log`` is append-only: the application role may insert only the columns that describe the
event, so ``id`` and ``at`` always come from their defaults.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from app.database_roles import DATABASE_APP_ROLE

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TENANT_TABLES = ("workflows", "workflow_versions", "secrets", "connectors", "audit_log")

APP_PRIVILEGES = {
    "workflows": "SELECT, INSERT, UPDATE, DELETE",
    "workflow_versions": "SELECT, INSERT",
    "secrets": "SELECT, INSERT, UPDATE, DELETE",
    "connectors": "SELECT, INSERT, UPDATE, DELETE",
    "audit_log": "SELECT, INSERT (workspace_id, actor_id, action, target, ip)",
}


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflows (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            type text NOT NULL CONSTRAINT workflows_type_check CHECK (type IN ('upload', 'chat')),
            name text NOT NULL,
            draft_graph jsonb NOT NULL,
            schema_version integer NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (workspace_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE workflow_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            workflow_id uuid NOT NULL,
            version_no integer NOT NULL
                CONSTRAINT workflow_versions_version_no_check CHECK (version_no >= 1),
            graph jsonb NOT NULL,
            created_by uuid NOT NULL REFERENCES users (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (workflow_id, version_no),
            UNIQUE (workspace_id, workflow_id, id),
            FOREIGN KEY (workspace_id, workflow_id)
                REFERENCES workflows (workspace_id, id) ON DELETE CASCADE
        )
        """
    )
    # The nonce is the 12-byte AES-GCM nonce that P01_S05 uses.
    op.execute(
        """
        CREATE TABLE secrets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            ciphertext bytea NOT NULL,
            wrapped_dek bytea NOT NULL,
            key_id text NOT NULL,
            nonce bytea NOT NULL CONSTRAINT secrets_nonce_check CHECK (octet_length(nonce) = 12),
            last4 text NOT NULL CONSTRAINT secrets_last4_check CHECK (char_length(last4) <= 4),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (workspace_id, id)
        )
        """
    )
    # MATCH SIMPLE, the default, skips the secret key when secret_ref is NULL: a connector may need
    # no secret.
    op.execute(
        """
        CREATE TABLE connectors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            type text NOT NULL,
            name text NOT NULL,
            config jsonb NOT NULL,
            secret_ref uuid,
            status text NOT NULL DEFAULT 'untested' CONSTRAINT connectors_status_check
                CHECK (status IN ('untested', 'passed', 'failed')),
            last_checked_at timestamptz,
            UNIQUE (workspace_id, id),
            FOREIGN KEY (workspace_id, secret_ref) REFERENCES secrets (workspace_id, id)
        )
        """
    )
    # A NULL actor_id records a system action.
    op.execute(
        """
        CREATE TABLE audit_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            actor_id uuid REFERENCES users (id),
            action text NOT NULL CONSTRAINT audit_log_action_check CHECK (
                action IN (
                    'login',
                    'logout',
                    'connector.create',
                    'connector.delete',
                    'secret.rotate',
                    'workflow.delete'
                )
            ),
            target uuid,
            ip inet,
            at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX audit_log_workspace_id_at_idx ON audit_log (workspace_id, at DESC)")
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} FOR ALL"
            " USING (workspace_id = app_current_workspace_id())"
            " WITH CHECK (workspace_id = app_current_workspace_id())"
        )
    for table, privileges in APP_PRIVILEGES.items():
        op.execute(f"GRANT {privileges} ON {table} TO {DATABASE_APP_ROLE}")


def downgrade() -> None:
    # Dropping a table drops its policies and grants with it.
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")

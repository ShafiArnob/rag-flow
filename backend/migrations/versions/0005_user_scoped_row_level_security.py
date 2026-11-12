"""User-scoped row-level security on ``users``. Part of P01_S04.

``users`` has no workspace key, so 0002 left it without row-level security and any query could list
every account. Authentication needs it protected, and needs two lookups that happen before a
workspace is known: login finds a user by email, then finds that user's membership.

A transaction can read a user row only if it acts for that user, or names that user's exact email as
the one logging in. No policy lets it list users. Inserts are open, because signup creates a user
before anyone is logged in and the unique email constraint still applies; a row can only be updated
by its own user. ``workspace_members`` gains a second, SELECT-only policy so a user can read their
own memberships without a workspace.

The application role's privileges do not change.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULLIF for the same reason as app_current_workspace_id() in 0002: a pooled connection that
    # has run such a transaction carries '' instead of NULL, and ''::uuid raises.
    op.execute(
        """
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
        """
    )
    # Returns citext, not text: with a text result the policy's comparison would resolve to
    # text = text and match emails case-sensitively, unlike the unique constraint.
    op.execute(
        """
        CREATE FUNCTION app_login_email() RETURNS citext
        LANGUAGE sql STABLE
        AS $$ SELECT NULLIF(current_setting('app.login_email', true), '')::citext $$
        """
    )
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY users_select ON users FOR SELECT"
        " USING (id = app_current_user_id() OR email = app_login_email())"
    )
    op.execute("CREATE POLICY users_insert ON users FOR INSERT WITH CHECK (true)")
    op.execute(
        "CREATE POLICY users_update_self ON users FOR UPDATE"
        " USING (id = app_current_user_id())"
        " WITH CHECK (id = app_current_user_id())"
    )
    op.execute(
        "CREATE POLICY workspace_members_self ON workspace_members FOR SELECT"
        " USING (user_id = app_current_user_id())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY workspace_members_self ON workspace_members")
    op.execute("DROP POLICY users_update_self ON users")
    op.execute("DROP POLICY users_insert ON users")
    op.execute("DROP POLICY users_select ON users")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION app_login_email()")
    op.execute("DROP FUNCTION app_current_user_id()")

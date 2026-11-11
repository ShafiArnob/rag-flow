"""Baseline: the minimum tables the demo seed needs.

P01_S03 extends these tables with row-level security and adds the rest of the core schema.

This revision used to create the vector and citext extensions. P01_S03 moved that to
``app.database_bootstrap``, because migrations now run as a role that is not a superuser, and only a
superuser can create vector or drop either extension. Editing this merged revision was a one-time
exception approved in the P01_S03 plan.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workspaces (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email citext NOT NULL UNIQUE,
            password_hash text,
            sso_subject text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE workspace_members (
            workspace_id uuid NOT NULL REFERENCES workspaces (id),
            user_id uuid NOT NULL REFERENCES users (id),
            role text NOT NULL,
            PRIMARY KEY (workspace_id, user_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE workspace_members")
    op.execute("DROP TABLE users")
    op.execute("DROP TABLE workspaces")

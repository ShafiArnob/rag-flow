"""Lets the owner role read and update every workspace's rows of ``secrets``, for key rotation.
Part of P01_S05.

Rotating a master key means re-wrapping the data key of every secret in every workspace, which no
workspace transaction can see. The rotation command (``flowbuilder keys rotate``) runs as the owner
with row security on, and these two permissive policies, OR-ed with ``secrets_tenant``, let it read
and update any row of ``secrets``.

They are not a security boundary against the owner. Row-level security limits rows, not columns,
and ``WITH CHECK (true)`` accepts any new value in any column; the owner could also drop the
policies, since it owns the table. Only the rotation command's own queries keep it to
``wrapped_dek`` and ``key_id``. What the policies do is make rotation's cross-workspace access
explicit, in the catalog, and guard against mistakes: there is no owner INSERT or DELETE policy, so
an owner statement that creates or removes a secret still fails or touches no row, and no other
tenant table changes, so the owner still sees no row of ``connectors``. The policies name the
owner, so the application role gains nothing.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

from app.database.roles import DATABASE_OWNER_ROLE

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE POLICY secrets_key_rotation_select ON secrets FOR SELECT"
        f" TO {DATABASE_OWNER_ROLE} USING (true)"
    )
    op.execute(
        "CREATE POLICY secrets_key_rotation_update ON secrets FOR UPDATE"
        f" TO {DATABASE_OWNER_ROLE} USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY secrets_key_rotation_update ON secrets")
    op.execute("DROP POLICY secrets_key_rotation_select ON secrets")

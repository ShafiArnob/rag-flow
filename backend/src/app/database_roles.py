"""The database roles, and the check that the application role cannot bypass row-level security."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Owns every schema object and runs Alembic.
DATABASE_OWNER_ROLE = "flowbuilder_owner"
# Used by the API, the worker and the seed; row-level security always applies to it.
DATABASE_APP_ROLE = "flowbuilder_app"

_ROLE_CHECKS = text(
    """
    SELECT
        r.rolname AS role_name,
        r.rolsuper AS is_superuser,
        r.rolbypassrls AS has_bypassrls,
        EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS is_member_of_a_role,
        EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND pg_has_role(r.oid, c.relowner, 'USAGE')
        ) OR EXISTS (
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND pg_has_role(r.oid, p.proowner, 'USAGE')
        ) AS has_owner_privileges_in_public,
        NULLIF(current_setting('app.workspace_id', true), '') IS NOT NULL AS sees_workspace_setting
    FROM pg_roles r
    WHERE r.rolname = current_user
    """
)

# Each of these lets a connection read or write rows outside its workspace.
_BYPASS_DESCRIPTIONS = {
    "is_superuser": "is a superuser",
    "has_bypassrls": "has BYPASSRLS",
    "is_member_of_a_role": "is a member of another role",
    "has_owner_privileges_in_public": (
        "owns, or has the privileges of the owner of, a relation or function in schema public"
    ),
    "sees_workspace_setting": "sees app.workspace_id already set on a new connection",
}


async def assert_role_is_subject_to_row_level_security(engine: AsyncEngine) -> None:
    """Raises RuntimeError, naming the role and each failed check, if the engine's role can
    bypass row-level security. Run it before the engine serves any request."""
    async with engine.connect() as connection:
        checks = (await connection.execute(_ROLE_CHECKS)).mappings().one()
    failures = [description for check, description in _BYPASS_DESCRIPTIONS.items() if checks[check]]
    if failures:
        raise RuntimeError(
            f"database role {checks['role_name']!r} must be subject to row-level security,"
            f" but it {'; it '.join(failures)}"
        )

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.roles import (
    DATABASE_OWNER_ROLE,
    assert_role_is_subject_to_row_level_security,
)
from support.database_helpers import PROBE_ROLE, probe_role_engine

pytestmark = pytest.mark.integration

GUARD_PROBE_TABLE = "rls_guard_probe"
PROBE_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
PROBE_USER_ID = "00000000-0000-4000-8000-000000000002"
PROBE_LOGIN_EMAIL = "probe@example.test"


async def test_guard_accepts_app_role(app_engine: AsyncEngine) -> None:
    await assert_role_is_subject_to_row_level_security(app_engine)


async def test_guard_rejects_superuser_owner_and_member_of_owner(
    superuser_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    superuser = make_url(os.environ["BOOTSTRAP_DATABASE_URL"]).username
    with pytest.raises(RuntimeError, match=f"'{superuser}' .* it is a superuser"):
        await assert_role_is_subject_to_row_level_security(superuser_engine)

    # The owner owns nothing until migrations run, so give it an object to own.
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE TABLE {GUARD_PROBE_TABLE} (id int)"))
    try:
        with pytest.raises(
            RuntimeError, match=f"'{DATABASE_OWNER_ROLE}' .* owns, or has the privileges"
        ):
            await assert_role_is_subject_to_row_level_security(owner_engine)
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE {GUARD_PROBE_TABLE}"))

    async with probe_role_engine(
        superuser_engine, f"GRANT {DATABASE_OWNER_ROLE} TO {PROBE_ROLE}"
    ) as member_of_owner:
        with pytest.raises(RuntimeError, match=f"'{PROBE_ROLE}' .* is a member of another role"):
            await assert_role_is_subject_to_row_level_security(member_of_owner)


@pytest.mark.parametrize(
    ("setup_statement", "failed_check"),
    [
        (f"ALTER ROLE {PROBE_ROLE} BYPASSRLS", "has BYPASSRLS"),
        (
            f"ALTER ROLE {PROBE_ROLE} SET app.workspace_id = '{PROBE_WORKSPACE_ID}'",
            "sees app.workspace_id already set on a new connection",
        ),
        (
            f"ALTER ROLE {PROBE_ROLE} SET app.user_id = '{PROBE_USER_ID}'",
            "sees app.user_id already set on a new connection",
        ),
        (
            f"ALTER ROLE {PROBE_ROLE} SET app.login_email = '{PROBE_LOGIN_EMAIL}'",
            "sees app.login_email already set on a new connection",
        ),
    ],
)
async def test_guard_rejects_role_that_bypasses_rls_or_starts_with_a_tenancy_setting(
    superuser_engine: AsyncEngine, setup_statement: str, failed_check: str
) -> None:
    async with probe_role_engine(superuser_engine, setup_statement) as probe:
        with pytest.raises(RuntimeError, match=f"'{PROBE_ROLE}' .* {failed_check}"):
            await assert_role_is_subject_to_row_level_security(probe)

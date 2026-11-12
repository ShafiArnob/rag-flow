import os
import secrets

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.bootstrap import bootstrap_database
from app.database.engine import create_database_engine
from app.database.roles import DATABASE_APP_ROLE, DATABASE_OWNER_ROLE
from support.database_helpers import PROBE_ROLE, database_url_with, probe_role_engine

pytestmark = pytest.mark.integration

AUTHENTICATION_FAILED_SQLSTATE = "28P01"
READ_ONLY_TRANSACTION_SQLSTATE = "25006"


def role_passwords() -> tuple[SecretStr, SecretStr]:
    return (
        SecretStr(os.environ["DATABASE_OWNER_PASSWORD"]),
        SecretStr(os.environ["DATABASE_APP_PASSWORD"]),
    )


async def logged_in_role(url: SecretStr) -> str:
    engine = create_database_engine(url)
    try:
        async with engine.connect() as connection:
            return str((await connection.execute(text("SELECT current_user"))).scalar_one())
    finally:
        await engine.dispose()


async def test_bootstrap_is_idempotent_and_passwords_authenticate_over_network(
    superuser_engine: AsyncEngine,
) -> None:
    await bootstrap_database(superuser_engine, *role_passwords())
    async with superuser_engine.begin() as connection:
        # Drift that a second run must undo.
        await connection.execute(text(f"ALTER ROLE {DATABASE_APP_ROLE} CREATEDB BYPASSRLS"))
        await connection.execute(text(f"ALTER ROLE {DATABASE_APP_ROLE} SET search_path = pg_temp"))

    await bootstrap_database(superuser_engine, *role_passwords())

    async with superuser_engine.connect() as connection:
        roles = (
            await connection.execute(
                text(
                    "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
                    " rolcreaterole, rolreplication, rolconfig FROM pg_roles"
                    " WHERE rolname IN (:owner, :app) ORDER BY rolname"
                ),
                {"owner": DATABASE_OWNER_ROLE, "app": DATABASE_APP_ROLE},
            )
        ).all()
        extensions = (
            await connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'citext')")
            )
        ).scalars()
        owner_can_create = (
            await connection.execute(
                text("SELECT has_schema_privilege(:owner, 'public', 'CREATE')"),
                {"owner": DATABASE_OWNER_ROLE},
            )
        ).scalar_one()
    assert [tuple(role) for role in roles] == [
        (DATABASE_APP_ROLE, True, False, False, False, False, False, None),
        (DATABASE_OWNER_ROLE, True, False, False, False, False, False, None),
    ]
    assert sorted(extensions) == ["citext", "vector"]
    assert owner_can_create is True

    assert await logged_in_role(SecretStr(os.environ["DATABASE_URL"])) == DATABASE_APP_ROLE
    assert (
        await logged_in_role(SecretStr(os.environ["MIGRATION_DATABASE_URL"])) == DATABASE_OWNER_ROLE
    )
    # Negative control: a wrong password must fail, which proves the connection is not trusted
    # loopback and the passwords above were really checked.
    with pytest.raises(DBAPIError) as raised:
        await logged_in_role(database_url_with("DATABASE_URL", password=secrets.token_urlsafe(24)))
    assert getattr(raised.value.orig, "sqlstate", None) == AUTHENTICATION_FAILED_SQLSTATE


async def test_bootstrap_refuses_non_superuser(app_engine: AsyncEngine) -> None:
    owner_password, app_password = role_passwords()
    sent: list[str] = []

    def record(
        _connection: object, _cursor: object, statement: str, parameters: object, *_: object
    ) -> None:
        sent.append(f"{statement} {parameters!r}")

    event.listen(app_engine.sync_engine, "before_cursor_execute", record)

    with pytest.raises(RuntimeError, match=f"superuser, but role '{DATABASE_APP_ROLE}'"):
        await bootstrap_database(app_engine, owner_password, app_password)

    assert sent
    assert not any(owner_password.get_secret_value() in item for item in sent)
    assert not any(app_password.get_secret_value() in item for item in sent)


async def test_bootstrap_reports_database_errors_as_type_and_sqlstate_only(
    superuser_engine: AsyncEngine,
) -> None:
    async with probe_role_engine(
        superuser_engine,
        f"ALTER ROLE {PROBE_ROLE} SUPERUSER",
        f"ALTER ROLE {PROBE_ROLE} SET default_transaction_read_only = on",
    ) as read_only_superuser:
        with pytest.raises(RuntimeError) as raised:
            await bootstrap_database(read_only_superuser, *role_passwords())

    message = str(raised.value)
    assert f"(SQLSTATE {READ_ONLY_TRANSACTION_SQLSTATE})" in message
    assert "EXTENSION" not in message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True

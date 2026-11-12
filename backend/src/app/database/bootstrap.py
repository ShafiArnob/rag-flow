"""Creates the extensions and database roles that migrations and the application rely on.

Run as the superuser before migrations: ``python -m app.database.bootstrap``. It runs in one
transaction and is idempotent: each run re-asserts the role attributes and passwords, and clears
any per-role settings, so a failure leaves the database as it was.
"""

import asyncio

from pydantic import SecretStr
from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.config import BootstrapSettings
from app.database.engine import create_database_engine
from app.database.roles import DATABASE_APP_ROLE, DATABASE_OWNER_ROLE

_CURRENT_ROLE = text(
    "SELECT rolname AS role_name, rolsuper AS is_superuser FROM pg_roles"
    " WHERE rolname = current_user"
)

# Role statements carry plaintext passwords, so no statement in this transaction may reach the
# server log, including on error or through duration sampling.
_STATEMENT_LOGGING_OFF = (
    text("SET LOCAL log_statement = 'none'"),
    text("SET LOCAL log_min_error_statement = 'panic'"),
    text("SET LOCAL log_min_duration_statement = -1"),
    text("SET LOCAL log_min_duration_sample = -1"),
    text("SET LOCAL log_transaction_sample_rate = 0"),
)

# vector can only be created by a superuser; citext is created here too, so the superuser owns
# every extension.
_CREATE_EXTENSIONS = (
    text("CREATE EXTENSION IF NOT EXISTS vector"),
    text("CREATE EXTENSION IF NOT EXISTS citext"),
)

# A DO block cannot take bind parameters, so the server builds each statement with format() and
# it runs as plain SQL. The casts are needed because format() takes VARIADIC "any".
_CREATE_OR_ALTER_ROLE = text(
    """
    SELECT CASE
        WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = CAST(:role AS text)) THEN format(
            'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE'
            ' NOREPLICATION PASSWORD %L',
            CAST(:role AS text), CAST(:password AS text)
        )
        ELSE format(
            'CREATE ROLE %I WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE'
            ' NOREPLICATION PASSWORD %L',
            CAST(:role AS text), CAST(:password AS text)
        )
    END
    """
)
_RESET_ROLE_SETTINGS = text("SELECT format('ALTER ROLE %I RESET ALL', CAST(:role AS text))")
_GRANT_CREATE_ON_PUBLIC = text(
    "SELECT format('GRANT CREATE ON SCHEMA public TO %I', CAST(:role AS text))"
)


async def bootstrap_database(
    engine: AsyncEngine, owner_password: SecretStr, app_password: SecretStr
) -> None:
    """Raises RuntimeError if the engine's role is not a superuser, before any password is sent.

    A database error is re-raised as its type and SQLSTATE only, because its message can quote
    a statement that holds a password.
    """
    try:
        async with engine.begin() as connection:
            await _require_superuser(connection)
            for statement in (*_STATEMENT_LOGGING_OFF, *_CREATE_EXTENSIONS):
                await connection.execute(statement)
            for role, password in (
                (DATABASE_OWNER_ROLE, owner_password),
                (DATABASE_APP_ROLE, app_password),
            ):
                await _run_generated_statement(
                    connection,
                    _CREATE_OR_ALTER_ROLE,
                    {"role": role, "password": password.get_secret_value()},
                )
                await _run_generated_statement(connection, _RESET_ROLE_SETTINGS, {"role": role})
            await _run_generated_statement(
                connection, _GRANT_CREATE_ON_PUBLIC, {"role": DATABASE_OWNER_ROLE}
            )
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None)
        raise RuntimeError(
            f"database bootstrap failed with {type(error).__name__} (SQLSTATE {sqlstate})"
        ) from None


async def _require_superuser(connection: AsyncConnection) -> None:
    role = (await connection.execute(_CURRENT_ROLE)).mappings().one()
    if not role["is_superuser"]:
        raise RuntimeError(
            f"database bootstrap must run as a superuser, but role {role['role_name']!r} is not one"
        )


async def _run_generated_statement(
    connection: AsyncConnection, generator: TextClause, parameters: dict[str, str]
) -> None:
    statement = (await connection.execute(generator, parameters)).scalar_one()
    await connection.exec_driver_sql(statement)


async def main() -> None:
    settings = BootstrapSettings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.bootstrap_database_url)
    try:
        await bootstrap_database(
            engine, settings.database_owner_password, settings.database_app_password
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

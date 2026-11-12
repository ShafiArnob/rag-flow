"""Helpers for integration tests that log in as a role other than the configured ones."""

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.engine import create_database_engine

PROBE_ROLE = "flowbuilder_test_probe"

_CREATE_LOGIN_ROLE = text(
    "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L',"
    " CAST(:role AS text), CAST(:password AS text))"
)


def database_url_with(variable: str, *, password: str, username: str | None = None) -> SecretStr:
    """The URL in ``variable`` with its credentials replaced; host and database are kept, so the
    connection still goes over the network and the password is really checked."""
    url = make_url(os.environ[variable]).set(password=password)
    if username is not None:
        url = url.set(username=username)
    return SecretStr(url.render_as_string(hide_password=False))


@asynccontextmanager
async def probe_role_engine(
    superuser_engine: AsyncEngine, *setup_statements: str
) -> AsyncIterator[AsyncEngine]:
    """Creates PROBE_ROLE, runs ``setup_statements`` as the superuser, yields an engine logged in
    as the probe role, then drops the role."""
    password = secrets.token_urlsafe(24)
    async with superuser_engine.begin() as connection:
        await connection.execute(text(f"DROP ROLE IF EXISTS {PROBE_ROLE}"))
        create_role = (
            await connection.execute(_CREATE_LOGIN_ROLE, {"role": PROBE_ROLE, "password": password})
        ).scalar_one()
        await connection.exec_driver_sql(create_role)
        for statement in setup_statements:
            await connection.execute(text(statement))
    engine = create_database_engine(
        database_url_with("BOOTSTRAP_DATABASE_URL", username=PROBE_ROLE, password=password)
    )
    try:
        yield engine
    finally:
        await engine.dispose()
        async with superuser_engine.begin() as connection:
            await connection.execute(text(f"DROP ROLE {PROBE_ROLE}"))

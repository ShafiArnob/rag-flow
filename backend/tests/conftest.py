"""Shared fixtures. Integration tests expect `python -m app.database.bootstrap` to have run, as
it does in infra/docker-compose.test.yml and CI."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.engine import create_database_engine
from app.main import create_app
from support.http_users import TwoUsers, signed_up_over_http
from support.tenants import TwoTenants, create_tenant
from support.unreachable_services import UNREACHABLE_DATABASE_URL, UNREACHABLE_REDIS_URL

TEST_DATABASE_URL_VARIABLES = ("DATABASE_URL", "MIGRATION_DATABASE_URL", "BOOTSTRAP_DATABASE_URL")
TEST_DATABASE_NAME_SUFFIX = "_test"
ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def refuse_databases_not_named_for_tests(request: pytest.FixtureRequest) -> None:
    """The integration suite migrates down to an empty schema and changes roles, so it must never
    reach a developer or production database."""
    if not any(item.get_closest_marker("integration") for item in request.session.items):
        return
    for variable in TEST_DATABASE_URL_VARIABLES:
        url = os.environ.get(variable)
        if url is None:
            continue
        database = make_url(url).database or ""
        if not database.endswith(TEST_DATABASE_NAME_SUFFIX):
            pytest.exit(
                f"{variable} names database {database!r}; integration tests run only against a"
                f" database whose name ends in {TEST_DATABASE_NAME_SUFFIX!r}",
                returncode=1,
            )


@pytest.fixture(scope="module")
def schema_at_head() -> None:
    """Migrates to head, so a module that needs the schema does not depend on the migration tests
    having run first and left it there. Synchronous, because env.py runs its own event loop."""
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


async def engine_from_environment(variable: str) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(SecretStr(os.environ[variable]))
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    async for engine in engine_from_environment("DATABASE_URL"):
        yield engine


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    async for engine in engine_from_environment("MIGRATION_DATABASE_URL"):
        yield engine


@pytest.fixture
async def superuser_engine() -> AsyncIterator[AsyncEngine]:
    async for engine in engine_from_environment("BOOTSTRAP_DATABASE_URL"):
        yield engine


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    """The Redis at REDIS_URL. Tests keep apart by using their own emails and peer addresses, so
    nothing is flushed."""
    client = Redis.from_url(os.environ["REDIS_URL"])
    yield client
    await client.aclose()


@pytest.fixture
async def two_tenants(schema_at_head: None, app_engine: AsyncEngine) -> TwoTenants:
    """Two new workspaces, each with a row in every tenant table, written as the application role.
    Ids are random, so the rows never collide with other tests' rows and need no cleanup."""
    return TwoTenants(first=await create_tenant(app_engine), second=await create_tenant(app_engine))


@pytest.fixture
async def two_users(
    schema_at_head: None, app_engine: AsyncEngine, redis: Redis
) -> AsyncIterator[TwoUsers]:
    """Two users signed up over HTTP, each with its own client holding its own session cookie."""
    async with (
        signed_up_over_http(app_engine, redis) as first,
        signed_up_over_http(app_engine, redis) as second,
    ):
        yield TwoUsers(first=first, second=second)


@pytest.fixture
async def app_without_database() -> AsyncIterator[FastAPI]:
    """Requests without a session cookie are refused before the database or Redis is reached, so
    tests of those, and tests that only list an app's routes, need neither."""
    engine = create_database_engine(SecretStr(UNREACHABLE_DATABASE_URL))
    redis = Redis.from_url(UNREACHABLE_REDIS_URL)
    yield create_app(TracerProvider(), {}, engine, redis, session_cookie_secure=True)
    await redis.aclose()
    await engine.dispose()

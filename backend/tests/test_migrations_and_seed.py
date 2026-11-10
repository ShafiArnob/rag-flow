import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.seed import DEMO_USER_EMAIL, DEMO_WORKSPACE_NAME, seed_demo_data

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"


async def count_rows(table: str) -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def seed_twice() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        await seed_demo_data(engine)
        await seed_demo_data(engine)
        async with engine.connect() as connection:
            member = await connection.execute(
                text(
                    "SELECT u.email, w.name FROM workspace_members m"
                    " JOIN users u ON u.id = m.user_id"
                    " JOIN workspaces w ON w.id = m.workspace_id"
                )
            )
            assert member.all() == [(DEMO_USER_EMAIL, DEMO_WORKSPACE_NAME)]
    finally:
        await engine.dispose()


def test_migrations_apply_roll_back_and_seed_is_idempotent() -> None:
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "base")

    command.upgrade(config, "head")
    asyncio.run(seed_twice())
    assert asyncio.run(count_rows("workspaces")) == 1
    assert asyncio.run(count_rows("users")) == 1

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert asyncio.run(count_rows("workspaces")) == 0

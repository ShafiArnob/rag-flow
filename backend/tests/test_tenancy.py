import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import create_database_engine
from app.tenancy import workspace_transaction

BACKEND_DIRECTORY = Path(__file__).parent.parent
SCANNED_DIRECTORIES = (BACKEND_DIRECTORY / "src", BACKEND_DIRECTORY / "migrations")
TENANCY_MODULE = BACKEND_DIRECTORY / "src" / "app" / "tenancy.py"

# Any of these could set app.workspace_id for the whole session, which then survives the commit and
# reaches the next user of the pooled connection.
WORKSPACE_SETTING_WRITE = re.compile(
    r"set_config\s*\(|\b(?:set(?:\s+(?:local|session))?|reset)\s+app\.", re.IGNORECASE
)

CONNECTION_STATE = text(
    "SELECT pg_backend_pid() AS backend_pid,"
    " NULLIF(current_setting('app.workspace_id', true), '') AS workspace_id"
)


class AbandonedTransaction(Exception):
    """Raised inside a workspace transaction to leave it through a Python exception."""


async def connection_state(engine: AsyncEngine) -> tuple[int, str | None]:
    async with engine.connect() as connection:
        state = (await connection.execute(CONNECTION_STATE)).one()
    return state.backend_pid, state.workspace_id


@pytest.fixture
async def single_connection_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(SecretStr(os.environ["DATABASE_URL"]), pool_size=1)
    yield engine
    await engine.dispose()


@pytest.mark.integration
async def test_new_and_reused_pooled_connections_carry_no_workspace_setting(
    single_connection_engine: AsyncEngine,
) -> None:
    engine = single_connection_engine
    workspace_id = uuid.uuid4()
    backend_pid, setting = await connection_state(engine)
    assert setting is None

    async with workspace_transaction(engine, workspace_id) as connection:
        inside = (await connection.execute(CONNECTION_STATE)).one()
    # The negative control: the setting is really applied, on the same physical connection.
    assert (inside.backend_pid, inside.workspace_id) == (backend_pid, str(workspace_id))
    assert await connection_state(engine) == (backend_pid, None)

    with pytest.raises(AbandonedTransaction):
        async with workspace_transaction(engine, workspace_id):
            raise AbandonedTransaction
    assert await connection_state(engine) == (backend_pid, None)

    with pytest.raises(DBAPIError, match="division by zero"):
        async with workspace_transaction(engine, workspace_id) as connection:
            await connection.execute(text("SELECT 1 / 0"))
    assert await connection_state(engine) == (backend_pid, None)


def test_workspace_setting_is_only_set_by_tenancy_module() -> None:
    assert WORKSPACE_SETTING_WRITE.search(TENANCY_MODULE.read_text(encoding="utf-8")), (
        "the scan no longer matches how app.tenancy sets the workspace"
    )
    offenders: list[str] = []
    for directory in SCANNED_DIRECTORIES:
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path == TENANCY_MODULE or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in WORKSPACE_SETTING_WRITE.finditer(source):
                line_number = source.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(BACKEND_DIRECTORY)}:{line_number}")
    assert offenders == [], "only app/tenancy.py may set app.workspace_id"

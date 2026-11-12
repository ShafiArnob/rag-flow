import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.database import create_database_engine
from app.tenancy import (
    login_transaction,
    member_transaction,
    user_transaction,
    workspace_transaction,
)

BACKEND_DIRECTORY = Path(__file__).parent.parent
SCANNED_DIRECTORIES = (BACKEND_DIRECTORY / "src", BACKEND_DIRECTORY / "migrations")
TENANCY_MODULE = BACKEND_DIRECTORY / "src" / "app" / "tenancy.py"

# Any of these could set an app.* setting for the whole session, which then survives the commit and
# reaches the next user of the pooled connection.
SETTING_WRITE = re.compile(
    r"set_config\s*\(|\b(?:set(?:\s+(?:local|session))?|reset)\s+app\.", re.IGNORECASE
)

CONNECTION_STATE = text(
    "SELECT pg_backend_pid() AS backend_pid,"
    " NULLIF(current_setting('app.workspace_id', true), '') AS workspace_id,"
    " NULLIF(current_setting('app.user_id', true), '') AS user_id,"
    " NULLIF(current_setting('app.login_email', true), '') AS login_email"
)

WORKSPACE_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
LOGIN_EMAIL = "someone@example.test"


@dataclass(frozen=True)
class Settings:
    """Who a connection acts for; None where the setting is unset."""

    workspace_id: str | None = None
    user_id: str | None = None
    login_email: str | None = None


NO_SETTINGS = Settings()

TransactionOpener = Callable[[AsyncEngine], AbstractAsyncContextManager[AsyncConnection]]

# Every transaction app.tenancy offers, with the settings it must apply and nothing more.
TRANSACTIONS: dict[str, tuple[TransactionOpener, Settings]] = {
    "workspace": (
        lambda engine: workspace_transaction(engine, WORKSPACE_ID),
        Settings(workspace_id=str(WORKSPACE_ID)),
    ),
    "member": (
        lambda engine: member_transaction(engine, WORKSPACE_ID, USER_ID),
        Settings(workspace_id=str(WORKSPACE_ID), user_id=str(USER_ID)),
    ),
    "user": (
        lambda engine: user_transaction(engine, USER_ID),
        Settings(user_id=str(USER_ID)),
    ),
    "login": (
        lambda engine: login_transaction(engine, LOGIN_EMAIL),
        Settings(login_email=LOGIN_EMAIL),
    ),
}


class AbandonedTransaction(Exception):
    """Raised inside a transaction to leave it through a Python exception."""


async def state_of(connection: AsyncConnection) -> tuple[int, Settings]:
    state = (await connection.execute(CONNECTION_STATE)).one()
    return state.backend_pid, Settings(state.workspace_id, state.user_id, state.login_email)


async def connection_state(engine: AsyncEngine) -> tuple[int, Settings]:
    async with engine.connect() as connection:
        return await state_of(connection)


@pytest.fixture
async def single_connection_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(SecretStr(os.environ["DATABASE_URL"]), pool_size=1)
    yield engine
    await engine.dispose()


@pytest.mark.integration
@pytest.mark.parametrize(("open_transaction", "applied"), TRANSACTIONS.values(), ids=TRANSACTIONS)
async def test_new_and_reused_pooled_connections_carry_no_settings(
    single_connection_engine: AsyncEngine, open_transaction: TransactionOpener, applied: Settings
) -> None:
    engine = single_connection_engine
    backend_pid, settings = await connection_state(engine)
    assert settings == NO_SETTINGS

    async with open_transaction(engine) as connection:
        # The negative control: the settings are really applied, on the same physical connection.
        assert await state_of(connection) == (backend_pid, applied)
    assert await connection_state(engine) == (backend_pid, NO_SETTINGS)

    with pytest.raises(AbandonedTransaction):
        async with open_transaction(engine):
            raise AbandonedTransaction
    assert await connection_state(engine) == (backend_pid, NO_SETTINGS)

    with pytest.raises(DBAPIError, match="division by zero"):
        async with open_transaction(engine) as connection:
            await connection.execute(text("SELECT 1 / 0"))
    assert await connection_state(engine) == (backend_pid, NO_SETTINGS)


def test_settings_are_only_set_by_tenancy_module() -> None:
    assert SETTING_WRITE.search(TENANCY_MODULE.read_text(encoding="utf-8")), (
        "the scan no longer matches how app.tenancy sets who a transaction acts for"
    )
    offenders: list[str] = []
    for directory in SCANNED_DIRECTORIES:
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path == TENANCY_MODULE or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in SETTING_WRITE.finditer(source):
                line_number = source.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(BACKEND_DIRECTORY)}:{line_number}")
    assert offenders == [], "only app/tenancy.py may set an app.* setting"

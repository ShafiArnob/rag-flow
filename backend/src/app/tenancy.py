"""The only place that sets who a database transaction acts for: a workspace, a user, or an email
that is logging in."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_WORKSPACE_SETTING = "app.workspace_id"
_USER_SETTING = "app.user_id"
_LOGIN_EMAIL_SETTING = "app.login_email"

# is_local = true: the setting ends with the transaction, so it can never reach the next user of a
# pooled connection. A session-level setting would survive the commit.
_SET_FOR_TRANSACTION = text("SELECT set_config(:setting, :value, true)")


@asynccontextmanager
async def _transaction_acting_for(
    engine: AsyncEngine, settings: dict[str, str]
) -> AsyncIterator[AsyncConnection]:
    async with engine.begin() as connection:
        for setting, value in settings.items():
            await connection.execute(_SET_FOR_TRANSACTION, {"setting": setting, "value": value})
        yield connection


@asynccontextmanager
async def workspace_transaction(
    engine: AsyncEngine, workspace_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """Yields a connection in a new transaction whose row-level security policies see only
    ``workspace_id``. The transaction commits when the block exits normally and rolls back if it
    raises.

    It acts for no user, so it sees no row of ``users``.

    Never commit inside the block: the workspace setting ends with the transaction, so later
    statements would see no rows and every write would be rejected. Never swallow a database error
    inside it either: the transaction is already aborted, and catching the error does not undo that.
    """
    # asyncpg sends the parameter as text, so it must be a str, not a UUID.
    async with _transaction_acting_for(
        engine, {_WORKSPACE_SETTING: str(workspace_id)}
    ) as connection:
        yield connection


@asynccontextmanager
async def member_transaction(
    engine: AsyncEngine, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """A ``workspace_transaction`` that also acts for ``user_id``: it sees that user's row of
    ``users`` and may update it, and it sees the user's memberships of other workspaces.

    It does not check that the user belongs to the workspace; the caller must have established that.
    The rules of ``workspace_transaction`` about committing and database errors apply.
    """
    async with _transaction_acting_for(
        engine, {_WORKSPACE_SETTING: str(workspace_id), _USER_SETTING: str(user_id)}
    ) as connection:
        yield connection


@asynccontextmanager
async def user_transaction(
    engine: AsyncEngine, user_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """Yields a connection in a new transaction that acts for ``user_id`` and no workspace: it sees
    that user's row of ``users`` and the user's memberships, and no other tenant row. Login uses it
    to find the workspace to act in.

    The rules of ``workspace_transaction`` about committing and database errors apply.
    """
    async with _transaction_acting_for(engine, {_USER_SETTING: str(user_id)}) as connection:
        yield connection


@asynccontextmanager
async def login_transaction(engine: AsyncEngine, email: str) -> AsyncIterator[AsyncConnection]:
    """Yields a connection in a new transaction that can read the one row of ``users`` whose email
    equals ``email``, compared case-insensitively, and nothing else. It cannot update that row.
    Login uses it to find the user before anyone is authenticated.

    The rules of ``workspace_transaction`` about committing and database errors apply.
    """
    async with _transaction_acting_for(engine, {_LOGIN_EMAIL_SETTING: email}) as connection:
        yield connection

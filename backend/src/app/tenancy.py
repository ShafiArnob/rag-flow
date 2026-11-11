"""The only place that sets the workspace a database transaction acts for."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

# is_local = true: the setting ends with the transaction, so it can never reach the next user of a
# pooled connection. A session-level setting would survive the commit.
_SET_WORKSPACE = text("SELECT set_config('app.workspace_id', :workspace_id, true)")


@asynccontextmanager
async def workspace_transaction(
    engine: AsyncEngine, workspace_id: uuid.UUID
) -> AsyncIterator[AsyncConnection]:
    """Yields a connection in a new transaction whose row-level security policies see only
    ``workspace_id``. The transaction commits when the block exits normally and rolls back if it
    raises.

    Never commit inside the block: the workspace setting ends with the transaction, so later
    statements would see no rows and every write would be rejected. Never swallow a database error
    inside it either: the transaction is already aborted, and catching the error does not undo that.
    """
    async with engine.begin() as connection:
        # asyncpg sends the parameter as text, so it must be a str, not a UUID.
        await connection.execute(_SET_WORKSPACE, {"workspace_id": str(workspace_id)})
        yield connection

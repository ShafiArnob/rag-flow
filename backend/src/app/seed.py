"""Seeds a demo user and workspace. Safe to run repeatedly.

The demo user has no password until P01_S04 adds authentication.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import Settings

DEMO_WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
DEMO_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
DEMO_USER_EMAIL = "demo@flowbuilder.local"
DEMO_WORKSPACE_NAME = "Demo Workspace"
OWNER_ROLE = "owner"


async def seed_demo_data(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO workspaces (id, name) VALUES (:id, :name) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": DEMO_WORKSPACE_ID, "name": DEMO_WORKSPACE_NAME},
        )
        await connection.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email) ON CONFLICT (id) DO NOTHING"),
            {"id": DEMO_USER_ID, "email": DEMO_USER_EMAIL},
        )
        await connection.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (:workspace_id, :user_id, :role) ON CONFLICT DO NOTHING"
            ),
            {"workspace_id": DEMO_WORKSPACE_ID, "user_id": DEMO_USER_ID, "role": OWNER_ROLE},
        )


async def main() -> None:
    engine = create_async_engine(Settings().database_url)  # type: ignore[call-arg]
    try:
        await seed_demo_data(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

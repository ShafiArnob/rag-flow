"""Seeds a demo user and workspace. Safe to run repeatedly.

The demo user deliberately has no password, so nobody can log in as it; people sign up instead.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.database import create_database_engine
from app.database_roles import assert_role_is_subject_to_row_level_security
from app.tenancy import member_transaction

DEMO_WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
DEMO_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
DEMO_USER_EMAIL = "demo@flowbuilder.local"
DEMO_WORKSPACE_NAME = "Demo Workspace"
OWNER_MEMBER_ROLE = "owner"


async def seed_demo_data(engine: AsyncEngine) -> None:
    await assert_role_is_subject_to_row_level_security(engine)
    # Acting for the demo user as well as the workspace: ON CONFLICT (id) makes Postgres apply the
    # SELECT policy of users to the proposed row, which only that user's own transaction passes.
    async with member_transaction(engine, DEMO_WORKSPACE_ID, DEMO_USER_ID) as connection:
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
            {
                "workspace_id": DEMO_WORKSPACE_ID,
                "user_id": DEMO_USER_ID,
                "role": OWNER_MEMBER_ROLE,
            },
        )


async def main() -> None:
    engine = create_database_engine(Settings().database_url)  # type: ignore[call-arg]
    try:
        await seed_demo_data(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Creates the monthly partitions of runs and run_steps up to 12 months ahead.

Run as the schema owner after migrations, on every deploy: ``python -m app.run_partitions``. Each
run moves the horizon forward to the current UTC month plus 12 and leaves existing partitions alone.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import MigrationSettings
from app.database import create_database_engine

_CREATE_RUN_PARTITIONS = text("SELECT create_run_partitions()")


async def create_run_partitions(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(_CREATE_RUN_PARTITIONS)


async def main() -> None:
    settings = MigrationSettings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.migration_database_url, row_security=False)
    try:
        await create_run_partitions(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

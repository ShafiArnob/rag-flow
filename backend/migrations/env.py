import asyncio

from alembic import context
from sqlalchemy import Connection

from app.config import MigrationSettings
from app.database.engine import create_database_engine


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = MigrationSettings()  # type: ignore[call-arg]
    engine = create_database_engine(settings.migration_database_url, row_security=False)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())

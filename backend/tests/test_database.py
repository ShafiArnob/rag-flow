import os

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.database import create_database_engine

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("row_security", "expected"), [(True, "on"), (False, "off")])
async def test_engine_sets_row_security_for_every_connection(
    row_security: bool, expected: str
) -> None:
    engine = create_database_engine(
        SecretStr(os.environ["MIGRATION_DATABASE_URL"]), row_security=row_security
    )
    try:
        async with engine.connect() as connection:
            setting = (await connection.execute(text("SHOW row_security"))).scalar_one()
    finally:
        await engine.dispose()

    assert setting == expected

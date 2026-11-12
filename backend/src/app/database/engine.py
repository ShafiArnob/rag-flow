from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_database_engine(
    url: SecretStr, *, row_security: bool = True, pool_size: int | None = None
) -> AsyncEngine:
    """Builds every engine in the service, so all of them keep bound values out of errors.

    Pass ``row_security=False`` only for connections that run as the schema owner (Alembic and
    partition maintenance). Tables use FORCE ROW LEVEL SECURITY, so without it an owner statement
    that RLS would filter silently touches zero rows; with it, that statement fails loudly.

    ``pool_size`` defaults to SQLAlchemy's. Tests set it to 1 to make every checkout reuse one
    connection.
    """
    connect_args = {} if row_security else {"server_settings": {"row_security": "off"}}
    pool_options: dict[str, Any] = {} if pool_size is None else {"pool_size": pool_size}
    return create_async_engine(
        url.get_secret_value(), hide_parameters=True, connect_args=connect_args, **pool_options
    )

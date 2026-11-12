"""Addresses no test environment can reach, for apps whose tests never use the database or Redis.
Engines and Redis clients connect only when first used, so building them from these is free."""

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://nobody@unreachable.invalid/unused"
UNREACHABLE_REDIS_URL = "redis://unreachable.invalid:6379/0"

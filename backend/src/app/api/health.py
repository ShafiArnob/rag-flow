import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx
from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# A readiness check returns normally when its dependency is usable and raises otherwise.
ReadinessCheck = Callable[[], Awaitable[None]]


def postgres_check(engine: AsyncEngine) -> ReadinessCheck:
    async def check() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return check


def redis_check(client: Redis) -> ReadinessCheck:
    async def check() -> None:
        await client.ping()

    return check


def object_storage_check(health_url: str, client: httpx.AsyncClient) -> ReadinessCheck:
    async def check() -> None:
        response = await client.get(health_url)
        response.raise_for_status()

    return check


def build_health_router(readiness_checks: Mapping[str, ReadinessCheck]) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readiness(response: Response) -> dict[str, str]:
        outcomes = await asyncio.gather(
            *(check() for check in readiness_checks.values()), return_exceptions=True
        )
        results: dict[str, str] = {}
        for name, outcome in zip(readiness_checks, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error("readiness check failed", extra={"check": name}, exc_info=outcome)
                results[name] = "unavailable"
            else:
                results[name] = "ok"
        if "unavailable" in results.values():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return results

    return router

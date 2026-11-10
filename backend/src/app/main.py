import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.health import (
    ReadinessCheck,
    build_health_router,
    object_storage_check,
    postgres_check,
    redis_check,
)
from app.logging_config import configure_logging
from app.telemetry import build_tracer_provider

logger = logging.getLogger("app.access")

READINESS_PROBE_TIMEOUT_SECONDS = 2.0


def create_app(
    tracer_provider: TracerProvider,
    readiness_checks: Mapping[str, ReadinessCheck],
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="FlowBuilder API", lifespan=lifespan)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

    @app.middleware("http")
    async def log_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    app.include_router(build_health_router(readiness_checks))
    return app


def create_app_from_environment() -> FastAPI:
    """Uvicorn factory: wires the app to the services named in the environment."""
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)

    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(READINESS_PROBE_TIMEOUT_SECONDS))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await http_client.aclose()
        await redis.aclose()
        await engine.dispose()

    readiness_checks = {
        "postgres": postgres_check(engine),
        "redis": redis_check(redis),
        "object_storage": object_storage_check(settings.object_storage_health_url, http_client),
    }
    return create_app(build_tracer_provider(settings), readiness_checks, lifespan)

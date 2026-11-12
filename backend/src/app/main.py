import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.auth import build_auth_router
from app.authentication import build_session_dependency
from app.config import Settings
from app.database import create_database_engine
from app.database_roles import assert_role_is_subject_to_row_level_security
from app.health import (
    ReadinessCheck,
    build_health_router,
    object_storage_check,
    postgres_check,
    redis_check,
)
from app.logging_config import configure_logging
from app.login_rate_limit import LoginRateLimiter
from app.passwords import PasswordHasher
from app.telemetry import build_tracer_provider

logger = logging.getLogger("app.access")
startup_logger = logging.getLogger(__name__)

READINESS_PROBE_TIMEOUT_SECONDS = 2.0
# Login waits on Redis for every attempt, so a hung Redis must fail it instead of holding it open.
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0


def create_app(
    tracer_provider: TracerProvider,
    readiness_checks: Mapping[str, ReadinessCheck],
    engine: AsyncEngine,
    redis: Redis,
    session_cookie_secure: bool,
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

    @app.exception_handler(RequestValidationError)
    async def respond_without_input(_: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default body echoes each error's input, which for signup is the password.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": [
                    {"type": detail["type"], "loc": detail["loc"], "msg": detail["msg"]}
                    for detail in error.errors()
                ]
            },
        )

    authenticated_session = build_session_dependency(
        engine, session_cookie_secure=session_cookie_secure
    )
    app.include_router(build_health_router(readiness_checks))
    app.include_router(
        build_auth_router(
            engine,
            PasswordHasher(),
            LoginRateLimiter(redis),
            authenticated_session,
            session_cookie_secure=session_cookie_secure,
        )
    )
    return app


def create_app_from_environment() -> FastAPI:
    """Uvicorn factory: wires the app to the services named in the environment."""
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    if not settings.session_cookie_secure:
        startup_logger.warning(
            "the session cookie is sent without the Secure attribute, so browsers also send it"
            " over plain HTTP; set SESSION_COOKIE_SECURE=true everywhere but local development"
        )

    engine = create_database_engine(settings.database_url)
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(READINESS_PROBE_TIMEOUT_SECONDS))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await assert_role_is_subject_to_row_level_security(engine)
            yield
        finally:
            await http_client.aclose()
            await redis.aclose()
            await engine.dispose()

    readiness_checks = {
        "postgres": postgres_check(engine),
        "redis": redis_check(redis),
        "object_storage": object_storage_check(settings.object_storage_health_url, http_client),
    }
    return create_app(
        build_tracer_provider(settings),
        readiness_checks,
        engine,
        redis,
        settings.session_cookie_secure,
        lifespan,
    )

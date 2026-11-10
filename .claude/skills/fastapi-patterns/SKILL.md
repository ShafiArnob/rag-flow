---
name: fastapi-patterns
description: How FastAPI code is written in this repo (backend/src/app) — app factory, explicit dependencies, routers, settings, logging, and async tests. Use when adding or changing an API endpoint, router, middleware, or backend configuration.
---

# FastAPI patterns for this repo

Read `backend/src/app/main.py` and `backend/src/app/health.py` before writing anything; they are the reference implementation of every rule below.

## App factory, explicit dependencies

- `create_app(...)` takes everything it needs as arguments (tracer provider, readiness checks, lifespan). It never reads the environment.
- `create_app_from_environment()` is the only place that reads `Settings()` and builds real clients (engine, Redis, httpx). It is the uvicorn factory.
- Build routers with a function that receives their dependencies, like `build_health_router(readiness_checks)`. Don't reach for module-level clients or globals.
- Dependencies are plain callables or small closures (see `postgres_check(engine)`), not classes, unless there's state with real invariants.

## Endpoints

- Request and response bodies are Pydantic v2 models with precise types (`Literal`, enums, `uuid.UUID`, constrained strings). Validate at the boundary; trust inside.
- Return typed models or typed dicts; set status codes with `fastapi.status` constants.
- Raise `HTTPException` only at the route layer. Core logic raises domain exceptions with context (what, which input, why).
- Never build SQL with string concatenation. Use `sqlalchemy.text(...)` with bound parameters or Core/ORM constructs.

## Settings

- All configuration lives in `app.config.Settings` (pydantic-settings). A new setting needs a matching entry in `.env.example`.
- No defaults that hide a missing value. Required settings have no default.

## Logging and tracing

- Use `logging.getLogger(__name__)` and pass structured fields through `extra={...}`.
- Never log secrets or raw request bodies. The `RedactionFilter` in `app.logging_config` is a safety net, not permission.
- OpenTelemetry is wired in by `FastAPIInstrumentor`; don't add manual spans unless a slow internal step needs one.

## Tests

- `pytest` with `asyncio_mode = "auto"`: write `async def test_...` with no `@pytest.mark.asyncio`.
- Drive the app through `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")`.
- Build the app with `create_app(...)` and pass fakes for external dependencies (see `healthy`/`broken` in `tests/test_health.py`). Don't mock internal functions.
- Tests that need Postgres are marked `@pytest.mark.integration` and read `DATABASE_URL`.
- Name tests after the behaviour: `test_readyz_returns_503_and_names_the_failing_dependency`.

## Done means

From `backend/`: `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest`. mypy is strict; don't add `# type: ignore` without a reason in the same line.

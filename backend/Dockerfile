FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 flowbuilder

# Development image for infra/docker-compose.dev.yml. Compose Watch copies edits into these folders as
# the container user, so that user must own them.
FROM base AS dev
RUN chown -R flowbuilder /app/src /app/migrations
USER flowbuilder

# Production image. Keep this stage last so a build without a target produces it.
FROM base AS production
USER flowbuilder
# --no-proxy-headers: uvicorn otherwise takes the client address from X-Forwarded-For, which any
# client can set, and the audit log records that address. tests/test_main.py pins it.
CMD ["uvicorn", "--factory", "app.main:create_app_from_environment", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

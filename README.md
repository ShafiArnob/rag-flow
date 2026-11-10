# FlowBuilder

Monorepo layout:

| Folder | Contents |
| --- | --- |
| `backend/` | FastAPI API and Celery worker (Python 3.12, uv) |
| `frontend/` | Next.js App Router app (pnpm) |
| `infra/` | Docker Compose stack and OpenTelemetry collector config |
| `docs/` | Design notes |

## Run the stack

```sh
cp .env.example .env        # then change the passwords
docker compose --env-file .env -f infra/docker-compose.yml up --build --wait
curl localhost:8000/readyz
```

The frontend is at <http://localhost:3000> and the API at <http://localhost:8000>.
`--wait` returns once every service reports healthy. The api applies Alembic migrations on start.
To load the demo user and workspace:

```sh
docker compose --env-file .env -f infra/docker-compose.yml exec api python -m app.seed
```

## Start, stop and rebuild

`stop` keeps the containers, so you can spin the stack down and up from Docker Desktop (the Start and
Stop buttons on the `flowbuilder` group) or from the CLI:

```sh
docker compose --env-file .env -f infra/docker-compose.yml stop            # spin down
docker compose --env-file .env -f infra/docker-compose.yml start           # spin up
docker compose --env-file .env -f infra/docker-compose.yml up --build -d   # after a code change
```

Code is baked into the images, so a plain `up` or `start` runs the old code; use `up --build -d` after
changing it. `down` (and Docker Desktop's delete button) removes the containers, and `up` creates them
again. The database and object storage live in named volumes that survive both; only `down -v` deletes them.

## Live reload

For development, run the dev overlay with Compose Watch and keep the terminal open:

```sh
docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml watch
```

It builds and starts the stack, then copies every edit into the running containers until you press
Ctrl+C. Edits under `frontend/src` and `backend/src` show up within about two seconds (`next dev` and
`uvicorn --reload`; the worker restarts). Migrations restart the api, which applies them, and changes to
dependencies or `next.config.ts` rebuild the affected image. Docker Desktop's Start and Stop buttons
still work, but edits only sync while `watch` is running; stopping `watch` leaves the containers up,
and the next `watch` catches up on anything changed in between. Dev images are tagged `*-dev`, so
they never replace the production images.
Running the stack without the overlay runs the production images, as CI does.

## Checks

```sh
cd backend && uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
cd frontend && pnpm install && pnpm lint && pnpm typecheck && pnpm test
```

`tests/test_migrations_and_seed.py` (marker `integration`) migrates the database in `DATABASE_URL` down
to empty and back up. Point it at a disposable database, never at one holding data you want to keep.
Run `uv run pytest -m "not integration"` when no database is available.

## Observability

Every request is traced with OpenTelemetry and sent to the collector (`OTEL_EXPORTER_OTLP_ENDPOINT`).
Logs are one JSON object per line on stdout, carry the request's `trace_id`, and have bearer tokens and
values of keys such as `password`, `secret`, `token` and `api_key` replaced with `[REDACTED]`.

Install the Git hooks with `pre-commit install`.

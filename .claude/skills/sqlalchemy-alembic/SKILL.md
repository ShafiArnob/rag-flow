---
name: sqlalchemy-alembic
description: Database access and schema migrations in this repo — async SQLAlchemy 2.0 on Postgres (asyncpg, pgvector, citext) and hand-written Alembic migrations in backend/migrations. Use when adding or changing tables, columns, indexes, queries, or seed data.
---

# SQLAlchemy and Alembic in this repo

Schema changes are a public contract: per CLAUDE.md, plan them and get approval before writing code.

## Migrations

- Location: `backend/migrations/versions/`. Revision IDs are sequential four-digit strings: `"0001"`, `"0002"`, … File name `NNNN_short_description.py`.
- Migrations are hand-written SQL through `op.execute(...)`, like `0001_baseline.py`. Don't use autogenerate.
- The module docstring says why the change exists and which story it belongs to.
- Every `upgrade()` has a `downgrade()` that fully reverses it, in reverse order.
- Never edit a migration that has been merged to `main`. Add a new one.
- Postgres features in use: `uuid` primary keys with `gen_random_uuid()`, `timestamptz` with `DEFAULT now()`, `citext` for emails, the `vector` extension for embeddings. Row-level security is planned for workspace isolation; new tenant-owned tables carry `workspace_id uuid NOT NULL REFERENCES workspaces (id)`.

### Changing tables that hold data

- Adding a required column: add it nullable, backfill, then `SET NOT NULL` — in separate migrations if the table can be large.
- Renaming or retyping a column: expand (add new), migrate readers and writers, contract (drop old) in a later migration.
- Creating indexes on large tables: `CREATE INDEX CONCURRENTLY`, which can't run inside a transaction block.

## Queries

- Async engine from `sqlalchemy.ext.asyncio.create_async_engine(settings.database_url)`. Pass the engine (or a session) into functions explicitly; no module-level engine.
- Use `text("... :name ...")` with a parameter dict, or SQLAlchemy Core/ORM constructs. Never interpolate values into SQL. The only f-string SQL allowed is a fixed identifier in test code, marked `# noqa: S608`.
- Writes that must be idempotent use `INSERT ... ON CONFLICT ... DO NOTHING / DO UPDATE` (see `app/seed.py`).
- Use `engine.begin()` for a unit of work that commits; `engine.connect()` for reads.

## Tests

- Migration behaviour is covered by `tests/test_migrations_and_seed.py`: downgrade to base, upgrade to head, check the result, then round-trip again. Extend that test, or add one like it, for every new migration.
- These tests are `@pytest.mark.integration` and need Postgres from `infra/docker-compose.yml` with `DATABASE_URL` set.

## Done means

From `backend/`: `uv run alembic upgrade head`, `uv run alembic downgrade base`, `uv run alembic upgrade head` all succeed, then `uv run pytest -m integration` and the normal lint/type checks pass.

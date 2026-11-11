---
name: verify
description: Step 4 of the FlowBuilder workflow. Runs the backend and frontend checks, the Docker stack, and the Postman collection, then reports what was and was not verified. Use after implementing a change or before committing.
---

# Verify the change

Extra instructions from the user: $ARGUMENTS

Run each check that applies to the services the change touched, and report the actual output, including failures. Never weaken or skip a check to get a green run.

```sh
# backend
cd backend && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest

# frontend
cd frontend && pnpm lint && pnpm typecheck && pnpm test
```

`backend/tests/test_migrations_and_seed.py` (marker `integration`) migrates the database in `DATABASE_URL` down to empty and back up. Run it only against a disposable database. Otherwise run `uv run pytest -m "not integration"` and report the integration tests as not run.

If the API, a migration, a Dockerfile, or `infra/` changed, run the stack and the Postman collection against it, from the repo root:

```sh
docker compose --env-file .env -f infra/docker-compose.yml up --build --wait
postman collection lint "postman/collections/FlowBuilder API"
postman collection run "postman/collections/FlowBuilder API" -e postman/environments/local.environment.yaml
```

## Report

- Each check: passed or failed, with the failing output.
- Each acceptance check from the plan: verified, and how, or not verified, and why.
- What was verified against the live stack, what only against a mock, and what was not verified at all.

If anything failed, find the root cause and fix it, then run the checks again. The next step is `/review-change`.

---
name: celery-tasks
description: Background work in this repo with Celery on Redis (backend/src/app/worker.py) — when to use a task, idempotency, retries, timeouts, and testing. Use when adding or changing a Celery task or moving slow work (ingestion, embedding, external calls) out of a request.
---

# Celery tasks in this repo

The Celery app is `celery_app` in `backend/src/app/worker.py`, using Redis as broker and result backend.

## When to use a task

- Work that can take more than a request should wait (document parsing, embedding, bulk writes) or that calls an unreliable external service.
- The endpoint validates input, records a job row, enqueues the task, and returns the job id immediately. Clients poll a status endpoint.
- Don't use a task just to avoid writing async code.

## Writing a task

- Keep the task body thin: load inputs, call a plain function that does the work, record the outcome. The plain function holds the logic and is what unit tests exercise.
- Arguments are small JSON-serialisable values — ids, not objects or file contents. Store large payloads in Postgres or object storage and pass the key.
- Tasks run at least once. Make them idempotent: check state before acting, use `ON CONFLICT` upserts, and pass idempotency keys to external services.
- Retry only transient failures, with backoff: `autoretry_for=(ConnectionError, TimeoutError)`, `retry_backoff=True`, a bounded `max_retries`. Permanent failures (bad input, missing record) fail the job with a clear error and are not retried.
- Set `soft_time_limit` and `time_limit` on tasks that touch external services or large files.
- Record job state transitions (`pending → running → succeeded | failed`) in the database, with the failure reason. Don't rely on the Celery result backend as the source of truth for job status.
- Log with `logging.getLogger(__name__)` and include the job id in `extra`. Never log document contents or secrets.

## Configuration

- Worker settings come from `app.config.Settings`. Any new setting goes in `.env.example`.
- Prefer `task_acks_late=True` with `worker_prefetch_multiplier=1` for long tasks so a crashed worker's job is redelivered. Change global config in `worker.py` only, and say why in the diff.

## Tests

- Unit-test the plain function the task calls; that's where the behaviour is.
- To test the task wiring, call `task.apply(args=...)` (runs eagerly in-process) rather than starting a worker.
- Fake only external boundaries (HTTP, object storage, model providers). Use real Postgres for state transitions under `@pytest.mark.integration`.

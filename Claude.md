# Engineering Principles

How to write code in this repository. The goal is code that is correct, simple, and easy to change. When these goals conflict, choose in that order.

## Before writing code

- Read the surrounding code first, and match its patterns, naming, and structure.
- Search for an existing helper before writing a new one.
- If a requirement is ambiguous in a way that changes the design, ask. Otherwise take the simplest reading and state the assumption.
- For bugs, find the root cause before changing anything. Fix the cause, not the symptom.

## Keep it simple

- Write the simplest code that solves the current problem. Add no features, options, configuration, or extension points that nobody asked for.
- Prefer a little duplication over the wrong abstraction. Extract on the third real use, once the shared shape is clear.
- Prefer plain functions and data. Use a class when it protects state with real invariants.
- Add a dependency only when it removes more complexity than it brings.
- Delete dead code. Leave no commented-out code, unused parameters, or shims for callers that no longer exist.
- Keep changes small and on task. Put unrelated refactors in a separate change.

## Design

- Give each function and module one responsibility. If describing it takes the word "and", split it.
- Keep logic separate from I/O: a pure core with thin edges that touch the network, disk, or database.
- Add interfaces only at real boundaries, such as external services and storage. An interface with one implementation and no testing need is noise.
- Prefer composition over inheritance.
- Make invalid states unrepresentable: use precise types and enums, and validate when constructing objects.
- Pass dependencies explicitly. Avoid global mutable state.
- Keep one source of truth for every fact, constant, and setting.

## Frontend

- Build the UI from shadcn/ui components everywhere. Write a custom component only when shadcn/ui has none that fits.

## Backend layout

This is the layout for `backend/src/app/`. Packages marked with a subphase do not exist yet; create each when that subphase starts.

```
backend/src/app/
├── main.py              # app factory: wires routers and dependencies
├── config.py
├── worker.py            # Celery app only; tasks live in their feature package
├── api/                 # HTTP edge: routers and request/response schemas, no business logic
│   ├── health.py
│   ├── auth.py
│   ├── connectors.py
│   ├── workflows.py
│   └── runs.py
├── observability/       # logging_config.py, telemetry.py, redaction.py
├── database/            # engine.py, bootstrap.py, roles.py, tenancy.py, seed.py
├── identity/            # passwords.py, sessions.py, authentication.py, login_rate_limit.py, service.py
├── audit.py
├── crypto/              # P01_S05: envelope.py, key_provider.py
├── egress/              # P01_S06: guarded HTTP client and allowlist policy
├── connectors/          # P01_S07: registry.py, repository.py, service.py
├── nodes/               # P01_S08: contract.py, registry.py, builtin/
├── workflows/           # P01_S11: graph.py, compiler.py, repository.py
└── runs/                # P01_S11: runner.py, partitions.py, tasks.py
```

- Group code by feature. Each package owns one feature's logic, data access, and Celery tasks.
- Keep `api/` thin. A router validates the request, calls the feature package, and shapes the response.
- Create a package when its feature's work starts. Don't create empty placeholder packages.
- Don't create `utils/`, `helpers/`, or `common/` packages. Put a helper in the package that uses it, or in a module named for what it does.
- Keep a feature in one module until it outgrows it, then turn it into a package.
- In `backend/tests/`, put shared fixtures and helpers in `tests/support/`. Mirror the source packages as features grow, for example `tests/identity/`.

## Readability

- Use names that say what a thing is or does, without abbreviations. Booleans read as predicates: `is_ready`, `has_access`.
- Return early and keep nesting shallow.
- Replace magic values with named constants.
- Write comments to explain why, never what. If code needs a comment to explain what it does, rewrite the code.

## Errors

- Validate at system boundaries: user input, files, and external responses. Trust internal code after that.
- Fail early and loudly. Never swallow an exception, and catch only what you can actually handle.
- Include context in errors: what failed, on which input, and why.
- Don't use silent fallbacks or default values that hide a failure.
- Don't add defensive checks for states that cannot occur.

## Security

- Read secrets from the environment. Never commit or log them.
- Treat all external data as untrusted, including uploaded files, third-party responses, and model output.
- Use parameterized queries. Never build queries or shell commands by concatenating strings.

## Testing

- Test behavior through the public interface, not implementation details.
- Start every bug fix with a failing test that reproduces the bug.
- Mock only at external boundaries. Keep tests deterministic and independent of each other.
- Never weaken, skip, or delete a test to get a passing run. Fix the code, or explain why the test is wrong.

## API testing with Postman

- Keep one Postman collection for the HTTP API, in `postman/collections/FlowBuilder API/`, committed alongside the code.
- Organize the collection into one folder per resource, matching the API's route groups, such as Health, Auth, Workflows, and Runs.
- When an endpoint is added or changed, add or update its request in the same change.
- Give every request tests that check the status code, the response shape, and the fields callers rely on. Cover the error cases, such as 401, 404, and 422, as well as the success case.
- Put the base URL and resource IDs in environment variables, in `postman/environments/`. Have one request's test script store the IDs that later requests need.
- Keep credentials in Postman secret variables. Never put them in committed collection or environment files.
- After changing the API, run the collection against the running server with the Postman CLI and report the results.
- Say whether each result came from a live server or a mock. An endpoint that has only run against a mock has not been tested.

## Before calling it done

- Run the tests, linter, and type checker, plus the Postman collection when the API changed, and report any failures as they are.
- Say what was verified and what was not. Don't call something working unless you ran it.
- Reread the diff and remove anything the task did not need.

# Workflow

Work starts from a requirement, pasted or attached, never a ticket. Each step is a slash command:

1. `/explore <requirement>` maps it onto the code. No code is written.
2. `/plan-change` writes the plan and waits for approval.
3. `/implement` builds the approved plan with its tests.
4. `/verify` runs every check, the Docker stack, and the Postman collection.
5. `/review-change` reviews the diff. Fix only the findings the user picks.
6. `/ship` commits, pushes, and opens the PR. Only when the user asks.

A small task (one service, fewer than three steps, no schema or API contract change) goes straight to `/implement` and `/verify`. Build-plan progress is recorded in `tasks/progress.md`.

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps, architectural decisions, or
  changes that touch more than one of `backend/`, `frontend/`, `infra/`).
- Use plan mode for verification strategy, not just building.
- Write detailed specs upfront to reduce ambiguity. Prefer approval before coding when
  the plan spans services or changes the database schema (SQLAlchemy models / Alembic
  migrations) or public API contracts (FastAPI routes and their request/response schemas).

### 2. Self-Improvement Loop

- After ANY correction from the user: update `tasks/lessons.md` with the pattern.
- Write rules for yourself that prevent the same mistake.
- Ruthlessly iterate on these lessons until the mistake rate drops.
- Review `tasks/lessons.md` at session start for this project.

### 3. Skills and subagents

- Load the domain skill before writing code in its area: `fastapi-patterns`, `sqlalchemy-alembic`, `celery-tasks`, `nextjs-shadcn`.
- `/implement` lists the agents and skills to use for each shape of change.
- Subagents cost tokens. For single-service work, use the main agent with the domain skill, or one engineer agent. Give each subagent one task.
- Multi-service design starts with `system-architect`. Review uses `code-reviewer`, plus `security-engineer` for auth, data access, file handling, LLM calls, or infra.
- `documentation-engineer` brings docs back in line with code. `ui-ux-engineer` reviews first and implements only the findings the user approves.

### 4. Git branches

- Never create, switch, or delete git branches yourself. Work on the branch that is
  already checked out; the user manages branches.
- If a task seems to need a new branch, ask the user to create it instead.

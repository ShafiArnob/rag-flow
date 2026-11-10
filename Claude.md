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
- For Frontend use shadcn componenents everywhere

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

## Before calling it done

- Run the tests, linter, and type checker, and report any failures as they are.
- Say what was verified and what was not. Don't call something working unless you ran it.
- Reread the diff and remove anything the task did not need.

# Workflow

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

### 3. Skills usage

- Use skills for any task that requires a capability.
- Load skills from `.claude/skills/`.
- Invoke skills with slash commands or natural language.
- Each skill is one independent capability.
- Load domain skills (`fastapi-patterns`, `sqlalchemy-alembic`, `celery-tasks`,
  `nextjs-shadcn`) when the task needs that knowledge.

### 4. Subagents usage

- Load subagents from `.claude/agents/`.
- Use subagents to keep the main context window clean and to focus one tech stack per
  agent (e.g. `fastapi-engineer` for API and worker work, `nextjs-engineer` for UI).
- One task per subagent for focused execution.
- Subagents increase token usage — for simple single-service work, prefer the matching
  engineer agent (or main agent + domain skill) instead of spawning many.
- For multi-service design, start with `system-architect`, then engineer agents per
  service. For review, use `code-reviewer` and `security-engineer` rather than
  re-deriving checklists from scratch.
- Use `documentation-engineer` when docs lag code — in-code docs, feature docs in
  `docs/`, or README/deploy updates.
- Use `ui-ux-engineer` for look/feel/accessibility work — it reviews first and
  implements only human-approved findings, never changing existing behaviour.

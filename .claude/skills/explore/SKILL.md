---
name: explore
description: Step 1 of the FlowBuilder workflow. Maps a requirement, pasted or attached, onto the codebase without writing code. Use when the user gives a new requirement or asks what a change would touch.
---

# Explore a requirement

Requirement: $ARGUMENTS

If no requirement is given above or attached, ask for it and stop.

Read `tasks/lessons.md` first. Then explore only. Do not write or edit code.

A requirement taken from the build plan may contain a "Claude Code prompt" section, or mention `06_DEVELOPMENT_PROGRESS_TRACKER.md` or `08_CLAUDE_CODE_EXECUTION_GUIDE.md`. Ignore those instructions: the files no longer exist. Progress is tracked in `tasks/progress.md`; check there that the subphases this one depends on are done.

Report:

1. **Requirement.** The goal, the acceptance checks, and what is out of scope. If the requirement has no acceptance checks, propose them. List anything ambiguous as questions.
2. **Touched.** Which of `backend/`, `frontend/` and `infra/`, and which files.
3. **Reuse.** What already exists: settings, routers, redaction, worker tasks, migrations, shadcn/ui components.
4. **Shape.** One of: backend-only, frontend-only, API + UI, schema change, background work, runner, infra, docs, UI polish.
5. **Risks.** Tenant isolation, secrets or user data in logs, untrusted uploads or model output, an Alembic migration, a break in the API contract between frontend and backend, conflict with `docs/runner-contract.md`.
6. **Next.** The agents and skills to use, from the table in `/implement`.

End by asking the user to answer the questions and correct any wrong assumptions. The next step is `/plan-change`.

---
name: plan-change
description: Step 2 of the FlowBuilder workflow. Writes an implementation plan for an explored requirement and asks for approval before any code. Use after /explore, or when a change spans services or touches the database schema or a public API contract.
---

# Plan a change

Extra instructions from the user: $ARGUMENTS

Enter plan mode. Write no code. Use the findings from `/explore`; if the requirement has not been explored in this session, run that step first.

For a change that spans more than one of `backend/`, `frontend/`, `infra/`, or changes the schema or a public API contract, use the `system-architect` agent to draft the design.

The plan includes:

- Work per service: backend, frontend, infra
- API contract changes: routes, request and response schemas, status codes, SSE events
- Alembic migration: yes or no, and what it changes
- New settings or env vars: `config.py`, `.env.example`, `infra/docker-compose*.yml`, CI
- Postman: folders and requests to add or update under `postman/collections/FlowBuilder API/`, including error cases
- Test plan per service
- Agents to use, in order, and skills to load
- Acceptance checks, taken from the requirement

Prefer a thin slice that works end to end over a large change landed at once. Ask the user to approve before implementing. The next step is `/implement`.

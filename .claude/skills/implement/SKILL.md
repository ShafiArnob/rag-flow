---
name: implement
description: Step 3 of the FlowBuilder workflow. Implements an approved plan with its tests, using the right engineer agents and domain skills for the shape of the change. Use after the user approves a plan from /plan-change.
---

# Implement the approved plan

Extra instructions from the user: $ARGUMENTS

If no plan has been approved in this session, say so and stop, unless the task is small: one service, fewer than three steps, and no schema or API contract change.

## Rules

- Work on the branch that is checked out.
- Reuse before inventing. Load the skills the plan names before writing code.
- Ship tests with the change, including error cases.
- Update the Postman requests in the same change as their endpoints.
- No secrets, tokens, or user data in logs.
- Multi-service: one engineer agent per service, backend before frontend, so the frontend builds against a real contract.
- If the plan turns out to be wrong, stop and tell the user. Don't quietly change scope.
- Do not commit. The user asks for that with `/ship`.

## Agents and skills by shape

| Shape | Agents, in order | Skills |
| --- | --- | --- |
| Backend-only | `fastapi-engineer` | `fastapi-patterns` |
| Schema change | `system-architect`, then `fastapi-engineer` | `sqlalchemy-alembic`, `fastapi-patterns` |
| Background work | `fastapi-engineer` | `celery-tasks` |
| Frontend-only | `nextjs-engineer` | `nextjs-shadcn` |
| API + UI | `system-architect`, then `fastapi-engineer`, then `nextjs-engineer` | `fastapi-patterns`, `nextjs-shadcn` |
| Runner or graph execution | `system-architect`, then `fastapi-engineer`. Must honour `docs/runner-contract.md` | `fastapi-patterns`, `celery-tasks` |
| Infra (`infra/`, Dockerfiles, CI) | Main agent | None |
| Docs | `documentation-engineer` | None |
| UI polish | `ui-ux-engineer`: it reviews, the user approves, then it implements | `nextjs-shadcn` |

For single-service work, the main agent with the domain skill is enough; an engineer agent is optional.

When the code is written, the next step is `/verify`.

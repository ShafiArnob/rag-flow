---
name: ship
description: Last step of the FlowBuilder workflow. Commits the verified change, pushes the current branch, and opens a pull request to main. Only when the user asks to commit, push, or open a PR.
disable-model-invocation: true
---

# Ship the change

Extra instructions from the user: $ARGUMENTS

1. Run `git status` and `git diff`. Stage only the files that belong to this change. Never stage `.env` or anything holding a secret. If unrelated changes are present, list them and ask before including them.
2. If `/verify` has not passed since the last edit, run it first. Do not ship a red check.
3. Commit with a message in the repo's style: a `feat:`, `fix:`, `docs:`, `test:` or `chore:` prefix and a short summary.
4. If the change completes a build-plan subphase, update its row in `tasks/progress.md` in the same commit.
5. Push the current branch. Never create or switch branches; if the current branch is `main`, stop and tell the user.
6. Open a pull request to `main` with `gh pr create`. The description lists the requirement, what changed per service, any migration or new env vars, and how it was verified: live, mock, or not verified.

Report the commit hash and the PR link.

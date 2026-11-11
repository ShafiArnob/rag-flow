---
name: review-change
description: Step 5 of the FlowBuilder workflow. Reviews the current branch's diff against main with the code-reviewer agent, and the security-engineer agent when the change is security-relevant, then reports findings without fixing them. Use after /verify and before /ship.
---

# Review the change

Extra instructions from the user: $ARGUMENTS

Review the diff between `main` and the working tree, including uncommitted changes.

- Use the `code-reviewer` agent for correctness, CLAUDE.md adherence, and missing tests.
- Also use the `security-engineer` agent if the change touches auth, data access, tenant isolation, file handling, LLM calls, or infra.

Give each agent the diff and the requirement, not your own opinion of the change.

Report the findings as one numbered list ranked by severity, and say which agent raised each. Do not fix anything yet. Ask the user which findings to fix.

After fixing the findings the user picks, run `/verify` again. If docs now lag the code, use the `documentation-engineer` agent and show the user the doc changes. The next step is `/ship`.

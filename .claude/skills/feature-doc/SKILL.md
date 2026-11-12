---
name: feature-doc
description: Step 6 of the FlowBuilder workflow. Writes or updates a short PDF doc for a finished feature in docs/feature/<feature-name>.pdf, with a diagram and a code map. Use automatically after /review-change on a feature whose tests and build pass, before /ship. Skip for bug fixes, refactors, and tiny tweaks unless the user asks.
---

# Document the feature

Feature and extra instructions from the user: $ARGUMENTS

If no feature is named, document the feature the current branch implements, from its diff against `main`.

## Output

- Save to `docs/feature/<feature-name>.pdf`, with the name in kebab-case. Create the folder if it is missing.
- If the PDF already exists, update it instead of making a new one.
- Use as many pages as the feature needs. Keep it scannable, with no filler.

## Contents, in this order

1. **Title and one-line summary**: feature name, today's date, and one sentence on its purpose.
2. **What it does**: 2-4 sentences or bullets in plain language, from the user's point of view.
3. **How it works**: the flow from request to logic to result, in 4-6 steps.
4. **Visual**: at least one diagram (flow, sequence, or architecture) showing how the pieces connect. Write it in Mermaid or Graphviz, render it to an image, and embed it. Include only the nodes that matter.
5. **Code map**: a compact table with the columns File path | Class / Function | Role, with one short line per role. List only the files and symbols this feature added or meaningfully changed.
6. **Notes**, only if useful: config and env vars, API endpoints, known limitations.

## Rules

- Accuracy over polish. Read the actual code and the diff, and use only real file names and symbol names.
- Don't paste large code blocks. Include at most one 5-10 line snippet, and only if it clarifies something.
- Use a clean, consistent layout: one font, clear headings, and a readable diagram.
- Generate every feature PDF with the same script, `scripts/make_feature_doc.*`, so the style stays consistent. Write the content as a source file next to the PDF (for example `docs/feature/<feature-name>.md`) and pass it to the script. If the script does not exist yet, create it.
- Before creating the script, check which tools are installed (pandoc, weasyprint, mermaid-cli, Graphviz, reportlab, Node, a headless Chrome or Edge). Choose what is already installed or cheapest to install. Don't add heavy dependencies.

## Verify

- Confirm the PDF exists and that the diagram rendered as an image, not as raw Mermaid or Graphviz text. Read the PDF to check.
- Finish with one line giving the PDF path. The next step is `/ship`.

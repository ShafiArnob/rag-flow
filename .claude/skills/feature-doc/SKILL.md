---
name: feature-doc
description: Step 6 of the FlowBuilder workflow. Writes or updates a PDF doc for a finished feature in docs/feature/<feature-name>.pdf, in two parts - a plain-language part for everyone and a technical part - each with visuals and worked examples, including failing ones. Use automatically after /review-change on a feature whose tests and build pass, before /ship. Skip for bug fixes, refactors, and tiny tweaks unless the user asks.
---

# Document the feature

Feature and extra instructions from the user: $ARGUMENTS

If no feature is named, document the feature the current branch implements, from its diff against `main`.

## Output

- Save to `docs/feature/<feature-name>.pdf`, with the name in kebab-case. Create the folder if it is missing.
- If the PDF already exists, update it instead of making a new one.
- Use as many pages as the feature needs. Keep it scannable, with no filler.

## Contents, in this order

The doc has a title and two parts. Part 1 is for anyone, such as a product owner, a support person or a customer. Part 2 is for engineers. Each part opens with a `part` banner, and Part 2 starts on a new page.

**Title**: the feature name, then a lead line `**<subphase>** · <today's date> · <one sentence on its purpose>`. When the feature spans built and unbuilt parts, follow it with a `cards` row (for example Built / Not used yet / Later).

**Part 1: the short version** (`Part 1 | The short version | For everyone. No technical background needed.`)

1. **What it is**: 1-3 sentences. An everyday comparison helps when it is accurate (a receptionist, a lock, a filing cabinet).
2. **Why it matters**: the problem it solves, as a `compare` block (without it | with it), 3-5 rows.
3. **How it works, in plain words**: one visual, from the Part 1 list below, with a sentence or two around it.
4. **Examples**: an `examples` block with at least two that work and two that fail. Describe each as the person using FlowBuilder sees it, and for a failure, say what they see and why.
5. **What it does not do yet**: limits and what comes later, in 2-4 bullets.

**Part 2: technical** (`Part 2 | How it works | For engineers working on FlowBuilder.`)

1. **How it works**: the flow from request to logic to result, in 4-6 steps or short subsections. Put each figure right after the text it explains.
2. **Examples**: an `examples` block with real inputs (a request, a call, a config value) and the exact result: the response, the status code, or the error class and message. Include at least one success, plus one failure for each error path the feature has.
3. **Code map**: a compact table with the columns File path | Class / Function | Role, with one short line per role. List only the files and symbols this feature added or meaningfully changed.
4. **Notes**, only if useful: config and env vars, API endpoints with their status codes, a usage snippet, known limitations.

## Writing Part 1

- Use plain words and short sentences, under about 20 words each.
- No file names, function names, code, status codes or library names.
- Avoid jargon. When a term can't be avoided, explain it in the same sentence: "a redirect, when one website passes you on to another".
- Write about what people see and what is protected, not how the code does it.
- Numbers are fine when they mean something to a reader: "15 seconds", "10 MB".

## Visuals

Use at least three kinds of visual in a doc, with at least one in each part. Don't use the same kind twice for different things when another kind fits better. Pick the one that fits what the reader must see.

**Part 1 (plain words)**

| Reader needs to see | Use |
| --- | --- |
| What changes for them, before and after | a `compare` block |
| What works and what fails, case by case | an `examples` block |
| A decision in plain words ("is it safe? yes / no") | `flowchart LR` with plain-language labels and one `{decision}` node |
| Someone's experience, step by step, and how it feels | `journey` |
| Everything the feature covers, at a glance | `mindmap` |
| A share or proportion (real numbers only) | `pie` |
| Key facts or status | a `cards` row |

**Part 2 (technical)**

| Reader needs to see | Use |
| --- | --- |
| Components and how they connect | `flowchart LR` or `flowchart TB` |
| One request over time (login, a run, a callback) | `sequenceDiagram` |
| A linear pipeline of 3-5 stages (middleware, startup, a rotation) | a `steps` block |
| States something moves through, and how each failure ends | `stateDiagram-v2` |
| Order of events over time (a rollout, a migration, a phase plan) | `timeline` |
| Tables a feature adds or relies on | `flowchart TB` with one `subgraph` per area, not `erDiagram`, which renders cramped. Arrows go from the foreign key to the row it references. |
| Exact inputs and their results | an `examples` block |

How to draw them so they read well in print:

- Colour carries meaning, and the script owns the palette. Tag flowchart nodes with its classes instead of styling them: `:::focus` for the node the figure is about, `:::store` for databases and caches, `:::observe` for telemetry, `:::external` for clients and outside services, `:::planned` (dashed) for designed but not built. Untagged nodes are code built in this repo.
- Give nodes a bold name and a muted detail line: `api["<b>api</b><br/><small>FastAPI · :8000</small>"]`.
- Keep each diagram to about 10 nodes and 1-3 word edge labels. When one gets bigger, split it into two figures.
- No crossing edges. Reorder declarations, flip an edge's direction, or split the figure until none remain.
- Prefer `flowchart TB` when an `LR` chart is more than three nodes wide; a wide chart is scaled down until its text is too small to read.
- In sequence diagrams, keep messages short and move detail to a second line with `<br/>`. Long messages force the diagram wide, and the text shrinks when it is scaled down.
- Number and caption every figure with a paragraph right below it: `Figure N. <what it shows>. <what the reader should notice>`. Say what the colours or dashes mean when they appear.
- Use one `>` callout per part at most, for the single point a reader must not miss.

## Rules

- Accuracy over polish. Read the actual code and the diff, and use only real file names and symbol names. Every example must match what the code does: take messages and status codes from the code or the tests.
- Don't paste large code blocks. Include at most one 5-10 line snippet, in Part 2, and only if it clarifies something.
- Use a clean, consistent layout: one text font, a monospace for code, clear headings.
- Generate every feature PDF with the same script, `scripts/make_feature_doc.mjs`, so the style stays consistent. Write the content as a source file next to the PDF (for example `docs/feature/<feature-name>.md`) and run `node scripts/make_feature_doc.mjs docs/feature/<feature-name>.md`. The script's header lists the blocks it supports (`part`, `compare`, `examples`, `steps`, `cards`, callouts, captions) and the Mermaid types it themes.
- Change the look only in the script, never per doc, and re-render the existing docs in `docs/feature/` after a style change.

## Verify

- Confirm the PDF exists and that every diagram rendered as an image, not as raw Mermaid text. The script refuses to write the PDF when a diagram or a block fails to render.
- Read the PDF and check:
  - Part 2 starts on its own page.
  - Part 1 has no code or jargon.
  - Each figure's text is large enough to read, with no crossing edges, no squeezed step boxes, and no figure stranded on a mostly blank page.
  - Every example matches the code.
  
  Fix the source and re-render until it is clean.
- Finish with one line giving the PDF path. The next step is `/ship`.

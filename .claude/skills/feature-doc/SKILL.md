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

1. **Title and one-line summary**: feature name, then a lead line `**<subphase>** · <today's date> · <one sentence on its purpose>`. When the feature spans built and unbuilt parts, follow it with a `cards` row (Built / Scaffold only / Designed, not built).
2. **What it does**: 2-4 sentences or bullets in plain language, from the user's point of view.
3. **How it works**: the flow from request to logic to result, in 4-6 steps or short subsections. Put each figure right after the text it explains, not in a separate section.
4. **Code map**: a compact table with the columns File path | Class / Function | Role, with one short line per role. List only the files and symbols this feature added or meaningfully changed.
5. **Notes**, only if useful: config and env vars, API endpoints with their status codes, known limitations.

## Visuals

Every doc has at least one Mermaid diagram, and most features need two or three. Pick the form that fits what the reader must see:

- **Components and how they connect**: `flowchart LR`.
- **One request over time** (login, a run, a callback): `sequenceDiagram`.
- **A linear pipeline of 3-5 stages** (middleware chain, startup order, a rotation): a `steps` block.
- **Tables a feature adds or relies on**: a `flowchart TB` with one `subgraph` per area, not `erDiagram`, which renders cramped. Arrows go from the foreign key to the row it references.

How to draw them so they read well in print:

- Colour carries meaning, and the script owns the palette. Tag nodes with its classes instead of styling them: `:::focus` for the node the figure is about, `:::store` for databases and caches, `:::observe` for telemetry, `:::external` for clients and outside services, `:::planned` (dashed) for designed but not built. Untagged nodes are code built in this repo.
- Give nodes a bold name and a muted detail line: `api["<b>api</b><br/><small>FastAPI · :8000</small>"]`.
- Keep each diagram to about 10 nodes and 1-3 word edge labels. When one gets bigger, split it into two figures.
- No crossing edges. Reorder declarations, flip an edge's direction, or split the figure until none remain.
- In sequence diagrams, keep messages short and move detail to a second line with `<br/>`. Long messages force the diagram wide, and the text shrinks when it is scaled down.
- Number and caption every figure with a paragraph right below it: `Figure N. <what it shows>. <what the reader should notice>`. Say what the colours or dashes mean when they appear.
- Use one `>` callout per doc, for the single point a reader must not miss.

## Rules

- Accuracy over polish. Read the actual code and the diff, and use only real file names and symbol names.
- Don't paste large code blocks. Include at most one 5-10 line snippet, and only if it clarifies something.
- Use a clean, consistent layout: one text font, a monospace for code, clear headings.
- Generate every feature PDF with the same script, `scripts/make_feature_doc.mjs`, so the style stays consistent. Write the content as a source file next to the PDF (for example `docs/feature/<feature-name>.md`) and run `node scripts/make_feature_doc.mjs docs/feature/<feature-name>.md`. The script's header lists the Markdown extensions it supports (`steps`, `cards`, callouts, captions).
- Change the look only in the script, never per doc, and re-render the existing docs in `docs/feature/` after a style change.

## Verify

- Confirm the PDF exists and that every diagram rendered as an image, not as raw Mermaid text. The script refuses to write the PDF when one fails to render.
- Read the PDF and look at each figure: text large enough to read, no crossing edges, no squeezed step boxes, and no figure stranded on a mostly blank page. Fix the source and re-render until it is clean.
- Finish with one line giving the PDF path. The next step is `/ship`.

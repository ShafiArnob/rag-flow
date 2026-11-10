---
name: nextjs-shadcn
description: How frontend code is written in this repo (frontend/) — Next.js App Router, React 19, TypeScript, shadcn/ui components, and Vitest tests. Use when adding or changing a page, layout, component, or frontend data fetching.
---

# Next.js and shadcn/ui in this repo

The app lives in `frontend/src/app` (App Router), built with `output: "standalone"`, managed with pnpm.

## Components: shadcn everywhere

- CLAUDE.md requires shadcn/ui for every UI element. Before writing a button, input, dialog, table, form, or toast by hand, use the shadcn component.
- Add one with `pnpm dlx shadcn@latest add <component>` from `frontend/`. Generated files go in `src/components/ui/` and are owned by us; edit them only to change our design, not to patch behaviour.
- If `frontend/components.json` doesn't exist yet, shadcn isn't initialised. Initialising it adds Tailwind and new dependencies — confirm with the user before running `pnpm dlx shadcn@latest init`.
- Compose screens from shadcn primitives in `src/components/`. Keep raw HTML elements for layout and text only.

## Server and client components

- Components are Server Components by default. Add `"use client"` only to the smallest component that needs state, effects, event handlers, or browser APIs.
- Fetch data in Server Components or route handlers. The backend URL comes from an environment variable, never hard-coded.
- Treat API responses as untrusted: parse them into typed shapes at the boundary before use, and show a real error state when parsing fails.
- Every route segment with async data has a `loading.tsx` or `<Suspense>` boundary and an `error.tsx`.
- Use the `metadata` export for titles, as in `layout.tsx`.

## TypeScript

- Strict types. No `any`; prefer discriminated unions for UI states (`{ status: "loading" } | { status: "error"; message: string } | ...`).
- Props types are named and exported only if another module uses them.

## Tests

- Vitest, colocated as `*.test.tsx` (see `src/app/page.test.tsx`).
- Test what the user sees through the rendered output, not component internals.

## Done means

From `frontend/`: `pnpm lint && pnpm typecheck && pnpm test` all pass. For visible changes, run `pnpm dev` and look at the page.

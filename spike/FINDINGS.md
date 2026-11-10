# P01_S01 Runner Spike: Findings

Throwaway code. This directory lives only on branch `spike/p01_s01` and must never be merged into `main`. The lasting output is [docs/runner-contract.md](../docs/runner-contract.md). P01_S11 reimplements the runner, and should carry over the tests here.

## Result

Branch, skip, merge, stream and cancel all work against the fake model on `langgraph==1.2.20`, Python 3.12.

## Run it

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## What LangGraph does natively, and why the design avoids it

Each row was observed by running it.

| Native behaviour | Consequence | Design response |
| --- | --- | --- |
| `add_conditional_edges` into a join: the join never runs, and the run reports success | Silent wrong answers | No conditional edges. Every node fires, and decides for itself whether to skip |
| One `add_edge` per source into a join: the target runs twice on unequal branches | Double model calls | Joins use `add_edge([sources], target)`, which waits across unequal depths |
| Parallel branches advance in lockstep by superstep | A slow branch delays a fast one | Accepted, documented in contract section 10 |
| A parked branch holds back its siblings, and parked nodes re-execute on every resume | Constrains P03_S05 | Documented in contract section 9 |
| `on_chat_model_stream` fires for every model call, including internal `ainvoke` calls | A rewrite step's text can reach the client | Client tokens come only from what a handler yields |
| A failing node cancels siblings mid-flight, and their code may not finish | In-flight steps would stay `running` forever | The runner closes open steps from what it observed |
| `RunControl.request_drain()` stops between supersteps and raises `GraphDrained` | A cancel flag already exists | Used as the cancel flag, instead of a custom one |
| `astream_events(version="v3")` is marked experimental | Unstable API | Stay on `v2`, which is also what the plan names |
| `GraphInterrupt` subclasses `Exception` | A broad `except` turns a park into a failure | The node wrapper handles it first |
| A plain `Command(resume=value)` fails when two nodes are parked | Parallel webhooks cannot resume | Resume by interrupt ID |
| A checkpoint stays resumable after a run fails or times out | Finished runs could come back to life | `runs.status` is the authority, claimed atomically (contract section 9) |
| A resume value is lost if the worker crashes after resume, and recovery parks again | Single-use callbacks cannot be sent twice | Store the payload before resuming, redeliver it (contract section 9) |

## Decisions the plan did not specify

| Decision | Why |
| --- | --- |
| Dead-path elimination for skip | Makes deadlock structurally impossible rather than something to test for |
| An edge is live exactly when its source wrote the port | If/Else and Router need no special runner support |
| Tokens are custom events from the wrapper, not model events | One source of truth for client text and node output |
| Compile-time exclusivity check on streaming | Parallel models into a Merge would otherwise interleave tokens |
| Failure after a sent token ends the run, whatever the policy | Avoids adding a `token_reset` event to the public SSE API, which every client would have to implement |
| When nothing streamed, send the final output as one token | Clients have one way to receive an answer |
| Invalid citation markers stay in streamed text, and are dropped from `sources` only | Removing them live would break the token invariant. **A product choice: confirm it before P04_S06** |
| A cancel during the last node ends `completed` | Nothing was left to stop, so reporting `cancelled` would be false |
| Retries in the node wrapper, not LangGraph's `RetryPolicy` | The token rule above needs to see each attempt |
| No `retry` on parking nodes | A retry would park again, and callback tokens are single-use |
| Explicit start, resume and recover modes | Restarting a redelivered run with fresh input re-executed finished nodes |
| Only runs without a checkpointer stream live | A checkpointed run spans streams and repeats work, so live tokens could duplicate. Chat, the only live path, has no checkpointer |

`astream_events` now carries only custom events, so `astream(stream_mode="custom")` could likely replace it at lower overhead. Not tested. Worth measuring in P04_S08.

## How the design changed under review

An independent review of the first draft returned "do not freeze", with six blockers reproduced by running probes. All were real:

| Blocker | Resolution |
| --- | --- |
| Wiring Output to retrieved chunks silently stopped live streaming | The walk follows Output's `text` port only |
| Client tokens and node output had different sources, so an internal model call leaked to the client | One source: what the handler yields |
| Two parallel parks could not resume | Resume by interrupt ID; `RunFinished.parked` lists the IDs |
| Retry plus park parked twice, needing a second single-use callback | `retry` banned on parking nodes |
| No lifecycle across parks: lost cancels, resumable failed runs, double resume | Lifecycle table, atomic claim, closed parked steps |
| An unenforced "must" produced false success, such as a handler omitting a port | The runner checks handler outputs, and the compiler rejects parking without a checkpointer |

A second review of those fixes returned "not fit to freeze" again, with four new blockers: optional edges broke exclusivity, run outcome was held per stream so a resume lost output, recovery refused real crashes, and `can_park` per type blocked sync-mode HTTP nodes. All four are fixed, and the second and third led to the live-streaming and redelivery rules above. A third review returned "fit to freeze", with no blockers. Its one pre-tag fix, keeping an earlier stream's output when a resume fails, is done.

All three reviews were by an independent AI agent. The project owner has not yet reviewed the contract.

The reviews' should-fix items were also resolved: optional input ports, a null rule, required-input checks, `internal_error`, capped retries, `version` on nodes, crash recovery, and `is_cancel_requested` and `report_progress` for P02_S10. Each has a test in `tests/test_review_regressions.py`.

## Evidence

- 121 tests pass, stable across repeated runs. ruff and mypy strict are clean.
- `test_random_graphs_never_deadlock_and_match_the_reference_model` runs 300 seeded random DAGs and compares every node's status with an independent sequential model.
- `mutation_check.py` breaks 51 load-bearing rules one at a time and confirms a test fails for each. Its first pass exposed a real gap, an unsound exclusivity proof at a Merge that nothing caught, and a test was added. Run it with `uv run python mutation_check.py`. It rewrites source files while it runs, and restores them.
- Crashes were simulated by closing the event stream, with `InMemorySaver` and the fake model only.

Measured on the development machine, without a checkpointer unless stated:

| Measure | Result |
| --- | --- |
| Cost of a skipped node | About 1 ms (50 dead nodes added about 50 ms) |
| `steps` checkpoint size, 12-node chain, 1 KB outputs | Grows at every step, from 39 B to 11 KB, so total storage is quadratic in depth |

## Not proven

See contract section 13. In short: real providers, the Postgres checkpointer, the database claims, cancel across processes, recording `cancelled` on abort, `max_tokens`, Router, and performance under load.

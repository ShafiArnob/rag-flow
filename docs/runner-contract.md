# Runner Contract

| Field | Value |
| --- | --- |
| Version | 1 |
| Status | Frozen. Git tag: `runner-contract-v1` |
| Produced by | P01_S01 Runner Spike |
| Evidence | Branch `spike/p01_s01`: tests in `spike/tests/`, write-up in `spike/FINDINGS.md` |
| Pinned to | Python 3.12, `langgraph==1.2.20`, `langchain-core==1.7.6` |
| Binds | P01_S08, P01_S11, P02_S10, P03_S05, P03_S06, P03_S07, P04_S05, P04_S06, P04_S08, P04_S10 |

This document defines how a saved graph runs. The words "must" and "must not" are binding.

To change anything here, change the document, re-run the evidence, and tag a new version. Section 13 lists what the spike did not prove. Those items may refine this contract without contradicting it, and anything that would contradict it needs a new version.

## 1. Graph JSON

```json
{
  "schema_version": 1,
  "nodes": [
    {"id": "n1", "type": "text_input", "version": 1, "settings": {}, "on_failure": {"policy": "fail_run"}}
  ],
  "edges": [
    {"source": "n1", "source_port": "text", "target": "n2", "target_port": "text"}
  ]
}
```

- A node ID must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`. This rules out LangGraph's reserved `__start__`, `__end__`, `:` and `|`.
- `version` selects the node implementation, `type@version` in the registry.
- `on_failure` defaults to `fail_run`. See section 8.
- Graph JSON is untrusted input. It is validated before compiling.

The compiler must reject, with the error keyed to a node ID:

| Rule | Why |
| --- | --- |
| Cycles | Agent loops are out of v1, and skip propagation assumes a DAG |
| Edges to or from an unknown node or port | A dead edge must only ever mean "not taken" |
| A required input port with no edge | It would fail at runtime instead of on the canvas |
| More than one edge into a port, except a Merge input | Fan-in has one meaning: Merge |
| More than one Output node | Token streaming needs one destination |
| `continue_with_null` on a branch node | A failed branch has no taken port |
| `continue_with_null` on Output, or on a Merge between a stream source and Output, when the graph has stream sources | Tokens already sent cannot become null |
| `retry` on a node configured to park | A retry would park again, and callback tokens are single-use |
| A node configured to park, with no checkpointer | There is nothing to resume from |
| A port type mismatch (P01_S11) | Output's `text` port accepts only `text`, which the streaming rules rely on |

## 2. Compilation

One graph compiles to one `StateGraph`, with one LangGraph node per graph node.

| Graph shape | LangGraph call |
| --- | --- |
| No inbound edges | `add_edge(START, id)` |
| One distinct predecessor | `add_edge(predecessor, id)` |
| Several distinct predecessors | `add_edge([predecessors...], id)` |
| No outbound edges | `add_edge(id, END)` |

These two constructs must not be used:

- **`add_conditional_edges`.** An untaken branch never fires, so a join waiting on it never runs, and the run still reports success.
- **One `add_edge` per source for a join.** On branches of unequal length, the target runs once per arrival.

A compiled graph holds no per-run state. One compiled graph must safely serve concurrent runs, which is what lets the chat path keep compiled graphs in an LRU.

Each run sets `recursion_limit` to the node count plus one. A superstep always completes at least one level of a DAG, so this limit tripping means a runner bug, not a user error.

## 3. State shape

Run state is everything checkpointed:

```python
class StepRecord(TypedDict):
    status: Literal["completed", "failed", "skipped", "cancelled"]
    outputs: dict[str, Any]          # port name -> value

class RunState(TypedDict):
    run_input: Any
    steps: Annotated[dict[str, StepRecord], merge_step_records]   # node ID -> record
```

- `steps` needs a reducer because parallel nodes write in the same superstep.
- State must be JSON-serialisable. The runner checks each node's outputs and fails the node if they are not.
- State must never hold a secret. Secrets, limits and other per-run values travel in `RunContext`, passed as LangGraph `context`, which is not checkpointed.
- Large payloads must be passed as `file_ref`, not inline. The whole `steps` channel is stored again at every checkpoint, so total checkpoint size grows with the square of graph depth (measured: a 12-node chain of 1 KB outputs stores 11 KB at its last checkpoint alone).
- `thread_id` is the run ID.

## 4. Skip semantics

This is dead-path elimination. **Every node fires on every run.** Firing does not mean executing: each LangGraph node first decides whether the graph node should execute.

1. **An edge is live exactly when its source wrote its `source_port`.**
2. **A branch node writes exactly one of its ports.** If/Else and Router need no other runner support.
3. **A skipped or cancelled node writes no ports,** so all its outbound edges are dead and the skip propagates.
4. **A node with no inbound edges always executes.**
5. **A node with inbound edges and none live is skipped.**
6. **A Merge with at least one live inbound edge executes.**
7. **Any other node executes only if every edge into a required port is live.** A dead edge into an optional port leaves that input absent.

A skipped node never calls its handler and never counts against `max_steps`. It does produce a `skipped` step event and a `run_steps` row.

No deadlock is possible: every node fires, so every LangGraph join barrier is always satisfied.

**Null.** A null input is live, whether it came from `continue_with_null`, from a handler or from the run input. A handler must accept null on any required input, or raise and let its own failure policy apply. If/Else treats null as not matching and takes its false port.

## 5. Merge

- A Merge runs exactly once per run, after all its predecessors have fired.
- It receives the values of its live inbound edges as a list, **in edge order, not completion order**.
- A null from `continue_with_null` is live and appears in the list. Only dead inputs are dropped.
- If every input is dead, the Merge is skipped.

## 6. Events

Once `RunStarted` is sent, a run produces this ordered stream. `RunFinished` is always last, and appears exactly once unless the stream is closed early (section 8). A refused entry (section 9), or a store failure while checking it, raises before `RunStarted`: nothing started, so nothing is reported as a run.

| Event | Fields |
| --- | --- |
| `RunStarted` | `run_id` |
| `StepEvent` | `node_id`, `node_type`, `status`, `attempt`, `latency_ms`, `input`, `output`, `error` |
| `TokenEvent` | `node_id`, `text` |
| `ProgressEvent` | `node_id`, `data` |
| `RunFinished` | `run_id`, `status`, `output`, `error`, `parked` |

- `RunFinished.output` is the value of Output's `text` port, or null if Output did not complete. A `cancelled` run can carry an output. A checkpointed run spans streams, so its output and cancellation are read from `steps`, not from the current stream.
- `RunFinished.parked` lists `{node_id, interrupt_id}` for each parked node.
- `RunFinished.error` is `{code, message, node_id}` with code `node_failed`, `max_steps_exceeded`, `timeout` or `internal_error`.
- `internal_error` covers anything unexpected, such as a checkpointer failure. Such failures must still end with `RunFinished`.
- All events are sent with `adispatch_custom_event`, so they share one ordered stream. For one node, `running` comes before its tokens, and its tokens before its terminal status.

### Statuses

| Step status | Meaning | Terminal | `run_steps` row |
| --- | --- | --- | --- |
| `running` | An attempt started | No | Updates the row |
| `retrying` | An attempt failed and another will follow | No | Updates the row |
| `parked` | Waiting on an external event | No | Yes |
| `completed` | Handler returned | Yes | Yes |
| `failed` | Handler failed and no retry remains | Yes | Yes |
| `skipped` | On a dead path | Yes | Yes |
| `cancelled` | Stopped before finishing | Yes | Yes |

A node the run never reached has no step event and no row. The UI shows it as not run. If a run ends for any reason other than parking, every step still open, including a `parked` one, ends `cancelled`.

Run lifecycle. `runs.status` is the authority, not the checkpoint:

| Run status | Meaning | Next |
| --- | --- | --- |
| `running` | A worker holds the run | Any status below, or recovery after a crash |
| `parked` | Waiting to resume | `running` (resume), `cancelled`, `timed_out` |
| `completed` | The graph finished. Steps may be `failed` under `continue_with_null` | None |
| `failed` | A node failed the run, a limit was exceeded, or an internal error occurred | None |
| `cancelled` | Stopped by a cancel or a client disconnect | None |
| `timed_out` | A wall-clock limit was reached | None |

### Mapping to the public API

The public SSE event names in `04_API_IMPLEMENTATION_PLAN.md` are unchanged.

| Internal | Public |
| --- | --- |
| `TokenEvent` | `token` `{text}` |
| `StepEvent` | `step` `{node_id, status, latency_ms}`, only with `?trace=1` |
| `RunFinished` `completed` | `sources` if Output produced them, then `done` |
| `RunFinished` `failed` or `timed_out` | `error` `{code: run_failed, message}`, then `done` |
| `RunFinished` `cancelled` | `done`, if a client is still connected |
| `RunFinished` `parked` | Never on the chat path, which has no checkpointer |
| `ProgressEvent` | Run-level SSE only (P02_S10) |
| `RunStarted` | Not exposed |

`input`, `output` and `error` on a `StepEvent` may hold user data or provider error text. They must be redacted before persistence and must never go to public clients.

`sources` and `done.usage` need data this spike did not model. P04_S06 adds a `sources` output port on Output, and P04_S01 adds a `usage` field to `StepEvent`, which `run_steps` stores and `done` sums. Both are additive.

## 7. Streaming

**Only runs without a checkpointer stream live.** That is the chat path, including the editor's test chat (P04_S12), which must therefore run without one. A checkpointed run can span several streams and repeat work, so it has no stream sources, and delivers its final text once, as one `TokenEvent`, in the stream that completes it.

**One source of truth.** Client tokens and a node's output both come from what its `stream()` handler yields. The node wrapper appends each non-empty token to the node's text and, if the node is a stream source, sends a `TokenEvent` through `astream_events(version="v2")`.

The runner must not read `on_chat_model_stream`. Those events include calls a handler makes internally and never yields, such as a query rewrite, and LangChain emits them even for `ainvoke`. All other model tokens are therefore dropped by construction.

**Stream sources are computed at compile time:**

1. Walk back from the Output node's `text` port only, through stream-transparent nodes. A Merge is transparent only if a single live text input passes through it unchanged, so `first_non_null` is transparent and `list` is not. The nodes where the walk stops are the suppliers of the final text.
2. The suppliers must be pairwise mutually exclusive. Two suppliers are exclusive if one requires a branch node to take a port and the other requires the same branch node to take a different port.
3. If they are, the suppliers with a `stream()` handler are the stream sources. If they are not, there are no stream sources.

To compute what a node requires, a non-Merge node takes the union of the requirements of the edges into its required ports. A Merge, or a node with only optional inputs, takes the intersection across its inbound edges. A union at a Merge, or over optional edges, would claim exclusivity that does not exist. Exclusivity depends on rule 2 of section 4.

**Invariant: the concatenated `token` text always equals the final output.**

- Parallel models would interleave, so they do not stream live.
- When a run completes with no token sent and a text output, the runner sends that output as one `TokenEvent` before `RunFinished`. Clients therefore always receive the answer through `token`.
- A completed run with a null output sends no token and no error, only `done`.
- **Output must not alter the text of a stream source.** If the final output differs from the tokens sent, the runner ends the run `failed` with `internal_error`.

**Citations (P04_S06).** Because of that invariant, an invalid `[n]` marker stays visible in the streamed text. "Drop invalid ones" applies to `sources`: Output reads its optional `chunks` input, keeps only valid cited chunks, and writes them to its `sources` port.

## 8. Failure, cancel and limits

### Failure policies

| Policy | Behaviour |
| --- | --- |
| `{"policy": "fail_run"}` | Step `failed`, run `failed` |
| `{"policy": "continue_with_null"}` | Step `failed`, every output port is written as null, the run continues |
| `{"policy": "retry", "max_retries": n, "backoff_seconds": b}` | Up to `n` more attempts (`n` at most 10), the k-th after `b * 2^(k-1)` seconds. Then as `fail_run` |

- The runner implements retries in its node wrapper. It must not use LangGraph's `RetryPolicy`, which cannot see any of the rules below.
- **Tokens cannot be unsent.** Once a stream source has yielded a non-empty token, its failure is `fail_run` whatever its policy says. The client sees partial text and then `error`. This is why no reset event exists. A node that is not a stream source can retry mid-stream, because nothing reached the client.
- A retried node counts as one step.
- `latency_ms` covers all attempts and backoff.
- When a run fails, LangGraph cancels any parallel sibling mid-flight. That sibling's step ends `cancelled`.

### Stopping a run

| Stop | Trigger | Takes effect | In-flight node |
| --- | --- | --- | --- |
| Cancel | `RunControl.request_drain()` | Next superstep | Finishes, unless it is between retries or chooses to stop. Then its step ends `cancelled` and its siblings still finish |
| Timeout | `timeout_seconds` reached | Immediately | Killed, step `cancelled` |
| Abort | Consumer closes the event stream | Immediately | Killed, model stream stops |

- **`RunControl` is the cancel flag.** LangGraph checks it between supersteps and raises `GraphDrained`. Create one `RunControl` per run.
- A node stopping for a cancel must end its own step quietly, not raise an error, because a raised error kills parallel siblings. A long-running handler checks `call.is_cancel_requested()` and raises `NodeCancelled`, which the wrapper turns into that quiet stop. This is how P02_S10 honours cancel between documents.
- A cancel while no superstep remains cannot stop anything, so the run ends `completed`, unless a step ended `cancelled`. Dead nodes count as remaining supersteps. Callers must not assume that a cancel request produces `cancelled`.
- A cancel waits for in-flight nodes, so its latency is bounded only by the timeout. A client disconnect is an abort and does not wait.
- `timeout_seconds` is wall clock. It must not span a `yield` to the consumer, so the graph runs in its own task and hands events over through a queue.
- `max_steps` counts nodes that executed. It is checked before a node runs, and exceeding it ends the run `failed`.
- LangGraph kills in-flight nodes without finishing their code. Steps are therefore closed by the runner from what it observed, never by the node itself.
- **Persistence must happen in the task that drives the graph, not in the event consumer.** An abort removes the consumer, so `RunFinished` is never read. That task must record `cancelled` itself when its own cancellation arrives.

## 9. Park, resume and recovery

For P02_S10, P03_S05, P03_S06 and P03_S07. A checkpointer is required, and `thread_id` must be the run ID. The chat path runs without one. These hold with `InMemorySaver`. See section 13.

### Three ways to enter a run

| Mode | Graph input | Allowed when |
| --- | --- | --- |
| `StartRun(input)` | Fresh state | The run ID has no checkpoint |
| `ResumeRun({interrupt_id: value})` | `Command(resume=...)` keyed by interrupt ID | Every ID is currently parked |
| `RecoverRun()` | `None`, continuing from the last checkpoint | The run has pending work. A node that was parked parks again, on the same interrupt ID |

Any other combination is refused. Starting again with fresh input would re-execute finished nodes, so a redelivered task (P02_S10 `acks_late`) must use `RecoverRun`.

**The checkpoint cannot enforce the lifecycle.** A `failed` or `timed_out` run can still look resumable to LangGraph. Before entering a run, the caller must atomically claim it in the database:

- Resume: `UPDATE runs SET status = 'running' WHERE id = ? AND status = 'parked'`. Without this, two concurrent resumes both run, which breaks P03_S05's "completes once".
- Recovery: the claim must prove the previous worker is gone.
- Cancelling a parked run has no task to signal. It is the same atomic update to `cancelled`, closing parked steps. A later callback then finds nothing to claim.

### Parking

- A node type declares `can_park(settings)`, because parking can depend on settings: an HTTP Request node parks in webhook mode only. A node parks by calling LangGraph `interrupt()`. The wrapper lets `GraphInterrupt` through. A node not configured to park fails instead. Catching `Exception` would catch it, because it subclasses `Exception` through `GraphBubbleUp`.
- A parked run ends its event stream with `RunFinished` status `parked` and the interrupt IDs. A cancel that lands in the same superstep as a park wins, and the run ends `cancelled`.
- Parallel parks are allowed and resume independently, one callback at a time. Interrupt IDs stay stable across resumes, so a callback token keeps pointing at its node.
- **Every parked node re-executes from its first line on every resume,** including nodes whose callback has not arrived. Anything before `interrupt()` with an outside effect must be guarded by a durable send-once marker keyed on `(run_id, step_id)`.
- **A parked branch holds back every sibling branch,** not only the nodes after it, because of section 10. Work already finished is not repeated.
- **A failure after resume applies the node's own policy, once.** `retry` is banned on parking nodes for this reason. Any retry of an outside call belongs inside the handler, before `interrupt()`.
- **A park deadline (P03_S05)** is a scheduled resume of that interrupt with a timeout value. The handler raises on it, and its failure policy applies. The run's `timeout_seconds` does not run during a park.
- On resume or recovery, the caller passes the time budget still unspent as `timeout_seconds`, because `timeout_seconds` covers one stream.
- `RunContext` is not checkpointed. The runner recomputes `steps_used` from the executed nodes in state.
- Recovery re-executes any node that was in flight at the crash, so that node must be idempotent. Its attempt count restarts.
- **A crash after resume may lose the delivered value.** The checkpoint does not reliably keep it, and when it is lost, recovery parks that node again. Callback tokens are single-use, so the caller must store a callback's payload durably, in `run_callbacks`, before resuming, and redeliver it with `ResumeRun` whenever recovery reports the same interrupt ID.
- **A cancel of a running checkpointed run must also be recorded durably.** `RunControl` does not survive a crash, so the recovering worker must check for that intent before entering the run.
- If the graph finished before the crash, nothing is pending and `RecoverRun` is refused. The caller finalises the run from `steps`.

## 10. Consequences to design around

- **Parallel branches advance in lockstep by superstep.** A fast branch's second node waits for the slow branch's first node to finish. A run's latency is the sum, over supersteps, of the slowest node in each.
- **A skipped node costs about 1 ms, not a model call.** A Merge still waits for a dead path to finish firing, so a dead chain adds roughly 1 ms per node to the run (measured: 50 dead nodes added about 50 ms). With a checkpointer, each extra superstep also writes a checkpoint.
- **The event queue is unbounded.** Memory for a stalled client is bounded only by the run's timeout.

## 11. Node author rules

For P01_S08. A node type declares `input_ports`, `optional_input_ports`, `output_ports`, `can_park(settings)`, and exactly one of `run()` or `stream()`. A `stream()` node has exactly one output port.

A handler receives a call with `inputs`, `settings`, `run_input`, `attempt`, `secrets`, `is_cancel_requested()` and `report_progress(data)`.

- `run()` returns `{port: value}`. It must write exactly its output ports, and a branch node exactly one. The runner enforces this: a violation is a handler failure, and the node's policy applies.
- `stream()` yields text tokens. The runner joins them into the node's `text` port. Only what is yielded counts: anything a handler keeps to itself never reaches the client.
- Handlers raise on failure and must not catch `asyncio.CancelledError`.
- Handlers raise `NodeCancelled` only after `is_cancel_requested()` returned true.
- Handlers must not return a secret in an output.
- Handlers read secrets from the call, never from state, inputs or settings.

## 12. Upgrade guard

These LangGraph behaviours are load-bearing. The tests in `spike/tests/` pin them, and P01_S11 must carry those tests into the runner suite, so that a version bump fails loudly:

- A list `add_edge` waits for all sources across unequal branch lengths.
- `add_conditional_edges` starves a join (the reason for section 4).
- Parallel branches advance in lockstep.
- `RunControl` stops between supersteps and raises `GraphDrained`.
- An exception raised in a node reaches the `astream_events` consumer with its type intact.
- `astream_events` `v2` delivers custom events in one ordered stream.
- `Command(resume={interrupt_id: value})` resumes only the named interrupt, and interrupt IDs stay stable.

`astream_events(version="v3")` exists but is marked experimental, so it must not be used.

## 13. Not proven by the spike

Each of these needs its owning subphase to confirm it. None was exercised here.

| Item | Owner |
| --- | --- |
| Real provider streaming, including Anthropic content blocks through `chunk.text` | P04_S01 |
| `sources` and `usage` (section 6) | P04_S01, P04_S06 |
| `langgraph-checkpoint-postgres`, and resume on a different worker | P03_S05 |
| The atomic claims in section 9, including concurrent resume. The spike has no database | P03_S05 |
| Cancel from another process, such as a poller that calls `request_drain()` | P01_S11 |
| Telling a shutdown drain from a cancel by `drain_reason`, so shutdown can resume | P03_S05 |
| Recording `cancelled` on abort (section 8). The spike proves only that the model stops | P01_S11, P04_S08 |
| The `max_tokens` limit. Expected to behave like `max_steps`, ending the run `failed` with code `max_tokens_exceeded` | P01_S11 |
| Finalising a run from `steps` after a crash, and a crash before the first node finishes, which currently refuses both `StartRun` and `RecoverRun` | P02_S10 |
| Router, and the `text` Merge combine mode | P04_S05 |
| `type@version` lookup, which the spike parses but ignores, and `migrate()` | P01_S08 |
| Typed port checks. The spike checks port names only | P01_S11 |
| Throughput and overhead of `astream_events` under load | P04_S08 |

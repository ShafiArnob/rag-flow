"""What happens inside one graph node: skip decision, attempts and failure policy."""

import asyncio
import json
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.callbacks import adispatch_custom_event
from langgraph.errors import GraphInterrupt
from langgraph.runtime import Runtime

from runner_spike.events import (
    PROGRESS_EVENT_NAME,
    STEP_EVENT_NAME,
    TOKEN_EVENT_NAME,
    ProgressEvent,
    RunContext,
    RunState,
    StepEvent,
    StepRecord,
    StepStatus,
    TokenEvent,
)
from runner_spike.nodes import MERGE_INPUT_PORT, NodeCall, NodeCancelled, NodeKind, NodeType
from runner_spike.spec import ContinueWithNull, EdgeSpec, FailRun, NodeSpec, Retry


class GraphStep(Protocol):
    def __call__(
        self, state: RunState, *, runtime: Runtime[RunContext]
    ) -> Awaitable[dict[str, Any]]: ...


class NodeFailed(Exception):
    def __init__(self, node_id: str, message: str) -> None:
        super().__init__(f"node {node_id} failed: {message}")
        self.node_id = node_id
        self.message = message


class MaxStepsExceeded(Exception):
    def __init__(self, node_id: str, max_steps: int) -> None:
        super().__init__(f"node {node_id} would exceed the limit of {max_steps} steps")
        self.node_id = node_id


class NodeContractViolation(Exception):
    """A handler broke the node contract. Handled like any other handler failure."""


@dataclass(frozen=True)
class RetryAfter:
    delay_seconds: float


@dataclass(frozen=True)
class FailTheRun:
    pass


@dataclass(frozen=True)
class ContinueNull:
    pass


FailureDecision = RetryAfter | FailTheRun | ContinueNull


def decide_on_failure(
    policy: FailRun | ContinueWithNull | Retry, attempt: int, *, has_sent_tokens: bool
) -> FailureDecision:
    """Tokens already sent cannot be unsent, so that failure ends the run under any policy."""
    if has_sent_tokens:
        return FailTheRun()
    match policy:
        case Retry(max_retries=max_retries, backoff_seconds=backoff) if attempt <= max_retries:
            return RetryAfter(backoff * 2 ** (attempt - 1))
        case ContinueWithNull():
            return ContinueNull()
        case _:
            return FailTheRun()


def live_edges(edges: Sequence[EdgeSpec], records: dict[str, StepRecord]) -> list[EdgeSpec]:
    """An edge is live exactly when its source wrote its port."""
    return [edge for edge in edges if edge.source_port in records[edge.source]["outputs"]]


def is_skipped(node_type: NodeType, inbound: Sequence[EdgeSpec], live: Sequence[EdgeSpec]) -> bool:
    if not inbound:
        return False
    if not live:
        return True
    if node_type.kind is NodeKind.MERGE:
        return False
    required = [e for e in inbound if e.target_port not in node_type.optional_input_ports]
    return any(edge not in live for edge in required)


def gather_inputs(
    kind: NodeKind, live: Sequence[EdgeSpec], records: dict[str, StepRecord]
) -> dict[str, Any]:
    values = [
        (edge.target_port, records[edge.source]["outputs"][edge.source_port]) for edge in live
    ]
    if kind is NodeKind.MERGE:
        return {MERGE_INPUT_PORT: [value for _, value in values]}
    return dict(values)


def check_outputs(node_type: NodeType, outputs: Mapping[str, Any]) -> None:
    """A wrong port set would silently kill or wake edges, so it is a failure, not a skip."""
    written = set(outputs)
    declared = set(node_type.output_ports)
    if node_type.kind is NodeKind.BRANCH:
        if len(written) != 1 or not written <= declared:
            raise NodeContractViolation(f"branch must write one port, wrote {sorted(written)}")
    elif written != declared:
        raise NodeContractViolation(f"expected ports {sorted(declared)}, got {sorted(written)}")
    try:
        json.dumps(dict(outputs), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise NodeContractViolation(f"outputs are not JSON-serialisable: {error}") from error


async def _emit(event: StepEvent) -> None:
    await adispatch_custom_event(STEP_EVENT_NAME, event)


async def _run_attempt(
    node: NodeSpec,
    node_type: NodeType,
    call: NodeCall,
    tokens: list[str],
    *,
    is_stream_source: bool,
) -> dict[str, Any]:
    """Run the handler once. `tokens` is the single source of both client text and output."""
    if node_type.stream is None:
        assert node_type.run is not None  # guaranteed by NodeType.__post_init__
        outputs = await node_type.run(call)
    else:
        async for token in node_type.stream(call):
            if not token:
                continue
            tokens.append(token)
            if is_stream_source:
                await adispatch_custom_event(TOKEN_EVENT_NAME, TokenEvent(node.id, token))
        outputs = {node_type.output_ports[0]: "".join(tokens)}
    check_outputs(node_type, outputs)
    return outputs


def make_step(
    node: NodeSpec,
    node_type: NodeType,
    inbound: Sequence[EdgeSpec],
    *,
    is_stream_source: bool,
) -> GraphStep:
    """Build the function LangGraph runs for `node`. Every node fires on every run."""

    def event(status: StepStatus, attempt: int = 0, **fields: Any) -> StepEvent:
        return StepEvent(node.id, node.type, status, attempt, **fields)

    def result(
        status: Literal["completed", "failed", "skipped", "cancelled"], outputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {"steps": {node.id: StepRecord(status=status, outputs=outputs)}}

    async def report_progress(data: Mapping[str, Any]) -> None:
        await adispatch_custom_event(PROGRESS_EVENT_NAME, ProgressEvent(node.id, data))

    async def step(state: RunState, *, runtime: Runtime[RunContext]) -> dict[str, Any]:
        records = state["steps"]
        live = live_edges(inbound, records)
        if is_skipped(node_type, inbound, live):
            await _emit(event(StepStatus.SKIPPED))
            return result("skipped", {})

        context = runtime.context
        if context.steps_used >= context.limits.max_steps:
            raise MaxStepsExceeded(node.id, context.limits.max_steps)
        context.steps_used += 1

        inputs = gather_inputs(node_type.kind, live, records)
        started_at = time.monotonic()

        def elapsed_ms() -> int:
            return round((time.monotonic() - started_at) * 1000)

        def is_cancel_requested() -> bool:
            return runtime.drain_requested

        async def stop_cancelled(attempt: int) -> dict[str, Any]:
            """End this step quietly: raising here would kill parallel siblings mid-flight."""
            await _emit(event(StepStatus.CANCELLED, attempt, latency_ms=elapsed_ms()))
            return result("cancelled", {})

        attempt = 0
        while True:
            attempt += 1
            await _emit(event(StepStatus.RUNNING, attempt, input=inputs))
            call = NodeCall(
                inputs,
                node.settings,
                state["run_input"],
                attempt,
                context.secrets,
                is_cancel_requested,
                report_progress,
            )
            tokens: list[str] = []
            try:
                outputs = await _run_attempt(
                    node, node_type, call, tokens, is_stream_source=is_stream_source
                )
            except GraphInterrupt:
                if not node_type.can_park(node.settings):
                    message = f"node {node.id} is not configured to park"
                    await _emit(event(StepStatus.FAILED, attempt, error=message))
                    raise NodeFailed(node.id, message) from None
                # A park is control flow, not a failure: it must reach LangGraph untouched.
                await _emit(event(StepStatus.PARKED, attempt))
                raise
            except NodeCancelled:
                return await stop_cancelled(attempt)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                decision = decide_on_failure(
                    node.on_failure, attempt, has_sent_tokens=is_stream_source and bool(tokens)
                )
                if isinstance(decision, RetryAfter):
                    await _emit(event(StepStatus.RETRYING, attempt, error=message))
                    await asyncio.sleep(decision.delay_seconds)
                    if runtime.drain_requested:
                        return await stop_cancelled(attempt)
                    continue
                await _emit(
                    event(StepStatus.FAILED, attempt, latency_ms=elapsed_ms(), error=message)
                )
                if isinstance(decision, ContinueNull):
                    return result("failed", dict.fromkeys(node_type.output_ports))
                raise NodeFailed(node.id, message) from error
            await _emit(
                event(
                    StepStatus.COMPLETED,
                    attempt,
                    latency_ms=elapsed_ms(),
                    input=inputs,
                    output=outputs,
                )
            )
            return result("completed", outputs)

    return step

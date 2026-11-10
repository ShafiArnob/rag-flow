"""Shared builders. Every run is bounded, so a deadlock fails a test instead of hanging it."""

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import replace
from typing import Any

from runner_spike.compiler import CompiledWorkflow, compile_workflow
from runner_spike.events import (
    ResumeRun,
    RunEvent,
    RunFinished,
    RunLimits,
    RunMode,
    StartRun,
    StepEvent,
    StepStatus,
    TokenEvent,
)
from runner_spike.nodes import NodeCall, NodeType, default_registry
from runner_spike.runner import stream_run
from runner_spike.spec import EdgeSpec, GraphSpec, NodeSpec

DEADLOCK_GUARD_SECONDS = 10.0
GENEROUS_LIMITS = RunLimits(timeout_seconds=DEADLOCK_GUARD_SECONDS, max_steps=1000)


def node(id: str, type: str, on_failure: Any = None, **settings: Any) -> NodeSpec:
    if on_failure is None:
        return NodeSpec(id=id, type=type, settings=settings)
    return NodeSpec(id=id, type=type, settings=settings, on_failure=on_failure)


def edge(
    source: str, target: str, source_port: str = "text", target_port: str = "text"
) -> EdgeSpec:
    return EdgeSpec(source=source, source_port=source_port, target=target, target_port=target_port)


def to_merge(source: str, target: str, source_port: str = "text") -> EdgeSpec:
    return edge(source, target, source_port, "inputs")


def from_merge(source: str, target: str) -> EdgeSpec:
    return edge(source, target, "merged", "text")


def spec(nodes: Iterable[NodeSpec], edges: Iterable[EdgeSpec] = ()) -> GraphSpec:
    return GraphSpec(nodes=tuple(nodes), edges=tuple(edges))


def compiled(
    nodes: Iterable[NodeSpec],
    edges: Iterable[EdgeSpec] = (),
    registry: Mapping[str, NodeType] | None = None,
) -> CompiledWorkflow:
    return compile_workflow(spec(nodes, edges), registry or default_registry())


async def collect(
    workflow: CompiledWorkflow,
    run_input: Any = "hello",
    limits: RunLimits = GENEROUS_LIMITS,
    mode: RunMode | None = None,
    **kwargs: Any,
) -> list[RunEvent]:
    async with asyncio.timeout(DEADLOCK_GUARD_SECONDS + 5):
        stream = stream_run(workflow, mode or StartRun(run_input), limits, **kwargs)
        return [event async for event in stream]


def finished(events: list[RunEvent]) -> RunFinished:
    terminal = [event for event in events if isinstance(event, RunFinished)]
    assert len(terminal) == 1, f"expected exactly one RunFinished, got {terminal}"
    assert events[-1] is terminal[0], "RunFinished must be the last event"
    return terminal[0]


def resume_all(events: list[RunEvent], value: Any) -> ResumeRun:
    return ResumeRun({node.interrupt_id: value for node in finished(events).parked})


def step_events(events: list[RunEvent]) -> list[StepEvent]:
    return [event for event in events if isinstance(event, StepEvent)]


def final_statuses(events: list[RunEvent]) -> dict[str, StepStatus]:
    """Each node's last reported status."""
    return {event.node_id: event.status for event in step_events(events)}


def tokens(events: list[RunEvent]) -> list[TokenEvent]:
    return [event for event in events if isinstance(event, TokenEvent)]


def token_text(events: list[RunEvent]) -> str:
    return "".join(event.text for event in tokens(events))


def started_nodes(events: list[RunEvent]) -> list[str]:
    return [e.node_id for e in step_events(events) if e.status is StepStatus.RUNNING]


def with_counted_tokens(registry: dict[str, NodeType], produced: list[str]) -> dict[str, NodeType]:
    """`fake_model` that also records every token it produces, forwarded to the client or not."""
    original = registry["fake_model"]
    assert original.stream is not None
    original_stream = original.stream

    async def counting_stream(call: NodeCall) -> AsyncIterator[str]:
        async for token in original_stream(call):
            produced.append(token)
            yield token

    return {**registry, "fake_model": replace(original, stream=counting_stream)}


def with_call_log(registry: dict[str, NodeType], calls: list[str]) -> dict[str, NodeType]:
    """Every `merge` logs its call, so tests can assert it fired exactly once."""
    original = registry["merge"]
    assert original.run is not None
    original_run = original.run

    async def logged_run(call: NodeCall) -> dict[str, Any]:
        calls.append(f"merge:{call.settings.get('label')}")
        return await original_run(call)

    return {**registry, "merge": replace(original, run=logged_run)}

"""Cancel between nodes, wall-clock timeout and max steps."""

import asyncio
import time

from langgraph.runtime import RunControl

from runner_spike.compiler import CompiledWorkflow
from runner_spike.events import (
    ErrorCode,
    RunEvent,
    RunLimits,
    RunStatus,
    StartRun,
    StepEvent,
    StepStatus,
)
from runner_spike.nodes import default_registry
from runner_spike.runner import stream_run
from runner_spike.spec import Retry
from tests.helpers import (
    GENEROUS_LIMITS,
    collect,
    compiled,
    edge,
    final_statuses,
    finished,
    from_merge,
    node,
    started_nodes,
    to_merge,
    with_counted_tokens,
)

CANCEL_REASON = "cancelled by user"


async def cancel_when_running(workflow: CompiledWorkflow, node_id: str) -> list[RunEvent]:
    control = RunControl()
    events: list[RunEvent] = []
    async with asyncio.timeout(10):
        async for event in stream_run(workflow, StartRun("hi"), GENEROUS_LIMITS, control=control):
            events.append(event)
            if (
                isinstance(event, StepEvent)
                and event.node_id == node_id
                and event.status is StepStatus.RUNNING
            ):
                control.request_drain(CANCEL_REASON)
    return events


def chain() -> CompiledWorkflow:
    nodes = [
        node("input", "text_input"),
        node("a", "fake_model", response="a a a", token_delay_seconds=0.01),
        node("b", "fake_model", response="b"),
        node("c", "fake_model", response="c"),
    ]
    return compiled(nodes, [edge("input", "a"), edge("a", "b"), edge("b", "c")])


async def test_cancel_lets_the_current_node_finish_and_starts_no_more() -> None:
    events = await cancel_when_running(chain(), "a")

    assert finished(events).status is RunStatus.CANCELLED
    assert final_statuses(events)["a"] is StepStatus.COMPLETED
    assert started_nodes(events) == ["input", "a"]


async def test_cancel_during_parallel_nodes_stops_at_the_next_superstep() -> None:
    nodes = [
        node("input", "text_input"),
        node("a", "fake_model", response="a", token_delay_seconds=0.02),
        node("b", "fake_model", response="b", token_delay_seconds=0.02),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "a"),
        edge("input", "b"),
        to_merge("a", "join"),
        to_merge("b", "join"),
        from_merge("join", "output"),
    ]

    events = await cancel_when_running(compiled(nodes, edges), "a")

    assert finished(events).status is RunStatus.CANCELLED
    assert final_statuses(events)["a"] is StepStatus.COMPLETED
    assert final_statuses(events)["b"] is StepStatus.COMPLETED
    assert "join" not in started_nodes(events)


async def test_cancel_during_the_last_node_lets_the_run_complete() -> None:
    """Nothing is left to stop, so the run's work is done and it reports that honestly."""
    events = await cancel_when_running(chain(), "c")

    assert finished(events).status is RunStatus.COMPLETED


async def test_cancel_stops_a_retry_loop_between_attempts() -> None:
    nodes = [
        node("input", "text_input"),
        node(
            "flaky",
            "fake_model",
            Retry(max_retries=10, backoff_seconds=0.001),
            response="x",
            failure_rate=1.0,
        ),
    ]
    workflow = compiled(nodes, [edge("input", "flaky")])

    events = await cancel_when_running(workflow, "flaky")

    assert finished(events).status is RunStatus.CANCELLED
    assert started_nodes(events).count("flaky") == 1
    assert final_statuses(events)["flaky"] is StepStatus.CANCELLED


async def test_timeout_interrupts_a_model_mid_stream_and_stops_its_tokens() -> None:
    produced: list[str] = []
    registry = with_counted_tokens(default_registry(), produced)
    nodes = [
        node("input", "text_input"),
        node(
            "slow",
            "fake_model",
            response=" ".join(str(i) for i in range(5000)),
            token_delay_seconds=0.002,
        ),
        node("after", "fake_model", response="never"),
    ]
    workflow = compiled(nodes, [edge("input", "slow"), edge("slow", "after")], registry)
    limits = RunLimits(timeout_seconds=0.15, max_steps=100)

    started_at = time.monotonic()
    events = await collect(workflow, limits=limits)
    elapsed = time.monotonic() - started_at

    result = finished(events)
    assert result.status is RunStatus.TIMED_OUT
    assert result.error is not None and result.error.code is ErrorCode.TIMEOUT
    assert elapsed < 1.5, "the timeout must not wait for the node to finish"
    assert final_statuses(events)["slow"] is StepStatus.CANCELLED
    assert "after" not in started_nodes(events)

    seen_at_timeout = len(produced)
    await asyncio.sleep(0.05)
    assert len(produced) == seen_at_timeout
    assert 0 < seen_at_timeout < 5000


async def test_timeout_covers_a_slow_consumer() -> None:
    """The limit is wall clock for the run, whatever the client is doing."""
    nodes = [
        node("input", "text_input"),
        node(
            "slow",
            "fake_model",
            response=" ".join(str(i) for i in range(5000)),
            token_delay_seconds=0.002,
        ),
        node("output", "text_output"),
    ]
    workflow = compiled(nodes, [edge("input", "slow"), edge("slow", "output")])
    limits = RunLimits(timeout_seconds=0.1, max_steps=100)

    statuses = []
    async for event in stream_run(workflow, StartRun("hi"), limits):
        await asyncio.sleep(0.05)
        statuses.append(event)
        if len(statuses) == 3:
            await asyncio.sleep(0.2)

    assert finished(statuses).status is RunStatus.TIMED_OUT


async def test_max_steps_stops_the_run_before_the_next_node() -> None:
    limits = RunLimits(timeout_seconds=5, max_steps=2)

    events = await collect(chain(), limits=limits)

    result = finished(events)
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code is ErrorCode.MAX_STEPS_EXCEEDED
    assert result.error.node_id == "b"
    assert started_nodes(events) == ["input", "a"]


async def test_skipped_nodes_do_not_use_up_max_steps() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("a", "fake_model", response="a"),
        node("b", "fake_model", response="b"),
        node("c", "fake_model", response="c"),
    ]
    edges = [edge("input", "if"), edge("if", "a", "true"), edge("a", "b"), edge("b", "c")]

    events = await collect(compiled(nodes, edges), limits=RunLimits(timeout_seconds=5, max_steps=2))

    assert finished(events).status is RunStatus.COMPLETED

"""Pins the LangGraph behaviour the contract leans on, so an upgrade that changes it fails here.

These test LangGraph itself, not our code. They are why the design looks the way it does.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from runner_spike.compiler import compile_workflow
from runner_spike.events import (
    RunContext,
    RunLimits,
    RunState,
    RunStatus,
    StepStatus,
    merge_step_records,
)
from runner_spike.fake_model import FakeStreamingChatModel
from runner_spike.nodes import NodeCall, NodeType, default_registry
from tests.helpers import (
    collect,
    compiled,
    edge,
    final_statuses,
    finished,
    node,
    resume_all,
    spec,
    started_nodes,
    to_merge,
)


def writer(name: str, calls: list[str]) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    async def write(_state: RunState) -> dict[str, Any]:
        calls.append(name)
        return {"steps": {name: {"status": "completed", "outputs": {}}}}

    return write


async def test_native_conditional_edge_starves_a_join_and_still_reports_success() -> None:
    """Why branching is not built on `add_conditional_edges`: the join silently never runs."""
    calls: list[str] = []
    builder: Any = StateGraph(RunState)
    for name in ("start", "taken", "untaken", "join"):
        builder.add_node(name, writer(name, calls))
    builder.add_edge(START, "start")
    builder.add_conditional_edges("start", lambda _state: "taken", ["taken", "untaken"])
    builder.add_edge(["taken", "untaken"], "join")
    builder.add_edge("join", END)

    await builder.compile().ainvoke({"run_input": None, "steps": {}})

    assert calls == ["start", "taken"]


async def test_plain_fan_in_edges_run_the_target_twice_on_unequal_branches() -> None:
    """Why joins use a list of sources rather than one edge per source."""
    calls: list[str] = []
    builder: Any = StateGraph(RunState)
    for name in ("start", "short", "long1", "long2", "join"):
        builder.add_node(name, writer(name, calls))
    builder.add_edge(START, "start")
    builder.add_edge("start", "short")
    builder.add_edge("start", "long1")
    builder.add_edge("long1", "long2")
    builder.add_edge("short", "join")
    builder.add_edge("long2", "join")
    builder.add_edge("join", END)

    await builder.compile().ainvoke({"run_input": None, "steps": {}})

    assert calls.count("join") == 2


async def test_parallel_branches_advance_in_lockstep_by_superstep() -> None:
    """A fast branch's second node waits for the slow branch's first. Latency adds up."""
    finished_at: dict[str, float] = {}
    started_at: dict[str, float] = {}
    loop = asyncio.get_running_loop()

    def timed(name: str, seconds: float) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
        async def run(_state: RunState) -> dict[str, Any]:
            started_at[name] = loop.time()
            await asyncio.sleep(seconds)
            finished_at[name] = loop.time()
            return {"steps": {name: {"status": "completed", "outputs": {}}}}

        return run

    builder: Any = StateGraph(RunState)
    builder.add_node("slow", timed("slow", 0.1))
    builder.add_node("fast1", timed("fast1", 0.0))
    builder.add_node("fast2", timed("fast2", 0.0))
    builder.add_edge(START, "slow")
    builder.add_edge(START, "fast1")
    builder.add_edge("fast1", "fast2")
    builder.add_edge("fast2", END)
    builder.add_edge("slow", END)

    await builder.compile().ainvoke({"run_input": None, "steps": {}})

    assert started_at["fast2"] >= finished_at["slow"]


def test_state_reducer_merges_parallel_writes() -> None:
    merged = merge_step_records(
        {"a": {"status": "completed", "outputs": {}}},
        {"b": {"status": "skipped", "outputs": {}}},
    )

    assert sorted(merged) == ["a", "b"]


def registry_with_park(calls: list[str]) -> dict[str, NodeType]:
    async def park(call: NodeCall) -> dict[str, Any]:
        calls.append("park entered")
        answer = interrupt({"waiting_for": "callback"})
        calls.append("park resumed")
        return {"text": answer}

    return {
        **default_registry(),
        "park": NodeType(("text",), ("text",), run=park, can_park=lambda _settings: True),
    }


def parked_graph() -> tuple[list[Any], list[Any]]:
    nodes = [
        node("input", "text_input"),
        node("parker", "park"),
        node("side1", "fake_model", response="side1"),
        node("side2", "fake_model", response="side2"),
        node("join", "merge"),
    ]
    edges = [
        edge("input", "parker"),
        edge("input", "side1"),
        edge("side1", "side2"),
        to_merge("parker", "join"),
        to_merge("side2", "join"),
    ]
    return nodes, edges


async def test_park_and_resume_through_a_merge_completes_once() -> None:
    """P03_S05 needs this to work without changing the state shape or the skip semantics."""
    calls: list[str] = []
    workflow = compile_workflow(
        spec(*parked_graph()), registry_with_park(calls), checkpointer=InMemorySaver()
    )

    first = await collect(workflow, run_id="run-1")

    assert finished(first).status is RunStatus.PARKED
    assert final_statuses(first)["parker"] is StepStatus.PARKED
    assert final_statuses(first)["side1"] is StepStatus.COMPLETED
    assert "side2" not in started_nodes(first), "a parked branch holds back its siblings too"

    second = await collect(workflow, run_id="run-1", mode=resume_all(first, "called back"))

    assert finished(second).status is RunStatus.COMPLETED
    assert final_statuses(second)["join"] is StepStatus.COMPLETED
    assert "side1" not in started_nodes(second), "finished work is not repeated on resume"
    assert calls == ["park entered", "park entered", "park resumed"], "the node re-enters"


async def test_resumed_run_still_counts_steps_already_used() -> None:
    workflow = compile_workflow(
        spec(*parked_graph()), registry_with_park([]), checkpointer=InMemorySaver()
    )
    limits = RunLimits(timeout_seconds=5, max_steps=4)

    first = await collect(workflow, limits=limits, run_id="run-1")
    resumed = await collect(workflow, limits=limits, run_id="run-1", mode=resume_all(first, "ok"))

    assert finished(resumed).status is RunStatus.FAILED


async def test_checkpointed_state_is_json_and_holds_no_secret() -> None:
    sentinel = "sk-test-do-not-persist"

    async def uses_secret(call: NodeCall) -> dict[str, Any]:
        assert call.secrets["api_key"] == sentinel
        return {"text": "used it"}

    registry = {
        **default_registry(),
        "needs_key": NodeType(("text",), ("text",), run=uses_secret),
    }
    saver = InMemorySaver()
    workflow = compile_workflow(
        spec([node("input", "text_input"), node("n", "needs_key")], [edge("input", "n")]),
        registry,
        checkpointer=saver,
    )

    events = await collect(workflow, run_id="run-1", secrets={"api_key": sentinel})

    assert finished(events).status is RunStatus.COMPLETED
    state = await workflow.graph.aget_state({"configurable": {"thread_id": "run-1"}})
    json.dumps(state.values)
    assert sentinel not in repr(saver.storage)
    assert sentinel not in repr(saver.writes)
    assert sentinel not in repr(saver.blobs)
    assert sentinel not in repr(state.metadata)


async def test_tokens_stream_even_when_a_node_awaits_the_model_without_astream() -> None:
    """Why client tokens never come from model events: `ainvoke` emits them as well."""

    async def uses_ainvoke(_call: NodeCall) -> dict[str, Any]:
        message = await FakeStreamingChatModel(response="quiet call").ainvoke("prompt")
        return {"text": message.text}

    registry = {**default_registry(), "quiet": NodeType(("text",), ("text",), run=uses_ainvoke)}
    workflow = compiled(
        [
            node("input", "text_input"),
            node("q", "quiet"),
            node("output", "text_output"),
        ],
        [edge("input", "q"), edge("q", "output")],
        registry,
    )

    raw_kinds = []
    async for raw in workflow.graph.astream_events(
        {"run_input": "x", "steps": {}},
        version="v2",
        context=RunContext(limits=RunLimits(timeout_seconds=5, max_steps=10)),
    ):
        raw_kinds.append(raw["event"])
    assert "on_chat_model_stream" in raw_kinds

"""If/Else skip propagation and Merge joins, including the randomized no-deadlock proof."""

import random
from graphlib import TopologicalSorter
from typing import Any

import pytest
from pydantic import ValidationError

from runner_spike.compiler import GraphInvalid
from runner_spike.events import RunStatus, StepStatus
from runner_spike.nodes import NodeCall, NodeKind, NodeType, default_registry
from runner_spike.spec import ContinueWithNull, EdgeSpec, NodeSpec
from tests.helpers import (
    collect,
    compiled,
    edge,
    final_statuses,
    finished,
    from_merge,
    node,
    started_nodes,
    step_events,
    to_merge,
    with_call_log,
)

COMPLETED = StepStatus.COMPLETED
SKIPPED = StepStatus.SKIPPED


def branch_graph(contains: str) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    """input -> if -(true)-> yes -> yes2 -> merge, -(false)-> no -> merge -> output."""
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains=contains),
        node("yes", "fake_model", response="yes"),
        node("yes2", "fake_model", response="yes2"),
        node("no", "fake_model", response="no"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "yes", "true"),
        edge("yes", "yes2"),
        edge("if", "no", "false"),
        to_merge("yes2", "join"),
        to_merge("no", "join"),
        from_merge("join", "output"),
    ]
    return nodes, edges


async def test_untaken_branch_is_skipped_and_never_executed() -> None:
    events = await collect(compiled(*branch_graph("hello")), "hello world")

    statuses = final_statuses(events)
    assert statuses["no"] is SKIPPED
    assert statuses["yes"] is COMPLETED and statuses["yes2"] is COMPLETED
    assert "no" not in started_nodes(events)


async def test_false_condition_takes_the_other_branch() -> None:
    events = await collect(compiled(*branch_graph("absent")), "hello world")

    statuses = final_statuses(events)
    assert statuses["yes"] is SKIPPED and statuses["yes2"] is SKIPPED
    assert statuses["no"] is COMPLETED


async def test_skip_propagates_through_a_whole_chain() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("a", "fake_model", response="a"),
        node("b", "fake_model", response="b"),
        node("c", "fake_model", response="c"),
    ]
    edges = [edge("input", "if"), edge("if", "a", "true"), edge("a", "b"), edge("b", "c")]

    statuses = final_statuses(await collect(compiled(nodes, edges)))

    assert [statuses[n] for n in "abc"] == [SKIPPED, SKIPPED, SKIPPED]


async def test_every_node_reports_exactly_one_terminal_status() -> None:
    events = await collect(compiled(*branch_graph("hello")), "hello")

    terminal = [e.node_id for e in step_events(events) if e.status in (COMPLETED, SKIPPED)]

    assert sorted(terminal) == sorted(["input", "if", "yes", "yes2", "no", "join", "output"])


@pytest.mark.parametrize("contains", ["hello", "absent"])
async def test_merge_after_skip_runs_once_with_only_the_live_input(contains: str) -> None:
    calls: list[str] = []
    registry = with_call_log(default_registry(), calls)

    events = await collect(compiled(*branch_graph(contains), registry), "hello")

    expected = ["yes2"] if contains == "hello" else ["no"]
    assert calls == ["merge:None"]
    assert finished(events).status is RunStatus.COMPLETED
    assert finished(events).output == expected


async def test_merge_waits_for_every_live_input_across_unequal_branch_lengths() -> None:
    nodes = [
        node("input", "text_input"),
        node("short", "fake_model", response="short"),
        node("long1", "fake_model", response="l1", token_delay_seconds=0.02),
        node("long2", "fake_model", response="l2", token_delay_seconds=0.02),
        node("long3", "fake_model", response="long"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "short"),
        edge("input", "long1"),
        edge("long1", "long2"),
        edge("long2", "long3"),
        to_merge("short", "join"),
        to_merge("long3", "join"),
        from_merge("join", "output"),
    ]
    calls: list[str] = []

    events = await collect(compiled(nodes, edges, with_call_log(default_registry(), calls)))

    assert calls == ["merge:None"]
    assert finished(events).output == ["short", "long"]


async def test_merge_input_order_follows_edges_not_completion_order() -> None:
    nodes = [
        node("input", "text_input"),
        node("slow", "fake_model", response="slow", token_delay_seconds=0.05),
        node("fast", "fake_model", response="fast"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "slow"),
        edge("input", "fast"),
        to_merge("slow", "join"),
        to_merge("fast", "join"),
        from_merge("join", "output"),
    ]

    assert finished(await collect(compiled(nodes, edges))).output == ["slow", "fast"]


async def test_merge_with_every_input_skipped_is_itself_skipped_and_the_run_ends() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("a", "fake_model", response="a"),
        node("b", "fake_model", response="b"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "a", "true"),
        edge("if", "b", "true"),
        to_merge("a", "join"),
        to_merge("b", "join"),
        from_merge("join", "output"),
    ]

    events = await collect(compiled(nodes, edges))

    statuses = final_statuses(events)
    assert statuses["join"] is SKIPPED and statuses["output"] is SKIPPED
    assert finished(events).status is RunStatus.COMPLETED
    assert finished(events).output is None


async def test_both_ports_of_a_branch_wired_straight_into_one_merge() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="hello"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        to_merge("if", "join", "true"),
        to_merge("if", "join", "false"),
        from_merge("join", "output"),
    ]

    assert finished(await collect(compiled(nodes, edges))).output == ["hello"]


async def test_nested_branches_merge_without_deadlock() -> None:
    nodes = [
        node("input", "text_input"),
        node("outer", "if_else", contains="hello"),
        node("inner", "if_else", contains="zzz"),
        node("a", "fake_model", response="a"),
        node("b", "fake_model", response="b"),
        node("c", "fake_model", response="c"),
        node("inner_join", "merge"),
        node("outer_join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "outer"),
        edge("outer", "inner", "true"),
        edge("outer", "c", "false"),
        edge("inner", "a", "true"),
        edge("inner", "b", "false"),
        to_merge("a", "inner_join"),
        to_merge("b", "inner_join"),
        to_merge("inner_join", "outer_join", "merged"),
        to_merge("c", "outer_join"),
        from_merge("outer_join", "output"),
    ]

    events = await collect(compiled(nodes, edges))

    assert finished(events).output == [["b"]]
    assert final_statuses(events)["a"] is SKIPPED
    assert final_statuses(events)["c"] is SKIPPED


async def test_node_needing_two_inputs_is_skipped_when_either_is_dead() -> None:
    async def concat(call: NodeCall) -> dict[str, Any]:
        return {"text": f"{call.inputs['left']}{call.inputs['right']}"}

    registry = {
        **default_registry(),
        "concat": NodeType(("left", "right"), ("text",), run=concat),
    }
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="hello"),
        node("live", "fake_model", response="live"),
        node("dead", "fake_model", response="dead"),
        node("concat", "concat"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "live", "true"),
        edge("if", "dead", "false"),
        edge("live", "concat", "text", "left"),
        edge("dead", "concat", "text", "right"),
    ]

    assert final_statuses(await collect(compiled(nodes, edges, registry)))["concat"] is SKIPPED


async def test_long_chain_is_not_stopped_by_the_recursion_limit() -> None:
    ids = [f"n{index}" for index in range(60)]
    nodes = [node(ids[0], "text_input"), *[node(i, "fake_model", response="x") for i in ids[1:]]]
    edges = [edge(left, right) for left, right in zip(ids, ids[1:], strict=False)]

    assert finished(await collect(compiled(nodes, edges))).status is RunStatus.COMPLETED


def random_graph(rng: random.Random) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    """A random DAG where every node has a chance of sitting on a dead path."""
    nodes = [node("n0", "text_input")]
    ports: list[tuple[str, str]] = [("n0", "text")]
    edges: list[EdgeSpec] = []
    for index in range(1, rng.randint(3, 14)):
        node_id = f"n{index}"
        kind = rng.choice(["if_else", "if_else", "merge", "merge", "pass", "concat"])
        if kind == "if_else":
            nodes.append(node(node_id, "if_else", contains=rng.choice(["x", "absent"])))
            source, port = rng.choice(ports)
            edges.append(edge(source, node_id, port))
            ports += [(node_id, "true"), (node_id, "false")]
        elif kind == "merge" and len(ports) >= 2:
            nodes.append(node(node_id, "merge"))
            count = rng.randint(2, min(4, len(ports)))
            for source, port in rng.sample(ports, count):
                edges.append(to_merge(source, node_id, port))
            ports.append((node_id, "merged"))
        elif kind == "concat" and len(ports) >= 2:
            nodes.append(node(node_id, "concat"))
            left, right = rng.sample(ports, 2)
            edges.append(edge(left[0], node_id, left[1], "left"))
            edges.append(edge(right[0], node_id, right[1], "right"))
            ports.append((node_id, "text"))
        else:
            nodes.append(node(node_id, "pass"))
            source, port = rng.choice(ports)
            edges.append(edge(source, node_id, port))
            ports.append((node_id, "text"))
    return nodes, edges


def expected_statuses(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> dict[str, StepStatus]:
    """An independent, sequential model of skip semantics to check the graph execution against."""
    by_id = {n.id: n for n in nodes}
    inbound: dict[str, list[EdgeSpec]] = {n.id: [] for n in nodes}
    for e in edges:
        inbound[e.target].append(e)
    order = TopologicalSorter({n: {e.source for e in es} for n, es in inbound.items()})

    written: dict[str, set[str]] = {}
    statuses: dict[str, StepStatus] = {}
    for node_id in order.static_order():
        live = [e for e in inbound[node_id] if e.source_port in written[e.source]]
        spec_node = by_id[node_id]
        needs_one = spec_node.type == "merge"
        runs = not inbound[node_id] or (
            bool(live) if needs_one else len(live) == len(inbound[node_id])
        )
        statuses[node_id] = COMPLETED if runs else SKIPPED
        if not runs:
            written[node_id] = set()
        elif spec_node.type == "if_else":
            written[node_id] = {"true" if spec_node.settings["contains"] == "x" else "false"}
        elif spec_node.type == "merge":
            written[node_id] = {"merged"}
        else:
            written[node_id] = {"text"}
    return statuses


async def test_random_graphs_never_deadlock_and_match_the_reference_model() -> None:
    join_calls: list[str] = []

    async def pass_through(call: NodeCall) -> dict[str, Any]:
        return {"text": "x"}

    async def concat(call: NodeCall) -> dict[str, Any]:
        return {"text": "x"}

    registry = with_call_log(default_registry(), join_calls) | {
        "pass": NodeType(("text",), ("text",), run=pass_through),
        "concat": NodeType(("left", "right"), ("text",), run=concat),
    }
    assert registry["merge"].kind is NodeKind.MERGE

    for seed in range(300):
        nodes, edges = random_graph(random.Random(seed))
        expected = expected_statuses(nodes, edges)
        join_calls.clear()

        events = await collect(compiled(nodes, edges, registry), "x")

        assert finished(events).status is RunStatus.COMPLETED, f"seed {seed}"
        assert final_statuses(events) == expected, f"seed {seed}"
        merges_run = [n.id for n in nodes if n.type == "merge" and expected[n.id] is COMPLETED]
        assert len(join_calls) == len(merges_run), f"seed {seed}: a merge fired twice"


@pytest.mark.parametrize(
    ("nodes", "edges", "blamed_node"),
    [
        (
            [node("a", "fake_model", response="x"), node("b", "fake_model", response="y")],
            [edge("a", "b"), edge("b", "a")],
            None,
        ),
        (
            [node("a", "text_input"), node("b", "text_input"), node("c", "text_output")],
            [edge("a", "c"), edge("b", "c")],
            "c",
        ),
        ([node("a", "if_else", ContinueWithNull(), contains="x")], [], "a"),
        (
            [node("i", "text_input"), node("a", "text_output"), node("b", "text_output")],
            [edge("i", "a"), edge("i", "b")],
            "b",
        ),
        ([node("a", "text_output")], [], "a"),
        ([node("a", "merge")], [], "a"),
        ([node("a", "no_such_type")], [], "a"),
        ([node("a", "text_input"), node("b", "text_output")], [edge("a", "b", "nope")], "a"),
    ],
    ids=[
        "cycle",
        "fan-in-without-merge",
        "null-on-branch",
        "two-outputs",
        "unconnected-required-port",
        "merge-without-inputs",
        "type",
        "port",
    ],
)
def test_compile_rejects_graphs_the_semantics_cannot_run(
    nodes: list[NodeSpec], edges: list[EdgeSpec], blamed_node: str | None
) -> None:
    with pytest.raises(GraphInvalid) as caught:
        compiled(nodes, edges)

    if blamed_node is not None:
        assert caught.value.node_id == blamed_node


@pytest.mark.parametrize("bad_id", ["__start__", "__end__", "a:b", "a|b", "a b", ""])
def test_node_ids_reject_what_langgraph_reserves(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        NodeSpec(id=bad_id, type="text_input")

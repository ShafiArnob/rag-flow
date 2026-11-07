"""Only the Output-wired model reaches the client, and tokens always agree with the output."""

import asyncio

from runner_spike.events import RunStatus, StartRun, StepEvent, StepStatus, TokenEvent
from runner_spike.nodes import default_registry
from runner_spike.runner import stream_run
from tests.helpers import (
    GENEROUS_LIMITS,
    collect,
    compiled,
    edge,
    finished,
    from_merge,
    node,
    to_merge,
    token_text,
    tokens,
    with_counted_tokens,
)


async def test_only_the_output_wired_models_tokens_reach_the_client() -> None:
    produced: list[str] = []
    registry = with_counted_tokens(default_registry(), produced)
    nodes = [
        node("input", "text_input"),
        node("rewriter", "fake_model", response="internal rewrite"),
        node("answer", "fake_model", response="the final answer"),
        node("output", "text_output"),
    ]
    edges = [edge("input", "rewriter"), edge("rewriter", "answer"), edge("answer", "output")]

    events = await collect(compiled(nodes, edges, registry))

    assert "internal " in produced, "the upstream model must really have streamed"
    assert {token.node_id for token in tokens(events)} == {"answer"}
    assert token_text(events) == "the final answer"
    assert finished(events).output == "the final answer"


async def test_two_models_behind_a_branch_stream_only_the_taken_one() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="hello"),
        node("yes", "fake_model", response="yes answer"),
        node("no", "fake_model", response="no answer"),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "yes", "true"),
        edge("if", "no", "false"),
        to_merge("yes", "join"),
        to_merge("no", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow, "hello")

    assert workflow.stream_sources == {"yes", "no"}
    assert len(tokens(events)) > 1, "a branch must not cost us live streaming"
    assert {token.node_id for token in tokens(events)} == {"yes"}
    assert token_text(events) == finished(events).output == "yes answer"


async def test_tokens_arrive_while_the_model_runs_not_after() -> None:
    nodes = [
        node("input", "text_input"),
        node("answer", "fake_model", response="a b c", token_delay_seconds=0.03),
        node("output", "text_output"),
    ]
    edges = [edge("input", "answer"), edge("answer", "output")]

    events = await collect(compiled(nodes, edges))

    answer_steps = [i for i, e in enumerate(events) if getattr(e, "node_id", "") == "answer"]
    statuses = [events[i] for i in answer_steps]
    assert isinstance(statuses[0], StepEvent) and statuses[0].status is StepStatus.RUNNING
    assert isinstance(statuses[-1], StepEvent) and statuses[-1].status is StepStatus.COMPLETED
    assert any(isinstance(event, TokenEvent) for event in statuses[1:-1])


async def test_parallel_models_into_a_merge_never_interleave() -> None:
    """Both run in one execution, so live tokens would be a mix of the two answers."""
    nodes = [
        node("input", "text_input"),
        node("a", "fake_model", response="a1 a2 a3", token_delay_seconds=0.01),
        node("b", "fake_model", response="b1 b2 b3", token_delay_seconds=0.01),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "a"),
        edge("input", "b"),
        to_merge("a", "join"),
        to_merge("b", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow)

    assert workflow.stream_sources == frozenset()
    assert [token.text for token in tokens(events)] == ["a1 a2 a3"]
    assert finished(events).output == "a1 a2 a3"


async def test_model_racing_a_plain_path_into_a_merge_does_not_stream() -> None:
    nodes = [
        node("input", "text_input"),
        node("model", "fake_model", response="from model"),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        to_merge("input", "join"),
        edge("input", "model"),
        to_merge("model", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow, "from input")

    assert workflow.stream_sources == frozenset()
    assert token_text(events) == finished(events).output == "from input"


async def test_taken_path_without_a_model_still_delivers_the_text_as_a_token() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="canned"),
        node("model", "fake_model", response="generated"),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        to_merge("if", "join", "true"),
        edge("if", "model", "false"),
        to_merge("model", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow, "canned reply")

    assert workflow.stream_sources == {"model"}
    assert token_text(events) == finished(events).output == "canned reply"


async def test_model_after_a_merge_of_both_branches_is_not_exclusive_with_either_branch() -> None:
    """`after_join` runs whichever way the branch goes, so it can run alongside `on_true`."""
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="hello"),
        node("a", "fake_model", response="a"),
        node("b", "fake_model", response="b"),
        node("rejoin", "merge"),
        node("after_join", "fake_model", response="x1 x2", token_delay_seconds=0.01),
        node("on_true", "fake_model", response="y1 y2", token_delay_seconds=0.01),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "a", "true"),
        edge("if", "b", "false"),
        to_merge("a", "rejoin"),
        to_merge("b", "rejoin"),
        from_merge("rejoin", "after_join"),
        edge("if", "on_true", "true"),
        to_merge("after_join", "join"),
        to_merge("on_true", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow, "hello")

    assert workflow.stream_sources == frozenset()
    assert token_text(events) == finished(events).output == "x1 x2"


async def test_list_merge_is_not_stream_transparent() -> None:
    nodes = [
        node("input", "text_input"),
        node("model", "fake_model", response="answer"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [edge("input", "model"), to_merge("model", "join"), from_merge("join", "output")]

    assert compiled(nodes, edges).stream_sources == frozenset()


async def test_graph_without_an_output_node_streams_nothing() -> None:
    nodes = [node("input", "text_input"), node("model", "fake_model", response="ignored")]

    events = await collect(compiled(nodes, [edge("input", "model")]))

    assert tokens(events) == []
    assert finished(events).status is RunStatus.COMPLETED


async def test_one_compiled_graph_serves_concurrent_runs_without_crosstalk() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="left"),
        node("l", "fake_model", response="left answer", token_delay_seconds=0.005),
        node("r", "fake_model", response="right answer", token_delay_seconds=0.005),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "l", "true"),
        edge("if", "r", "false"),
        to_merge("l", "join"),
        to_merge("r", "join"),
        from_merge("join", "output"),
    ]
    workflow = compiled(nodes, edges)

    runs = await asyncio.gather(
        *[collect(workflow, "left" if i % 2 == 0 else "right") for i in range(10)]
    )

    for index, events in enumerate(runs):
        expected = "left answer" if index % 2 == 0 else "right answer"
        assert token_text(events) == finished(events).output == expected


async def test_closing_the_stream_aborts_the_model() -> None:
    """A client disconnect closes this generator; the model must stop burning tokens."""
    produced: list[str] = []
    registry = with_counted_tokens(default_registry(), produced)
    nodes = [
        node("input", "text_input"),
        node(
            "answer",
            "fake_model",
            response=" ".join(str(i) for i in range(1000)),
            token_delay_seconds=0.002,
        ),
        node("output", "text_output"),
    ]
    edges = [edge("input", "answer"), edge("answer", "output")]

    stream = stream_run(compiled(nodes, edges, registry), StartRun("hi"), GENEROUS_LIMITS)
    async for event in stream:
        if isinstance(event, TokenEvent):
            break
    await stream.aclose()

    seen_at_close = len(produced)
    await asyncio.sleep(0.05)
    assert len(produced) == seen_at_close
    assert seen_at_close < 1000

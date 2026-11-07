"""Each test reproduces a defect found in the independent contract review."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import RunControl
from langgraph.types import interrupt
from pydantic import ValidationError

from runner_spike.compiler import CompiledWorkflow, GraphInvalid, compile_workflow
from runner_spike.events import (
    ErrorCode,
    ProgressEvent,
    RecoverRun,
    ResumeRun,
    RunEvent,
    RunLimits,
    RunStatus,
    StartRun,
    StepEvent,
    StepStatus,
    TokenEvent,
)
from runner_spike.fake_model import FakeStreamingChatModel
from runner_spike.nodes import NodeCall, NodeCancelled, NodeKind, NodeType, default_registry
from runner_spike.runner import RunStateConflict, stream_run
from runner_spike.spec import ContinueWithNull, EdgeSpec, NodeSpec, Retry
from tests.helpers import (
    GENEROUS_LIMITS,
    collect,
    compiled,
    edge,
    final_statuses,
    finished,
    from_merge,
    node,
    resume_all,
    spec,
    started_nodes,
    to_merge,
    token_text,
    tokens,
)


def registry_with(**types: NodeType) -> dict[str, NodeType]:
    return {**default_registry(), **types}


def stream_type(handler: Any) -> NodeType:
    return NodeType(("text",), ("text",), stream=handler)


def run_type(handler: Any, **fields: Any) -> NodeType:
    return NodeType(("text",), ("text",), run=handler, **fields)


def through(model: NodeSpec) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    nodes = [node("input", "text_input"), model, node("output", "text_output")]
    return nodes, [edge("input", model.id), edge(model.id, "output")]


# Streaming: one source of truth for client text


async def test_output_with_a_chunks_input_still_streams_live() -> None:
    nodes = [
        node("input", "text_input"),
        node("retriever", "fake_model", response="chunk text"),
        node("answer", "fake_model", response="the answer"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "retriever"),
        edge("retriever", "answer"),
        edge("answer", "output"),
        edge("retriever", "output", "text", "chunks"),
    ]

    workflow = compiled(nodes, edges)
    events = await collect(workflow)

    assert workflow.stream_sources == {"answer"}
    assert len(tokens(events)) > 1
    assert token_text(events) == finished(events).output == "the answer"


async def test_dead_optional_input_does_not_skip_the_node() -> None:
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("retriever", "fake_model", response="chunks"),
        node("direct", "fake_model", response="direct answer"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("if", "retriever", "true"),
        edge("input", "direct"),
        edge("direct", "output"),
        edge("retriever", "output", "text", "chunks"),
    ]

    events = await collect(compiled(nodes, edges))

    assert final_statuses(events)["output"] is StepStatus.COMPLETED
    assert finished(events).output == "direct answer"


async def test_node_with_only_dead_optional_inputs_is_skipped() -> None:
    async def lookup(call: NodeCall) -> dict[str, Any]:
        return {"text": "ran"}

    registry = registry_with(
        lookup=NodeType(("hint",), ("text",), run=lookup, optional_input_ports=("hint",))
    )
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("lookup", "lookup"),
    ]
    edges = [edge("input", "if"), edge("if", "lookup", "true", "hint")]

    assert final_statuses(await collect(compiled(nodes, edges, registry)))["lookup"] is (
        StepStatus.SKIPPED
    )


async def test_internal_model_call_inside_stream_does_not_reach_the_client() -> None:
    async def rewrite_then_answer(call: NodeCall) -> AsyncIterator[str]:
        await FakeStreamingChatModel(response="SECRET-REWRITE").ainvoke("rewrite this")
        yield "final answer"

    registry = registry_with(rag=stream_type(rewrite_then_answer))

    events = await collect(compiled(*through(node("rag", "rag")), registry))

    assert token_text(events) == finished(events).output == "final answer"


async def test_handler_that_buffers_model_tokens_can_retry_without_duplicates() -> None:
    attempts: list[int] = []

    async def buffered(call: NodeCall) -> AsyncIterator[str]:
        attempts.append(call.attempt)
        text = ""
        async for chunk in FakeStreamingChatModel(response="one two three").astream("x"):
            text += chunk.text
        if call.attempt == 1:
            raise RuntimeError("failed before yielding anything")
        yield text

    registry = registry_with(buffered=stream_type(buffered))
    model = node("b", "buffered", Retry(max_retries=1, backoff_seconds=0.001))

    events = await collect(compiled(*through(model), registry))

    assert attempts == [1, 2]
    assert token_text(events) == finished(events).output == "one two three"


async def test_empty_first_chunk_does_not_count_as_a_sent_token() -> None:
    async def empty_then_fail(call: NodeCall) -> AsyncIterator[str]:
        yield ""
        if call.attempt == 1:
            raise RuntimeError("transient")
        yield "recovered"

    registry = registry_with(flaky=stream_type(empty_then_fail))
    model = node("f", "flaky", Retry(max_retries=1, backoff_seconds=0.001))

    events = await collect(compiled(*through(model), registry))

    assert finished(events).status is RunStatus.COMPLETED
    assert token_text(events) == "recovered"


async def test_stream_handler_that_never_touches_langchain_streams_live() -> None:
    async def plain_generator(call: NodeCall) -> AsyncIterator[str]:
        yield "a "
        yield "b"

    registry = registry_with(plain=stream_type(plain_generator))

    events = await collect(compiled(*through(node("p", "plain")), registry))

    assert [token.text for token in tokens(events)] == ["a ", "b"]


async def test_non_text_final_output_is_not_stringified_into_a_token() -> None:
    nodes = [
        node("input", "text_input"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [to_merge("input", "join"), from_merge("join", "output")]

    events = await collect(compiled(nodes, edges), "a")

    assert tokens(events) == []
    assert finished(events).output == ["a"]


async def test_output_that_rewrites_streamed_text_fails_the_run_visibly() -> None:
    async def strip_marker(call: NodeCall) -> dict[str, Any]:
        return {"text": call.inputs["text"].replace(" [7]", "")}

    registry = registry_with(
        rewriting_output=NodeType(("text",), ("text",), run=strip_marker, kind=NodeKind.OUTPUT)
    )
    nodes = [
        node("input", "text_input"),
        node("model", "fake_model", response="answer [7] done"),
        node("output", "rewriting_output"),
    ]

    events = await collect(
        compiled(nodes, [edge("input", "model"), edge("model", "output")], registry)
    )

    result = finished(events)
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code is ErrorCode.INTERNAL_ERROR


def test_continue_with_null_after_a_stream_source_is_rejected() -> None:
    nodes = [
        node("input", "text_input"),
        node("model", "fake_model", response="x"),
        node("output", "text_output", ContinueWithNull()),
    ]

    with pytest.raises(GraphInvalid) as caught:
        compiled(nodes, [edge("input", "model"), edge("model", "output")])

    assert caught.value.node_id == "output"


# Handlers that break the node contract


async def test_handler_omitting_a_port_fails_instead_of_silently_skipping_downstream() -> None:
    async def forgetful(call: NodeCall) -> dict[str, Any]:
        return {}

    registry = registry_with(forgetful=run_type(forgetful))

    events = await collect(compiled(*through(node("f", "forgetful")), registry))

    assert finished(events).status is RunStatus.FAILED
    assert "expected ports" in (finished(events).error.message)  # type: ignore[union-attr]


@pytest.mark.parametrize("written", [{}, {"true": 1, "false": 2}, {"nope": 1}])
async def test_branch_must_write_exactly_one_declared_port(written: dict[str, Any]) -> None:
    async def bad_branch(call: NodeCall) -> dict[str, Any]:
        return written

    registry = registry_with(
        bad=NodeType(("text",), ("true", "false"), run=bad_branch, kind=NodeKind.BRANCH)
    )
    nodes = [node("input", "text_input"), node("b", "bad")]

    events = await collect(compiled(nodes, [edge("input", "b")], registry))

    assert finished(events).status is RunStatus.FAILED


async def test_output_that_is_not_json_fails_the_node() -> None:
    async def returns_a_set(call: NodeCall) -> dict[str, Any]:
        return {"text": {1, 2}}

    registry = registry_with(bad=run_type(returns_a_set))

    events = await collect(compiled(*through(node("b", "bad")), registry))

    assert finished(events).status is RunStatus.FAILED
    assert final_statuses(events)["b"] is StepStatus.FAILED


async def test_node_not_declared_as_parking_cannot_park() -> None:
    async def sneaky(call: NodeCall) -> dict[str, Any]:
        return {"text": interrupt("wait")}

    registry = registry_with(sneaky=run_type(sneaky))

    events = await collect(compiled(*through(node("s", "sneaky")), registry))

    assert finished(events).status is RunStatus.FAILED


def test_retries_are_capped() -> None:
    with pytest.raises(ValidationError):
        Retry(max_retries=11, backoff_seconds=1)


def test_node_spec_carries_a_version() -> None:
    assert NodeSpec(id="a", type="text_input", version=2).version == 2


# The run always ends visibly


async def test_unexpected_runner_error_still_ends_the_run() -> None:
    class BrokenSaver(InMemorySaver):
        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            raise OSError("database went away")

    workflow = compile_workflow(
        spec(*through(node("m", "fake_model", response="x"))),
        default_registry(),
        checkpointer=BrokenSaver(),
    )

    events = await collect(workflow, run_id="run-1")

    result = finished(events)
    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code is ErrorCode.INTERNAL_ERROR
    assert "database went away" in result.error.message


# Cancel


async def test_cancel_during_a_retry_does_not_kill_a_parallel_sibling() -> None:
    finished_siblings: list[str] = []

    async def slow_sibling(call: NodeCall) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        finished_siblings.append("done")
        return {"text": "ok"}

    registry = registry_with(sibling=run_type(slow_sibling))
    nodes = [
        node("input", "text_input"),
        node(
            "flaky",
            "fake_model",
            Retry(max_retries=3, backoff_seconds=0.001),
            response="x",
            failure_rate=1.0,
        ),
        node("sibling", "sibling"),
    ]
    workflow = compiled(nodes, [edge("input", "flaky"), edge("input", "sibling")], registry)

    control = RunControl()
    events: list[RunEvent] = []
    async for event in stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, control=control):
        events.append(event)
        if isinstance(event, StepEvent) and event.status is StepStatus.RETRYING:
            control.request_drain("cancel")

    assert finished_siblings == ["done"]
    assert final_statuses(events)["sibling"] is StepStatus.COMPLETED
    assert final_statuses(events)["flaky"] is StepStatus.CANCELLED
    assert finished(events).status is RunStatus.CANCELLED


async def test_long_handler_can_observe_cancel_and_report_progress() -> None:
    """P02_S10: cancel between documents, with cycles banned, needs both of these."""

    async def batch(call: NodeCall) -> dict[str, Any]:
        for done in range(1, 100):
            await asyncio.sleep(0.001)
            await call.report_progress({"documents_done": done, "documents_total": 99})
            if call.is_cancel_requested():
                raise NodeCancelled
        return {"text": "all done"}

    registry = registry_with(batch=run_type(batch))
    nodes = [node("input", "text_input"), node("b", "batch"), node("after", "batch")]
    workflow = compiled(nodes, [edge("input", "b"), edge("b", "after")], registry)

    control = RunControl()
    events: list[RunEvent] = []
    async for event in stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, control=control):
        events.append(event)
        if isinstance(event, ProgressEvent) and event.data["documents_done"] == 3:
            control.request_drain("cancel")

    progress = [event for event in events if isinstance(event, ProgressEvent)]
    assert 3 <= len(progress) < 99
    assert final_statuses(events)["b"] is StepStatus.CANCELLED
    assert finished(events).status is RunStatus.CANCELLED
    assert "after" not in started_nodes(events)


# Park, resume and recover


def park_registry(entries: list[str]) -> dict[str, NodeType]:
    async def park(call: NodeCall) -> dict[str, Any]:
        entries.append(call.settings["label"])
        answer = interrupt({"waiting_for": call.settings["label"]})
        if answer == "boom":
            raise RuntimeError("callback reported failure")
        return {"text": answer}

    return registry_with(park=run_type(park, can_park=lambda _settings: True))


def compiled_with_saver(
    nodes: list[NodeSpec], edges: list[EdgeSpec], registry: dict[str, NodeType]
) -> CompiledWorkflow:
    return compile_workflow(spec(nodes, edges), registry, checkpointer=InMemorySaver())


def two_parks() -> tuple[list[NodeSpec], list[EdgeSpec]]:
    nodes = [
        node("input", "text_input"),
        node("a", "park", label="a"),
        node("b", "park", label="b"),
        node("join", "merge"),
    ]
    edges = [
        edge("input", "a"),
        edge("input", "b"),
        to_merge("a", "join"),
        to_merge("b", "join"),
    ]
    return nodes, edges


async def test_parallel_parks_resume_one_callback_at_a_time_and_complete_once() -> None:
    entries: list[str] = []
    workflow = compiled_with_saver(*two_parks(), park_registry(entries))

    first = await collect(workflow, run_id="r")
    parked = {p.node_id: p.interrupt_id for p in finished(first).parked}
    assert sorted(parked) == ["a", "b"]

    second = await collect(workflow, run_id="r", mode=ResumeRun({parked["a"]: "from a"}))
    assert finished(second).status is RunStatus.PARKED
    assert [p.node_id for p in finished(second).parked] == ["b"]
    assert finished(second).parked[0].interrupt_id == parked["b"], "a callback token stays valid"

    third = await collect(workflow, run_id="r", mode=ResumeRun({parked["b"]: "from b"}))

    assert finished(third).status is RunStatus.COMPLETED
    assert final_statuses(third)["join"] is StepStatus.COMPLETED
    assert entries.count("a") >= 2 and entries.count("b") >= 2, "parked nodes re-enter on resume"


def test_retry_on_a_parking_node_is_rejected() -> None:
    nodes = [
        node("input", "text_input"),
        node("p", "park", Retry(max_retries=1, backoff_seconds=0), label="p"),
    ]

    with pytest.raises(GraphInvalid) as caught:
        compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))

    assert caught.value.node_id == "p"


def test_parking_node_without_a_checkpointer_is_rejected() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]

    with pytest.raises(GraphInvalid) as caught:
        compiled(nodes, [edge("input", "p")], park_registry([]))

    assert caught.value.node_id == "p"


async def test_failure_after_resume_applies_the_nodes_policy_without_parking_again() -> None:
    """P03_S05: a park timeout resumes the node, which fails under its own policy."""
    nodes = [
        node("input", "text_input"),
        node("p", "park", ContinueWithNull(), label="p"),
        node("after", "fake_model", response="continued"),
    ]
    workflow = compiled_with_saver(
        nodes, [edge("input", "p"), edge("p", "after")], park_registry([])
    )

    first = await collect(workflow, run_id="r")
    second = await collect(workflow, run_id="r", mode=resume_all(first, "boom"))

    assert finished(second).status is RunStatus.COMPLETED
    assert final_statuses(second)["p"] is StepStatus.FAILED
    assert final_statuses(second)["after"] is StepStatus.COMPLETED


async def test_sibling_failure_closes_a_parked_step() -> None:
    async def bad(call: NodeCall) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        raise RuntimeError("sibling failed")

    registry = {**park_registry([]), "bad": run_type(bad)}
    nodes = [node("input", "text_input"), node("p", "park", label="p"), node("bad", "bad")]
    workflow = compiled_with_saver(nodes, [edge("input", "p"), edge("input", "bad")], registry)

    events = await collect(workflow, run_id="r")

    assert finished(events).status is RunStatus.FAILED
    assert final_statuses(events)["p"] is StepStatus.CANCELLED


async def test_cancel_in_the_superstep_of_a_park_cancels_the_run() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]
    workflow = compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))

    control = RunControl()
    events: list[RunEvent] = []
    stream = stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, control=control, run_id="r")
    async for event in stream:
        events.append(event)
        if isinstance(event, StepEvent) and event.node_id == "p":
            control.request_drain("cancel")

    assert finished(events).status is RunStatus.CANCELLED
    assert final_statuses(events)["p"] is StepStatus.CANCELLED
    assert finished(events).parked == ()


async def test_resuming_a_finished_run_is_refused() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]
    workflow = compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))
    first = await collect(workflow, run_id="r")
    mode = resume_all(first, "ok")
    await collect(workflow, run_id="r", mode=mode)

    with pytest.raises(RunStateConflict):
        await collect(workflow, run_id="r", mode=mode)


async def test_starting_a_used_run_id_is_refused() -> None:
    workflow = compiled_with_saver([node("input", "text_input")], [], default_registry())
    await collect(workflow, run_id="r")

    with pytest.raises(RunStateConflict):
        await collect(workflow, run_id="r")


async def test_recovery_after_a_crash_does_not_repeat_finished_nodes() -> None:
    entered: list[str] = []

    async def counted(call: NodeCall) -> dict[str, Any]:
        entered.append(call.settings["label"])
        await asyncio.sleep(call.settings.get("seconds", 0))
        return {"text": call.settings["label"]}

    registry = registry_with(counted=run_type(counted))
    nodes = [
        node("input", "text_input"),
        node("first", "counted", label="first"),
        node("second", "counted", label="second", seconds=0.05),
        node("third", "counted", label="third"),
    ]
    edges = [edge("input", "first"), edge("first", "second"), edge("second", "third")]
    workflow = compiled_with_saver(nodes, edges, registry)

    crashed = stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, run_id="r")
    async for event in crashed:
        if isinstance(event, StepEvent) and event.node_id == "second":
            break
    await crashed.aclose()

    events = await collect(workflow, run_id="r", mode=RecoverRun())

    assert finished(events).status is RunStatus.COMPLETED
    assert entered == ["first", "second", "second", "third"], "only the in-flight node repeats"


async def test_recovery_keeps_counting_steps() -> None:
    nodes = [
        node("input", "text_input"),
        node("slow", "fake_model", response="a b c", token_delay_seconds=0.02),
        node("after", "fake_model", response="x"),
    ]
    workflow = compiled_with_saver(
        nodes, [edge("input", "slow"), edge("slow", "after")], default_registry()
    )
    limits = RunLimits(timeout_seconds=5, max_steps=2)

    crashed = stream_run(workflow, StartRun("x"), limits, run_id="r")
    async for event in crashed:
        if isinstance(event, StepEvent) and event.node_id == "slow":
            break
    await crashed.aclose()

    recovered = await collect(workflow, limits=limits, run_id="r", mode=RecoverRun())

    result = finished(recovered)
    assert result.error is not None and result.error.code is ErrorCode.MAX_STEPS_EXCEEDED
    assert result.error.node_id == "after", "input and slow were already counted before the crash"


async def test_recovering_a_parked_run_parks_it_again_on_the_same_interrupts() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]
    workflow = compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))
    first = await collect(workflow, run_id="r")

    recovered = await collect(workflow, run_id="r", mode=RecoverRun())

    assert finished(recovered).status is RunStatus.PARKED
    assert finished(recovered).parked == finished(first).parked


async def test_recovering_a_finished_run_is_refused() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]
    workflow = compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))
    first = await collect(workflow, run_id="r")
    await collect(workflow, run_id="r", mode=resume_all(first, "ok"))

    with pytest.raises(RunStateConflict):
        await collect(workflow, run_id="r", mode=RecoverRun())


async def test_token_event_is_the_only_path_to_the_client() -> None:
    """A stream source's tokens arrive as custom events in order with its step events."""
    events = await collect(compiled(*through(node("m", "fake_model", response="a b"))))

    for_model = [e for e in events if getattr(e, "node_id", None) == "m"]

    assert isinstance(for_model[0], StepEvent)
    assert isinstance(for_model[1], TokenEvent)
    assert isinstance(for_model[-1], StepEvent)
    assert for_model[-1].status is StepStatus.COMPLETED


# Second review round


async def test_optional_edge_gives_no_exclusivity() -> None:
    """`with_hint` runs on either branch, because only its optional input hangs off `if`."""

    async def with_hint(call: NodeCall) -> AsyncIterator[str]:
        for token in ("a ", "b"):
            await asyncio.sleep(0.01)
            yield token

    registry = registry_with(
        hinted=NodeType(
            ("text", "hint"), ("text",), stream=with_hint, optional_input_ports=("hint",)
        )
    )
    nodes = [
        node("input", "text_input"),
        node("if", "if_else", contains="zzz"),
        node("hinted", "hinted"),
        node("other", "fake_model", response="x y", token_delay_seconds=0.01),
        node("join", "merge", combine="first_non_null"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "if"),
        edge("input", "hinted"),
        edge("if", "hinted", "true", "hint"),
        edge("if", "other", "false"),
        to_merge("hinted", "join"),
        to_merge("other", "join"),
        from_merge("join", "output"),
    ]

    workflow = compiled(nodes, edges, registry)
    events = await collect(workflow)

    assert workflow.stream_sources == frozenset()
    assert finished(events).status is RunStatus.COMPLETED
    assert token_text(events) == finished(events).output == "a b"


def output_beside_a_park(model: NodeSpec) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    nodes = [node("input", "text_input"), model, node("output", "text_output")]
    nodes.append(node("p", "park", label="p"))
    return nodes, [edge("input", model.id), edge(model.id, "output"), edge("input", "p")]


async def test_output_finished_before_a_park_is_delivered_once_on_completion() -> None:
    model = node("m", "fake_model", response="the answer")
    workflow = compiled_with_saver(*output_beside_a_park(model), park_registry([]))

    first = await collect(workflow, run_id="r")
    second = await collect(workflow, run_id="r", mode=resume_all(first, "ok"))

    assert tokens(first) == []
    assert [token.text for token in tokens(second)] == ["the answer"]
    assert finished(second).output == "the answer"


async def test_recovered_run_delivers_its_text_once() -> None:
    nodes, edges = through(node("m", "fake_model", response="a b c", token_delay_seconds=0.02))
    workflow = compiled_with_saver(nodes, edges, default_registry())

    crashed = stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, run_id="r")
    seen: list[RunEvent] = []
    async for event in crashed:
        seen.append(event)
        if isinstance(event, StepEvent) and event.node_id == "m":
            break
    await crashed.aclose()
    recovered = await collect(workflow, run_id="r", mode=RecoverRun())

    assert tokens(seen) == []
    assert token_text(recovered) == finished(recovered).output == "a b c"


async def test_recovery_after_a_quiet_cancel_still_reports_cancelled() -> None:
    nodes = [
        node("input", "text_input"),
        node(
            "flaky",
            "fake_model",
            Retry(max_retries=3, backoff_seconds=0.001),
            response="x",
            failure_rate=1.0,
        ),
        node("after", "fake_model", response="y"),
    ]
    workflow = compiled_with_saver(
        nodes, [edge("input", "flaky"), edge("flaky", "after")], default_registry()
    )
    control = RunControl()
    stream = stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, control=control, run_id="r")
    async for event in stream:
        if isinstance(event, StepEvent) and event.status is StepStatus.RETRYING:
            control.request_drain("cancel")

    recovered = await collect(workflow, run_id="r", mode=RecoverRun())

    assert finished(recovered).status is RunStatus.CANCELLED


async def test_crash_after_resume_loses_the_value_so_the_caller_must_redeliver_it() -> None:
    """The checkpoint does not keep a resume value, and callback tokens are single-use."""
    crash_first = asyncio.Event()

    async def park_then_work(call: NodeCall) -> dict[str, Any]:
        answer = interrupt("wait")
        if not crash_first.is_set():
            crash_first.set()
            await asyncio.sleep(30)
        return {"text": answer}

    registry = registry_with(
        park=run_type(park_then_work, can_park=lambda _settings: True),
    )
    nodes = [node("input", "text_input"), node("p", "park"), node("output", "text_output")]
    workflow = compiled_with_saver(nodes, [edge("input", "p"), edge("p", "output")], registry)
    first = await collect(workflow, run_id="r")

    crashed = stream_run(workflow, resume_all(first, "delivered"), GENEROUS_LIMITS, run_id="r")
    async for event in crashed:
        if isinstance(event, StepEvent) and event.status is StepStatus.RUNNING:
            await crash_first.wait()
            break
    await crashed.aclose()

    recovered = await collect(workflow, run_id="r", mode=RecoverRun())

    assert finished(recovered).status is RunStatus.PARKED
    assert finished(recovered).parked == finished(first).parked

    redelivered = await collect(workflow, run_id="r", mode=resume_all(recovered, "delivered"))

    assert finished(redelivered).status is RunStatus.COMPLETED
    assert finished(redelivered).output == "delivered"


async def test_crash_with_a_park_recorded_and_a_sibling_in_flight_recovers() -> None:
    entries: list[str] = []
    registry = park_registry(entries)
    nodes = [
        node("input", "text_input"),
        node("p", "park", label="p"),
        node("sibling", "fake_model", response="a b c d", token_delay_seconds=0.02),
    ]
    workflow = compiled_with_saver(nodes, [edge("input", "p"), edge("input", "sibling")], registry)

    crashed = stream_run(workflow, StartRun("x"), GENEROUS_LIMITS, run_id="r")
    async for event in crashed:
        if isinstance(event, StepEvent) and event.status is StepStatus.PARKED:
            break
    await crashed.aclose()

    recovered = await collect(workflow, run_id="r", mode=RecoverRun())

    assert finished(recovered).status is RunStatus.PARKED
    assert final_statuses(recovered)["sibling"] is StepStatus.COMPLETED


async def test_output_from_an_earlier_stream_survives_a_failed_resume() -> None:
    nodes = [
        node("input", "text_input"),
        node("output", "text_output"),
        node("p", "park", label="p"),
    ]
    edges = [edge("input", "output"), edge("input", "p")]
    workflow = compiled_with_saver(nodes, edges, park_registry([]))

    first = await collect(workflow, "hello", run_id="r")
    second = await collect(workflow, run_id="r", mode=resume_all(first, "boom"))

    assert finished(second).status is RunStatus.FAILED
    assert finished(second).output == "hello"


def http_like_registry() -> dict[str, NodeType]:
    async def http(call: NodeCall) -> dict[str, Any]:
        if call.settings["mode"] == "webhook":
            return {"text": interrupt("wait for callback")}
        return {"text": "sync response"}

    def parks(settings: Any) -> bool:
        return bool(settings["mode"] == "webhook")

    return registry_with(http=run_type(http, can_park=parks))


def test_parking_is_decided_per_node_by_its_settings() -> None:
    retry = Retry(max_retries=1, backoff_seconds=0)
    sync = node("h", "http", retry, mode="sync")
    webhook = node("h", "http", mode="webhook")
    registry = http_like_registry()

    compiled([node("input", "text_input"), sync], [edge("input", "h")], registry)

    with pytest.raises(GraphInvalid):
        compiled([node("input", "text_input"), webhook], [edge("input", "h")], registry)


async def test_resume_with_cancel_already_requested_closes_the_parked_step() -> None:
    nodes = [node("input", "text_input"), node("p", "park", label="p")]
    workflow = compiled_with_saver(nodes, [edge("input", "p")], park_registry([]))
    first = await collect(workflow, run_id="r")
    control = RunControl()
    control.request_drain("cancel")

    events = await collect(workflow, run_id="r", mode=resume_all(first, "ok"), control=control)

    assert finished(events).status is RunStatus.CANCELLED
    assert final_statuses(events)["p"] is StepStatus.CANCELLED


async def test_a_store_timeout_is_not_reported_as_the_run_timing_out() -> None:
    class SlowSaver(InMemorySaver):
        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("store timed out")

    workflow = compile_workflow(
        spec(*through(node("m", "fake_model", response="x"))),
        default_registry(),
        checkpointer=SlowSaver(),
    )

    result = finished(await collect(workflow, run_id="r"))

    assert result.status is RunStatus.FAILED
    assert result.error is not None and result.error.code is ErrorCode.INTERNAL_ERROR


async def test_nan_output_fails_the_node() -> None:
    async def not_a_number(call: NodeCall) -> dict[str, Any]:
        return {"text": float("nan")}

    registry = registry_with(nan=run_type(not_a_number))

    events = await collect(compiled(*through(node("n", "nan")), registry))

    assert final_statuses(events)["n"] is StepStatus.FAILED


def test_stream_node_types_have_one_output_port() -> None:
    async def handler(call: NodeCall) -> AsyncIterator[str]:
        yield "x"

    with pytest.raises(ValueError, match="one output port"):
        NodeType(("text",), ("a", "b"), stream=handler)

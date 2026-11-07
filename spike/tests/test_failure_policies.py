"""fail_run, continue_with_null and retry, including what happens after tokens have gone out."""

import random

import pytest

from runner_spike.events import ErrorCode, RunStatus, StepStatus
from runner_spike.execution import ContinueNull, FailTheRun, RetryAfter, decide_on_failure
from runner_spike.spec import ContinueWithNull, EdgeSpec, FailRun, NodeSpec, Retry
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
    token_text,
)


def seed_failing_first_attempts(failure_rate: float, failures: int) -> int:
    """A seed whose first `failures` attempts fail and whose next attempt succeeds.

    The model node rolls `seed + attempt`, with attempts counted from 1.
    """

    def fails(seed: int) -> bool:
        return random.Random(seed).random() < failure_rate

    for seed in range(100_000):
        attempts = [fails(seed + attempt) for attempt in range(1, failures + 2)]
        if attempts == [True] * failures + [False]:
            return seed
    raise AssertionError("no seed found")


def through_model(model: NodeSpec) -> tuple[list[NodeSpec], list[EdgeSpec]]:
    nodes = [node("input", "text_input"), model, node("output", "text_output")]
    return nodes, [edge("input", model.id), edge(model.id, "output")]


async def test_fail_run_stops_the_run_and_names_the_node() -> None:
    model = node("model", "fake_model", response="x", failure_rate=1.0)

    events = await collect(compiled(*through_model(model)))

    result = finished(events)
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code is ErrorCode.NODE_FAILED
    assert result.error.node_id == "model"
    assert "FakeModelError" in result.error.message
    assert final_statuses(events)["model"] is StepStatus.FAILED
    assert "output" not in started_nodes(events)


async def test_fail_run_cancels_a_parallel_sibling_and_closes_its_step() -> None:
    nodes = [
        node("input", "text_input"),
        node("doomed", "fake_model", response="x", failure_rate=1.0),
        node("sibling", "fake_model", response=" ".join("x" * 500), token_delay_seconds=0.01),
    ]

    events = await collect(compiled(nodes, [edge("input", "doomed"), edge("input", "sibling")]))

    assert finished(events).status is RunStatus.FAILED
    assert final_statuses(events)["sibling"] is StepStatus.CANCELLED


async def test_continue_with_null_marks_the_step_failed_and_passes_null_on() -> None:
    nodes = [
        node("input", "text_input"),
        node("bad", "fake_model", ContinueWithNull(), response="x", failure_rate=1.0),
        node("good", "fake_model", response="ok"),
        node("join", "merge"),
        node("output", "text_output"),
    ]
    edges = [
        edge("input", "bad"),
        edge("input", "good"),
        to_merge("bad", "join"),
        to_merge("good", "join"),
        from_merge("join", "output"),
    ]

    events = await collect(compiled(nodes, edges))

    assert final_statuses(events)["bad"] is StepStatus.FAILED
    assert finished(events).status is RunStatus.COMPLETED
    assert finished(events).output == [None, "ok"], "a null input is live; only skips are dropped"


async def test_continue_with_null_keeps_downstream_nodes_live() -> None:
    nodes = [
        node("input", "text_input"),
        node("bad", "fake_model", ContinueWithNull(), response="x", failure_rate=1.0),
        node("after", "fake_model", response="ran anyway"),
    ]

    events = await collect(compiled(nodes, [edge("input", "bad"), edge("bad", "after")]))

    assert final_statuses(events)["after"] is StepStatus.COMPLETED


async def test_retry_succeeds_after_failures_and_counts_one_step() -> None:
    seed = seed_failing_first_attempts(0.5, failures=2)
    model = node(
        "model",
        "fake_model",
        Retry(max_retries=3, backoff_seconds=0.001),
        response="done",
        failure_rate=0.5,
        seed=seed,
    )

    events = await collect(compiled(*through_model(model)))

    assert finished(events).status is RunStatus.COMPLETED
    assert token_text(events) == "done"
    attempts = [e.attempt for e in step_events(events) if e.node_id == "model"]
    assert attempts == [1, 1, 2, 2, 3, 3]
    assert final_statuses(events)["model"] is StepStatus.COMPLETED


async def test_retry_exhausted_fails_the_run_after_max_retries_plus_one_attempts() -> None:
    model = node(
        "model",
        "fake_model",
        Retry(max_retries=2, backoff_seconds=0.001),
        response="x",
        failure_rate=1.0,
    )

    events = await collect(compiled(*through_model(model)))

    assert finished(events).status is RunStatus.FAILED
    assert started_nodes(events).count("model") == 3


async def test_retry_does_not_repeat_tokens_the_client_already_has() -> None:
    model = node(
        "model",
        "fake_model",
        Retry(max_retries=5, backoff_seconds=0.001),
        response="one two three",
        failure_rate=1.0,
        fail_after_tokens=2,
    )

    events = await collect(compiled(*through_model(model)))

    assert token_text(events) == "one two "
    assert started_nodes(events).count("model") == 1
    assert finished(events).status is RunStatus.FAILED


async def test_failure_after_tokens_overrides_continue_with_null() -> None:
    model = node(
        "model",
        "fake_model",
        ContinueWithNull(),
        response="one two three",
        failure_rate=1.0,
        fail_after_tokens=1,
    )

    events = await collect(compiled(*through_model(model)))

    assert finished(events).status is RunStatus.FAILED


async def test_upstream_model_can_retry_mid_stream_because_nothing_reached_the_client() -> None:
    seed = seed_failing_first_attempts(0.5, failures=1)
    nodes = [
        node("input", "text_input"),
        node(
            "upstream",
            "fake_model",
            Retry(max_retries=1, backoff_seconds=0.001),
            response="one two",
            failure_rate=0.5,
            fail_after_tokens=1,
            seed=seed,
        ),
        node("answer", "fake_model", response="final"),
        node("output", "text_output"),
    ]
    edges = [edge("input", "upstream"), edge("upstream", "answer"), edge("answer", "output")]

    events = await collect(compiled(nodes, edges))

    assert finished(events).status is RunStatus.COMPLETED
    assert started_nodes(events).count("upstream") == 2
    assert token_text(events) == "final"


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, RetryAfter(0.5)), (2, RetryAfter(1.0)), (3, RetryAfter(2.0)), (4, FailTheRun())],
)
def test_backoff_doubles_until_retries_run_out(
    attempt: int, expected: RetryAfter | FailTheRun
) -> None:
    policy = Retry(max_retries=3, backoff_seconds=0.5)

    assert decide_on_failure(policy, attempt, has_sent_tokens=False) == expected


def test_policy_decisions_without_tokens() -> None:
    assert decide_on_failure(FailRun(), 1, has_sent_tokens=False) == FailTheRun()
    assert decide_on_failure(ContinueWithNull(), 1, has_sent_tokens=False) == ContinueNull()

"""Drive a compiled workflow and turn LangGraph's event firehose into `RunEvent`s."""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl
from langgraph.types import Command, StateSnapshot

from runner_spike.compiler import CompiledWorkflow
from runner_spike.events import (
    PROGRESS_EVENT_NAME,
    STEP_EVENT_NAME,
    TOKEN_EVENT_NAME,
    ErrorCode,
    ParkedNode,
    ResumeRun,
    RunContext,
    RunError,
    RunEvent,
    RunFinished,
    RunLimits,
    RunMode,
    RunStarted,
    RunState,
    RunStatus,
    StartRun,
    StepEvent,
    StepStatus,
    TokenEvent,
)
from runner_spike.execution import MaxStepsExceeded, NodeFailed
from runner_spike.nodes import TEXT_PORT

_CUSTOM_EVENTS = (STEP_EVENT_NAME, TOKEN_EVENT_NAME, PROGRESS_EVENT_NAME)
_OPEN_STATUSES = (StepStatus.RUNNING, StepStatus.RETRYING, StepStatus.PARKED)

GraphInput = RunState | Command[Any] | None


class RunStateConflict(Exception):
    """The run is not in a state that allows the requested start, resume or recovery."""


@dataclass
class _RunProgress:
    """What the pump has seen so far, read when the run ends however it ends."""

    open_steps: dict[str, str] = field(default_factory=dict)
    sent_text: list[str] = field(default_factory=list)
    has_cancelled_step: bool = False
    output: Any = None
    parked: tuple[ParkedNode, ...] = ()


def _observe(event: RunEvent, workflow: CompiledWorkflow, progress: _RunProgress) -> None:
    if isinstance(event, TokenEvent):
        progress.sent_text.append(event.text)
    if not isinstance(event, StepEvent):
        return
    if event.status in _OPEN_STATUSES:
        progress.open_steps[event.node_id] = event.node_type
    else:
        progress.open_steps.pop(event.node_id, None)
    if event.status is StepStatus.CANCELLED:
        progress.has_cancelled_step = True
    if event.node_id == workflow.output_node_id and event.status is StepStatus.COMPLETED:
        progress.output = event.output[TEXT_PORT]


def _parked_nodes(snapshot: StateSnapshot) -> tuple[ParkedNode, ...]:
    return tuple(
        ParkedNode(task.name, interrupt.id)
        for task in snapshot.tasks
        for interrupt in task.interrupts
    )


def _load_output(
    snapshot: StateSnapshot, workflow: CompiledWorkflow, progress: _RunProgress
) -> None:
    records = snapshot.values.get("steps", {})
    if workflow.output_node_id in records:
        progress.output = records[workflow.output_node_id]["outputs"].get(TEXT_PORT)


def _load_outcome(
    snapshot: StateSnapshot, workflow: CompiledWorkflow, progress: _RunProgress
) -> None:
    """A checkpointed run can span streams, so its outcome is whatever state says, not this one."""
    records = snapshot.values.get("steps", {})
    progress.parked = _parked_nodes(snapshot)
    progress.has_cancelled_step = any(r["status"] == "cancelled" for r in records.values())
    _load_output(snapshot, workflow, progress)


async def _final_status(
    workflow: CompiledWorkflow,
    config: RunnableConfig,
    control: RunControl,
    progress: _RunProgress,
) -> RunStatus:
    """How a stream that ended without an exception really ended."""
    if workflow.has_checkpointer:
        _load_outcome(await workflow.graph.aget_state(config), workflow, progress)
    if progress.parked:
        if control.drain_requested:
            progress.parked = ()
            return RunStatus.CANCELLED
        return RunStatus.PARKED
    return RunStatus.CANCELLED if progress.has_cancelled_step else RunStatus.COMPLETED


async def _drive(
    workflow: CompiledWorkflow,
    graph_input: GraphInput,
    config: RunnableConfig,
    context: RunContext,
    control: RunControl,
    progress: _RunProgress,
    emit: Callable[[RunEvent], None],
) -> tuple[RunStatus, RunError | None]:
    run_timeout = asyncio.timeout(context.limits.timeout_seconds)
    try:
        async with run_timeout:
            raw_events = workflow.graph.astream_events(
                graph_input, config, version="v2", context=context, control=control
            )
            async for raw in raw_events:
                if raw["event"] == "on_custom_event" and raw["name"] in _CUSTOM_EVENTS:
                    event = cast(RunEvent, raw["data"])
                    _observe(event, workflow, progress)
                    emit(event)
            return await _final_status(workflow, config, control, progress), None
    except TimeoutError as error:
        if not run_timeout.expired():  # a store or client timeout, not the run's limit
            return RunStatus.FAILED, RunError(ErrorCode.INTERNAL_ERROR, repr(error))
        limit = context.limits.timeout_seconds
        return RunStatus.TIMED_OUT, RunError(ErrorCode.TIMEOUT, f"run exceeded {limit}s")
    except GraphDrained:
        return RunStatus.CANCELLED, None
    except NodeFailed as error:
        return RunStatus.FAILED, RunError(ErrorCode.NODE_FAILED, error.message, error.node_id)
    except MaxStepsExceeded as error:
        return RunStatus.FAILED, RunError(ErrorCode.MAX_STEPS_EXCEEDED, str(error), error.node_id)
    except Exception as error:
        # The run boundary: anything unexpected must still end the run visibly.
        message = f"{type(error).__name__}: {error}"
        return RunStatus.FAILED, RunError(ErrorCode.INTERNAL_ERROR, message)


async def _pump(
    workflow: CompiledWorkflow,
    graph_input: GraphInput,
    run_id: str,
    config: RunnableConfig,
    context: RunContext,
    control: RunControl,
    open_steps: dict[str, str],
    emit: Callable[[RunEvent], None],
) -> None:
    progress = _RunProgress(open_steps=open_steps)
    status, error = await _drive(workflow, graph_input, config, context, control, progress, emit)

    # An earlier stream may have finished Output. Not attempted after an internal error, which
    # may mean the store itself is what failed.
    has_readable_state = error is None or error.code is not ErrorCode.INTERNAL_ERROR
    if workflow.has_checkpointer and status is not RunStatus.COMPLETED and has_readable_state:
        _load_output(await workflow.graph.aget_state(config), workflow, progress)

    # LangGraph tears down in-flight nodes without running their step code to the end, so steps
    # are closed here, from what was observed, rather than by the nodes themselves.
    if status is not RunStatus.PARKED:
        for node_id, node_type in progress.open_steps.items():
            emit(StepEvent(node_id, node_type, StepStatus.CANCELLED))

    if status is RunStatus.COMPLETED and progress.sent_text:
        if "".join(progress.sent_text) != progress.output:
            status = RunStatus.FAILED
            message = "the final output differs from the tokens already sent"
            error = RunError(ErrorCode.INTERNAL_ERROR, message, workflow.output_node_id)
    elif status is RunStatus.COMPLETED and isinstance(progress.output, str):
        assert workflow.output_node_id is not None  # progress.output is only set for that node
        emit(TokenEvent(workflow.output_node_id, progress.output))
    emit(RunFinished(run_id, status, progress.output, error, progress.parked))


def _steps_used(snapshot: StateSnapshot) -> int:
    records = snapshot.values.get("steps", {})
    return sum(record["status"] != "skipped" for record in records.values())


@dataclass(frozen=True)
class _RunEntry:
    graph_input: GraphInput
    steps_used: int = 0
    open_steps: Mapping[str, str] = field(default_factory=dict)


def _entry(graph_input: GraphInput, snapshot: StateSnapshot, wf: CompiledWorkflow) -> _RunEntry:
    node_types = {node.id: node.type for node in wf.spec.nodes}
    parked = {node.node_id: node_types[node.node_id] for node in _parked_nodes(snapshot)}
    return _RunEntry(graph_input, _steps_used(snapshot), parked)


async def _prepare(
    workflow: CompiledWorkflow, mode: RunMode, run_id: str, config: RunnableConfig
) -> _RunEntry:
    """Check `mode` against the checkpoint. A refusal raises before the run counts as started."""
    if isinstance(mode, StartRun):
        if workflow.has_checkpointer and (await workflow.graph.aget_state(config)).values:
            raise RunStateConflict(f"run {run_id} has already started")
        return _RunEntry({"run_input": mode.run_input, "steps": {}})

    if not workflow.has_checkpointer:
        raise RunStateConflict(f"run {run_id} cannot continue: the workflow has no checkpointer")
    snapshot = await workflow.graph.aget_state(config)
    if isinstance(mode, ResumeRun):
        parked_ids = {node.interrupt_id for node in _parked_nodes(snapshot)}
        if not mode.values or not set(mode.values) <= parked_ids:
            raise RunStateConflict(f"run {run_id} is not parked on {sorted(mode.values)}")
        return _entry(Command(resume=dict(mode.values)), snapshot, workflow)
    if not snapshot.next:
        raise RunStateConflict(f"run {run_id} has nothing to recover")
    return _entry(None, snapshot, workflow)


async def stream_run(
    workflow: CompiledWorkflow,
    mode: RunMode,
    limits: RunLimits,
    *,
    control: RunControl | None = None,
    secrets: Mapping[str, str] | None = None,
    run_id: str | None = None,
) -> AsyncGenerator[RunEvent]:
    """Run to a terminal `RunFinished`. Closing this generator aborts the run.

    The graph runs in its own task so a timeout never spans a `yield` to the consumer, and a
    slow consumer cannot stall the limit.
    """
    resolved_run_id = run_id or str(uuid.uuid4())
    config: RunnableConfig = {
        # Each superstep advances at least one level of a DAG, so this can only trip on a bug.
        "recursion_limit": len(workflow.spec.nodes) + 1,
        "configurable": {"thread_id": resolved_run_id},
    }
    entry = await _prepare(workflow, mode, resolved_run_id, config)
    context = RunContext(limits=limits, secrets=dict(secrets or {}), steps_used=entry.steps_used)
    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    queue.put_nowait(RunStarted(resolved_run_id))

    async def pump_then_close() -> None:
        try:
            await _pump(
                workflow,
                entry.graph_input,
                resolved_run_id,
                config,
                context,
                control or RunControl(),
                dict(entry.open_steps),
                queue.put_nowait,
            )
        finally:
            queue.put_nowait(None)

    pump = asyncio.create_task(pump_then_close())
    try:
        while (event := await queue.get()) is not None:
            yield event
        await pump  # surfaces anything the pump did not turn into a RunFinished
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump

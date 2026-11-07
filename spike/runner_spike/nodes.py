"""The node types the spike needs: just enough to exercise the runner semantics."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from runner_spike.fake_model import FakeStreamingChatModel

TEXT_PORT = "text"
CHUNKS_PORT = "chunks"
MERGE_INPUT_PORT = "inputs"
MERGE_OUTPUT_PORT = "merged"
BRANCH_TRUE_PORT = "true"
BRANCH_FALSE_PORT = "false"
FIRST_NON_NULL = "first_non_null"


class NodeKind(Enum):
    PLAIN = "plain"
    BRANCH = "branch"
    MERGE = "merge"
    OUTPUT = "output"


class NodeCancelled(Exception):
    """Raised by a long-running handler that saw `is_cancel_requested()`."""


@dataclass(frozen=True)
class NodeCall:
    inputs: Mapping[str, Any]
    settings: Mapping[str, Any]
    run_input: Any
    attempt: int
    secrets: Mapping[str, str]
    is_cancel_requested: Callable[[], bool]
    report_progress: Callable[[Mapping[str, Any]], Awaitable[None]]


RunHandler = Callable[[NodeCall], Awaitable[dict[str, Any]]]
StreamHandler = Callable[[NodeCall], AsyncIterator[str]]


def _never(_settings: Mapping[str, Any]) -> bool:
    return False


@dataclass(frozen=True)
class NodeType:
    """Exactly one of `run` or `stream`; a `stream` node's tokens join into its `text` port."""

    input_ports: tuple[str, ...]
    output_ports: tuple[str, ...]
    run: RunHandler | None = None
    stream: StreamHandler | None = None
    kind: NodeKind = NodeKind.PLAIN
    is_stream_transparent: Callable[[Mapping[str, Any]], bool] = _never
    optional_input_ports: tuple[str, ...] = ()
    can_park: Callable[[Mapping[str, Any]], bool] = _never

    def __post_init__(self) -> None:
        if (self.run is None) == (self.stream is None):
            raise ValueError("a node type needs exactly one of run or stream")
        if self.stream is not None and len(self.output_ports) != 1:
            raise ValueError("a stream node has exactly one output port")


async def _text_input(call: NodeCall) -> dict[str, Any]:
    return {TEXT_PORT: call.run_input}


async def _text_output(call: NodeCall) -> dict[str, Any]:
    return {TEXT_PORT: call.inputs[TEXT_PORT]}


async def _if_else(call: NodeCall) -> dict[str, Any]:
    """Writes only the taken port: an unwritten port is what makes its edges dead."""
    text = call.inputs[TEXT_PORT]
    is_match = text is not None and call.settings["contains"] in text
    return {BRANCH_TRUE_PORT if is_match else BRANCH_FALSE_PORT: text}


async def _merge(call: NodeCall) -> dict[str, Any]:
    live_values = call.inputs[MERGE_INPUT_PORT]
    if call.settings.get("combine") == FIRST_NON_NULL:
        first = next((value for value in live_values if value is not None), None)
        return {MERGE_OUTPUT_PORT: first}
    return {MERGE_OUTPUT_PORT: live_values}


def _merge_is_stream_transparent(settings: Mapping[str, Any]) -> bool:
    return settings.get("combine") == FIRST_NON_NULL


async def _fake_model(call: NodeCall) -> AsyncIterator[str]:
    settings = call.settings
    model = FakeStreamingChatModel(
        response=settings["response"],
        token_delay_seconds=settings.get("token_delay_seconds", 0.0),
        failure_rate=settings.get("failure_rate", 0.0),
        fail_after_tokens=settings.get("fail_after_tokens", 0),
        seed=settings.get("seed", 0) + call.attempt,
    )
    async for chunk in model.astream(str(call.inputs.get(TEXT_PORT))):
        yield chunk.text


def default_registry() -> dict[str, NodeType]:
    return {
        "text_input": NodeType((), (TEXT_PORT,), run=_text_input),
        "text_output": NodeType(
            (TEXT_PORT, CHUNKS_PORT),
            (TEXT_PORT,),
            run=_text_output,
            kind=NodeKind.OUTPUT,
            optional_input_ports=(CHUNKS_PORT,),
        ),
        "if_else": NodeType(
            (TEXT_PORT,),
            (BRANCH_TRUE_PORT, BRANCH_FALSE_PORT),
            run=_if_else,
            kind=NodeKind.BRANCH,
        ),
        "merge": NodeType(
            (MERGE_INPUT_PORT,),
            (MERGE_OUTPUT_PORT,),
            run=_merge,
            kind=NodeKind.MERGE,
            is_stream_transparent=_merge_is_stream_transparent,
        ),
        "fake_model": NodeType((TEXT_PORT,), (TEXT_PORT,), stream=_fake_model),
    }

"""What a run tells the outside world, and the state and context it carries."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

STEP_EVENT_NAME = "flowbuilder.step"
TOKEN_EVENT_NAME = "flowbuilder.token"
PROGRESS_EVENT_NAME = "flowbuilder.progress"


class StepStatus(Enum):
    RUNNING = "running"
    RETRYING = "retrying"
    PARKED = "parked"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RunStatus(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    PARKED = "parked"


class ErrorCode(Enum):
    NODE_FAILED = "node_failed"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class RunError:
    code: ErrorCode
    message: str
    node_id: str | None = None


@dataclass(frozen=True)
class RunStarted:
    run_id: str


@dataclass(frozen=True)
class StepEvent:
    node_id: str
    node_type: str
    status: StepStatus
    attempt: int = 0
    latency_ms: int | None = None
    input: Any = None
    output: Any = None
    error: str | None = None


@dataclass(frozen=True)
class TokenEvent:
    node_id: str
    text: str


@dataclass(frozen=True)
class ProgressEvent:
    node_id: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class ParkedNode:
    node_id: str
    interrupt_id: str


@dataclass(frozen=True)
class RunFinished:
    run_id: str
    status: RunStatus
    output: Any = None
    error: RunError | None = None
    parked: tuple[ParkedNode, ...] = ()


RunEvent = RunStarted | StepEvent | TokenEvent | ProgressEvent | RunFinished


@dataclass(frozen=True)
class StartRun:
    """A new run. The run ID must not have been used."""

    run_input: Any


@dataclass(frozen=True)
class ResumeRun:
    """Deliver values to parked nodes, by interrupt ID. Other parked nodes stay parked."""

    values: Mapping[str, Any]


@dataclass(frozen=True)
class RecoverRun:
    """Continue from the last checkpoint after a crash. Finished nodes are not repeated."""


RunMode = StartRun | ResumeRun | RecoverRun


class StepRecord(TypedDict):
    status: Literal["completed", "failed", "skipped", "cancelled"]
    outputs: dict[str, Any]


def merge_step_records(
    current: dict[str, StepRecord], update: dict[str, StepRecord]
) -> dict[str, StepRecord]:
    """Parallel nodes write in the same superstep, so the channel needs a reducer."""
    return {**current, **update}


class RunState(TypedDict):
    """Everything checkpointed. Must stay JSON-serialisable and must never hold a secret."""

    run_input: Any
    steps: Annotated[dict[str, StepRecord], merge_step_records]


class RunLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0)
    max_steps: int = Field(gt=0)


@dataclass
class RunContext:
    """Per-run values that are deliberately kept out of checkpointed state."""

    limits: RunLimits
    secrets: dict[str, str] = field(default_factory=dict)
    steps_used: int = 0

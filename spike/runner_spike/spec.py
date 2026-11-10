"""Graph JSON as saved by the canvas: the untrusted input to the compiler."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Starting with an alphanumeric rules out LangGraph's reserved `__start__`/`__end__`, and the
# character set rules out its reserved `:` and `|`.
NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
MAX_RETRIES = 10


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FailRun(_Frozen):
    policy: Literal["fail_run"] = "fail_run"


class ContinueWithNull(_Frozen):
    policy: Literal["continue_with_null"] = "continue_with_null"


class Retry(_Frozen):
    policy: Literal["retry"] = "retry"
    max_retries: int = Field(ge=1, le=MAX_RETRIES)
    backoff_seconds: float = Field(ge=0)


FailurePolicy = Annotated[FailRun | ContinueWithNull | Retry, Field(discriminator="policy")]


class NodeSpec(_Frozen):
    id: str = Field(pattern=NODE_ID_PATTERN)
    type: str
    version: int = Field(default=1, ge=1)
    settings: dict[str, Any] = Field(default_factory=dict)
    on_failure: FailurePolicy = FailRun()


class EdgeSpec(_Frozen):
    source: str
    source_port: str
    target: str
    target_port: str


class GraphSpec(_Frozen):
    schema_version: Literal[1] = 1
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...] = ()

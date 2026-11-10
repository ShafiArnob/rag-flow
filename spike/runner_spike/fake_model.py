"""A chat model with scripted tokens, delay and failures.

It subclasses `BaseChatModel` so tokens travel the same callback path into `astream_events` that
real provider models use. A hand-rolled generator would prove nothing about streaming.
"""

import asyncio
import random
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import Field


class FakeModelError(Exception):
    """The scripted failure, standing in for a provider 5xx or a dropped connection."""


class FakeStreamingChatModel(BaseChatModel):
    response: str
    token_delay_seconds: float = Field(default=0.0, ge=0)
    failure_rate: float = Field(default=0.0, ge=0, le=1)
    fail_after_tokens: int = Field(default=0, ge=0)
    seed: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-streaming"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("FakeStreamingChatModel only streams; use astream()")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        will_fail = random.Random(self.seed).random() < self.failure_rate
        tokens = re.findall(r"\S+\s*", self.response)
        for index, token in enumerate(tokens):
            if will_fail and index >= self.fail_after_tokens:
                raise FakeModelError(f"scripted failure after {index} tokens")
            await asyncio.sleep(self.token_delay_seconds)
            yield ChatGenerationChunk(message=AIMessageChunk(content=token))
        if will_fail:
            raise FakeModelError(f"scripted failure after {len(tokens)} tokens")

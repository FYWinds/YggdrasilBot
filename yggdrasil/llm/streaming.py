"""Streamed model requests: per-request TTFT and generation speed, and the shared event handler.

Every ``agent.run`` passes ``event_stream_handler=drain_events`` so pydantic-ai streams each model
request; the time between chunks is what tells first-token latency apart from generation speed.
"""

import time
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import AgentStreamEvent, ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from yggdrasil.core.metrics import LLM_OUTPUT_SPEED, LLM_TTFT

# Short outputs (a tool call, a one-word reply) end before the speed means anything, and a response a
# proxy delivers in one burst after the first chunk would read as thousands of tokens per second.
_MIN_SPEED_TOKENS = 16
_MIN_SPEED_SECONDS = 0.25


async def drain_events(_ctx: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
    async for _ in events:
        pass


class MeteredModel(WrapperModel):
    """Records ``ygg.llm.ttft`` and ``ygg.llm.output_speed`` for each streamed request."""

    def __init__(self, wrapped: Model, *, task: str, label: str) -> None:
        super().__init__(wrapped)
        self._attrs = {"task": task, "model": label}

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        started = time.perf_counter()
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            yield response
        # The agent exits this context only after it has consumed the whole stream.
        ttft = response.time_to_first_chunk(started)
        if ttft is None:
            return
        LLM_TTFT.record(ttft, self._attrs)
        generating = time.perf_counter() - started - ttft
        tokens = response.usage.output_tokens or 0
        if tokens >= _MIN_SPEED_TOKENS and generating >= _MIN_SPEED_SECONDS:
            LLM_OUTPUT_SPEED.record(tokens / generating, self._attrs)

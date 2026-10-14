"""Streamed model requests: per-request TTFT and generation speed, and the shared event handler.

Every ``agent.run`` passes ``event_stream_handler=drain_events`` so pydantic-ai streams each model
request; the time between chunks is what tells first-token latency apart from generation speed.
A caller that wants the numbers for its own run (the reply metadata) sets ``RUN_STATS`` first.
"""

import time
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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


@dataclass(slots=True)
class RequestStat:
    model: str  # display name, no provider
    ttft: float
    generating: float
    tokens: int

    @property
    def measurable(self) -> bool:
        return self.tokens >= _MIN_SPEED_TOKENS and self.generating >= _MIN_SPEED_SECONDS


@dataclass
class RunStats:
    """Streamed requests of one run, sub-agents (vision) included."""

    requests: list[RequestStat] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys(r.model for r in self.requests))

    @property
    def ttft(self) -> float | None:
        return self.requests[0].ttft if self.requests else None

    @property
    def tokens_per_s(self) -> float | None:
        timed = [r for r in self.requests if r.measurable]
        seconds = sum(r.generating for r in timed)
        return sum(r.tokens for r in timed) / seconds if seconds else None


RUN_STATS: ContextVar[RunStats | None] = ContextVar("ygg_run_stats", default=None)


async def drain_events(_ctx: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
    async for _ in events:
        pass


class MeteredModel(WrapperModel):
    """Records ``ygg.llm.ttft`` and ``ygg.llm.output_speed`` for each streamed request."""

    def __init__(self, wrapped: Model, *, task: str, label: str, display: str) -> None:
        super().__init__(wrapped)
        self._attrs = {"task": task, "model": label}
        self._display = display

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
        stat = RequestStat(self._display, ttft, time.perf_counter() - started - ttft, response.usage.output_tokens or 0)
        LLM_TTFT.record(ttft, self._attrs)
        if stat.measurable:
            LLM_OUTPUT_SPEED.record(stat.tokens / stat.generating, self._attrs)
        if (stats := RUN_STATS.get()) is not None:
            stats.requests.append(stat)

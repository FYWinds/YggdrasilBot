"""Spans and metrics for every OneBot action, via NoneBot's call_api hooks."""

import time
from typing import Any

from nonebot.adapters import Bot
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from yggdrasil.core.metrics import ONEBOT_API, ONEBOT_API_DURATION
from yggdrasil.core.telemetry import TRACER

# NoneBot passes the same ``data`` dict to the calling and called hooks; key on its
# identity so concurrent calls from sibling tasks don't mix up their spans.
_inflight: dict[int, tuple[Span, float]] = {}

# Polled or trivially cheap actions that would only add noise to traces.
_NO_SPAN = {"get_status", "get_version_info"}


async def on_calling_api(bot: Bot, api: str, data: dict[str, Any]) -> None:
    span = (
        TRACER.start_span(f"onebot {api}", kind=trace.SpanKind.CLIENT, attributes={"onebot.action": api})
        if api not in _NO_SPAN
        else trace.INVALID_SPAN
    )
    _inflight[id(data)] = (span, time.perf_counter())


async def on_called_api(bot: Bot, exception: Exception | None, api: str, data: dict[str, Any], result: Any) -> None:
    entry = _inflight.pop(id(data), None)
    if entry is None:
        return
    span, started = entry
    elapsed = time.perf_counter() - started
    retcode = getattr(exception, "retcode", None)
    outcome = "ok" if exception is None else ("failed" if retcode is not None else "error")
    attrs = {"action": api, "outcome": outcome}
    ONEBOT_API.add(1, attrs)
    ONEBOT_API_DURATION.record(elapsed, attrs)
    if span.is_recording():
        if exception is not None:
            span.record_exception(exception)
            span.set_status(Status(StatusCode.ERROR, str(exception)[:200]))
            if retcode is not None:
                span.set_attribute("onebot.retcode", retcode)
        span.end()


def install() -> None:
    from nonebot.adapters.onebot.v11 import Bot as V11Bot

    V11Bot.on_calling_api(on_calling_api)
    V11Bot.on_called_api(on_called_api)

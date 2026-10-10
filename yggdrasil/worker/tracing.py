"""Taskiq middleware: W3C trace context through task labels, one span and run metrics per job."""

import time
from contextvars import Token
from dataclasses import dataclass
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from yggdrasil.core.metrics import JOB_DURATION, JOB_RUNS
from yggdrasil.core.telemetry import TRACER

_CARRIER_KEYS = ("traceparent", "tracestate")


@dataclass(slots=True)
class _Run:
    span: Span
    token: Token[otel_context.Context]
    started: float


class TracingMiddleware(TaskiqMiddleware):
    def __init__(self) -> None:
        super().__init__()
        self._runs: dict[str, _Run] = {}

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        for key in _CARRIER_KEYS:
            if key in carrier:
                message.labels[key] = carrier[key]
        return message

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        carrier = {k: str(message.labels[k]) for k in _CARRIER_KEYS if message.labels.get(k)}
        # An empty context makes the job its own root span when nothing was propagated.
        parent = propagate.extract(carrier, context=otel_context.Context()) if carrier else otel_context.Context()
        span = TRACER.start_span(
            f"job {message.task_name}",
            context=parent,
            kind=SpanKind.CONSUMER,
            attributes={"messaging.system": "taskiq", "messaging.message.id": message.task_id},
        )
        token = otel_context.attach(trace.set_span_in_context(span, parent))
        self._runs[message.task_id] = _Run(span, token, time.perf_counter())
        return message

    def on_error(self, message: TaskiqMessage, result: TaskiqResult[Any], exception: BaseException) -> None:
        run = self._runs.get(message.task_id)
        if run is not None:
            run.span.record_exception(exception)

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        run = self._runs.pop(message.task_id, None)
        outcome = "error" if result.is_err else "ok"
        # Not "job": Prometheus overwrites it with the scrape job, which merged every task into one series.
        attrs = {"ygg.job": message.task_name}
        JOB_RUNS.add(1, {**attrs, "outcome": outcome})
        if run is None:
            return
        JOB_DURATION.record(time.perf_counter() - run.started, attrs)
        if result.is_err:
            run.span.set_status(Status(StatusCode.ERROR, type(result.error).__name__ if result.error else None))
        run.span.end()
        otel_context.detach(run.token)

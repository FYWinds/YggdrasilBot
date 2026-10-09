"""OpenTelemetry setup shared by the bot and the worker.

Traces and metrics go over OTLP/HTTP to ``OTEL_EXPORTER_OTLP_ENDPOINT``. The
cluster collector's remote sampler returns probability 0 for root spans, so we
use the SDK sampler (``OTEL_TRACES_SAMPLER``, default ``parentbased_always_on``)
and create our own root spans per invocation / job.
"""

import os
import uuid
from collections.abc import Sequence

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, View
from opentelemetry.sdk.resources import SERVICE_INSTANCE_ID, SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.util.types import Attributes
from sqlalchemy.ext.asyncio import AsyncEngine

from yggdrasil import __version__
from yggdrasil.config import TELEMETRY_CONFIG

TRACER = trace.get_tracer("yggdrasil")

_SYSTEM_METRICS: dict[str, list[str] | None] = {
    "process.cpu.time": ["user", "system"],
    "process.memory.usage": None,
    "process.thread.count": None,
    "cpython.gc.collections": None,
}

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


class _DropOrphanClients(Sampler):
    """Root sampler: drop CLIENT spans that start a trace.

    Background loops (gauge refresh, message writer, Redis listeners, the taskiq BRPOP poll)
    run DB/Redis/OneBot calls outside any request; each would otherwise become its own
    one-span trace ("SELECT", "connect", "BRPOP") and bury the real invocation and job traces.
    """

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: trace.SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[trace.Link] | None = None,
        trace_state: trace.TraceState | None = None,
    ) -> SamplingResult:
        if kind == trace.SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return "DropOrphanClients"


def _resource(service_name: str) -> Resource:
    resource = Resource.create({SERVICE_NAME: service_name, SERVICE_VERSION: __version__})
    if SERVICE_INSTANCE_ID not in resource.attributes:
        resource = resource.merge(Resource({SERVICE_INSTANCE_ID: str(uuid.uuid4())}))
    return resource


def setup_telemetry(service_name: str, app: FastAPI | None = None) -> bool:
    """Configure providers and instrumentors. No-op (returns False) without an OTLP endpoint."""
    global _tracer_provider, _meter_provider
    if not TELEMETRY_CONFIG.enabled or _tracer_provider is not None:
        return _tracer_provider is not None

    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    resource = _resource(service_name)

    # Children follow their parent; only roots go through _DropOrphanClients.
    _tracer_provider = TracerProvider(resource=resource, sampler=ParentBased(root=_DropOrphanClients()))
    _tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        # HTTP RED metrics come from Tempo span metrics and Traefik.
        views=[View(instrument_name="http.server.*", aggregation=DropAggregation())],
    )
    metrics.set_meter_provider(_meter_provider)

    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="/healthz,/readyz,/onebot/v11/ws",
            exclude_spans=["receive", "send"],
        )
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    SystemMetricsInstrumentor(config=_SYSTEM_METRICS).instrument()
    return True


def instrument_engine(engine: AsyncEngine) -> None:
    if _tracer_provider is None:
        return
    # The instrumentor's dependency range stops at sqlalchemy < 2.1; the engine events it uses still work.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, enable_commenter=False, skip_dep_check=True)


def shutdown_telemetry() -> None:
    """Flush pending spans and metrics so a rollout doesn't lose the last batch."""
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    if _meter_provider is not None:
        _meter_provider.shutdown()


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return trace.format_trace_id(ctx.trace_id) if ctx.is_valid else None


__all__ = ["TRACER", "current_trace_id", "instrument_engine", "setup_telemetry", "shutdown_telemetry"]

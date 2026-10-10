"""Application metrics (prefix ``ygg.``).

Labels stay low-cardinality: group ids are whitelisted (a handful), never user ids.
In Prometheus they appear as e.g. ``ygg_invocations_total``.
"""

from collections.abc import Callable, Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

METER = metrics.get_meter("yggdrasil")

_LATENCY_BUCKETS = [0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60, 120]

MESSAGES_INGESTED = METER.create_counter("ygg.messages.ingested", unit="{message}")
INVOCATIONS = METER.create_counter("ygg.invocations", unit="{invocation}")
INVOCATION_ACK_LATENCY = METER.create_histogram(
    "ygg.invocation.ack_latency", unit="s", explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS
)
INVOCATION_DURATION = METER.create_histogram(
    "ygg.invocation.duration", unit="s", explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS
)
LLM_TOKENS = METER.create_counter("ygg.llm.tokens", unit="{token}")
LLM_REQUESTS = METER.create_counter("ygg.llm.requests", unit="{request}")
TOOL_CALLS = METER.create_counter("ygg.tool.calls", unit="{call}")
TOOL_DURATION = METER.create_histogram(
    "ygg.tool.duration", unit="s", explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS
)
SEARCH_REQUESTS = METER.create_counter("ygg.search.requests", unit="{request}")
MODERATION_VERDICTS = METER.create_counter("ygg.moderation.verdicts", unit="{verdict}")
MODERATION_DURATION = METER.create_histogram(
    "ygg.moderation.duration", unit="s", explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS
)
ONEBOT_API = METER.create_counter("ygg.onebot.api", unit="{call}")
ONEBOT_API_DURATION = METER.create_histogram(
    "ygg.onebot.api.duration", unit="s", explicit_bucket_boundaries_advisory=_LATENCY_BUCKETS
)
RATELIMIT_REJECTED = METER.create_counter("ygg.ratelimit.rejected", unit="{request}")
JOB_RUNS = METER.create_counter("ygg.job.runs", unit="{run}")
JOB_DURATION = METER.create_histogram(
    "ygg.job.duration", unit="s", explicit_bucket_boundaries_advisory=[0.5, 1, 5, 15, 30, 60, 120, 300, 600]
)
TRIGGERS_MATCHED = METER.create_counter("ygg.triggers.matched", unit="{match}")
BOT_EVENTS = METER.create_counter("ygg.bot.events", unit="{event}")


def observe_gauge(name: str, unit: str, fn: Callable[[], Iterable[tuple[float, dict[str, str]]]]) -> None:
    """Register an observable gauge backed by ``fn`` returning ``(value, attributes)`` pairs."""

    def _callback(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(value, attrs) for value, attrs in fn()]

    METER.create_observable_gauge(name, callbacks=[_callback], unit=unit)

"""Taskiq broker and scheduler for the memory worker.

Run with::

    taskiq worker yggdrasil.worker.broker:broker yggdrasil.worker.tasks
    taskiq scheduler yggdrasil.worker.broker:scheduler yggdrasil.worker.tasks
"""

import asyncio
import contextlib

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from yggdrasil import runtime
from yggdrasil.config import REDIS_CONFIG
from yggdrasil.core.log import LOGGER, setup_logging
from yggdrasil.core.telemetry import setup_telemetry, shutdown_telemetry
from yggdrasil.db.session import dispose_engine
from yggdrasil.worker.tracing import TracingMiddleware

TIMEZONE = "Asia/Shanghai"

# BRPOP blocks indefinitely; redis-py 8 defaults to a 5 s socket timeout that would kill the listener.
broker = ListQueueBroker(str(REDIS_CONFIG.url), queue_name="ygg:tasks", socket_timeout=None).with_middlewares(
    TracingMiddleware()
)
scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker)])


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(state: TaskiqState) -> None:
    setup_logging()
    setup_telemetry("yggdrasil-worker")
    await runtime.load()
    state.config_listener = asyncio.create_task(runtime.listen_forever(), name="config-listener")
    LOGGER.info("worker started")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(state: TaskiqState) -> None:
    listener: asyncio.Task[None] | None = getattr(state, "config_listener", None)
    if listener is not None:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
    shutdown_telemetry()
    await dispose_engine()

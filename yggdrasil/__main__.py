"""Bot process: ``python -m yggdrasil``."""

import asyncio
import contextlib
import logging
import time

import nonebot
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from sqlalchemy import text

from yggdrasil.config import APP_CONFIG, LOG_CONFIG
from yggdrasil.core.log import LOGGER, setup_logging

_tasks: list[asyncio.Task] = []
_gauges: dict[str, list[tuple[float, dict[str, str]]]] = {}


async def _gauge_loop() -> None:
    """Refresh DB-derived gauges (memory lag, partitions runway, pending reviews) once a minute."""
    from yggdrasil.db.partitions import months_of_runway
    from yggdrasil.db.session import engine
    from yggdrasil.moderation.review import refresh_pending
    from yggdrasil.runtime import snapshot

    while True:
        try:
            pending = await refresh_pending()
            _gauges["review"] = [(pending, {})]
            async with engine().connect() as conn:
                _gauges["runway"] = [(await months_of_runway(conn), {})]
                lag = []
                for gid, group in snapshot().groups.items():
                    if not group.enabled:
                        continue
                    for pipeline in ("chunk", "topic"):
                        row = await conn.execute(
                            text(
                                "SELECT count(*) FROM messages WHERE group_id = :g AND NOT recalled AND id > "
                                "COALESCE((SELECT last_msg_id FROM pipeline_state WHERE pipeline = :p "
                                "AND group_id = :g), 0)"
                            ),
                            {"g": gid, "p": pipeline},
                        )
                        lag.append((float(row.scalar() or 0), {"group": str(gid), "pipeline": pipeline}))
                _gauges["lag"] = lag
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("gauge refresh failed")
        await asyncio.sleep(60)


def _register_gauges() -> None:
    from yggdrasil.bot import state
    from yggdrasil.core.metrics import observe_gauge

    observe_gauge("ygg.onebot.connected", "{bot}", lambda: [(float(len(state.connected)), {})])
    # Connected and not reported logged out: what actually decides whether QQ messages flow.
    observe_gauge("ygg.onebot.logged_in", "{bot}", lambda: [(float(len(set(state.connected) - state.offline)), {})])
    observe_gauge(
        "ygg.onebot.heartbeat_age",
        "s",
        lambda: [
            (time.time() - ts, {"self_id": sid}) for sid, ts in state.last_heartbeat.items() if sid in state.connected
        ],
    )
    observe_gauge("ygg.review.pending", "{review}", lambda: _gauges.get("review", []))
    observe_gauge("ygg.partitions.runway", "mo", lambda: _gauges.get("runway", []))
    observe_gauge("ygg.memory.lag", "{message}", lambda: _gauges.get("lag", []))


def _health_routes(app: FastAPI) -> None:
    from yggdrasil.db.session import engine

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            async with engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            return JSONResponse({"status": "db unavailable", "error": str(e)[:200]}, status_code=503)
        return JSONResponse({"status": "ok", "bots": list(nonebot.get_bots())})

    @app.get("/drainz", include_in_schema=False)
    async def drainz(max_wait: float = 170) -> dict[str, int]:
        """preStop hook: hold the pod (and its OneBot connection) until in-flight replies are sent.

        The terminating pod is already out of the Service, so the dashboard is served by the new pod;
        the connection moves when SIGTERM closes it and NapCat reconnects.
        """
        from yggdrasil.bot.inflight import drain

        left = await drain(min(max_wait, 600))
        LOGGER.info("drained for shutdown; {} tasks still running", left)
        return {"left": left}


def main() -> None:
    nonebot.init(
        driver="~fastapi",
        host=APP_CONFIG.host,
        port=APP_CONFIG.port,
        onebot_access_token=APP_CONFIG.onebot_access_token or None,
        command_start={""},
        command_sep={" "},
        nickname=set(),
        log_level=LOG_CONFIG.level,
        superusers={str(u) for u in APP_CONFIG.bootstrap_superusers},
        alconna_use_command_start=False,
        alconna_auto_send_output=False,
        alconna_response_self=False,
        fastapi_reload=False,
        # FastAPI >= 0.142 adds its own OTLP exporters from OTEL_EXPORTER_OTLP_ENDPOINT at lifespan
        # startup, on top of setup_telemetry's: every span and metric was exported twice.
        fastapi_extra={"telemetry": {"auto_configure": False}},
        # Don't let NoneBot read .env (it logs every key, incl. secrets, at DEBUG). A falsy value
        # makes it fall back to (".env", ".env.<env>"), so point it at a file that never exists.
        _env_file=("__nonebot_env_disabled__",),
    )
    setup_logging()
    # NoneBot's uvicorn log config already routes uvicorn.* to loguru; stop a second copy via root.
    logging.getLogger("uvicorn").propagate = False
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    app: FastAPI = nonebot.get_app()

    from yggdrasil.core.telemetry import setup_telemetry, shutdown_telemetry
    from yggdrasil.napcat import hooks

    setup_telemetry("yggdrasil-bot", app)
    hooks.install()
    _register_gauges()
    _health_routes(app)
    try:
        from yggdrasil.admin import mount
    except ImportError:
        LOGGER.warning("admin dashboard module not available")
    else:
        mount(app)

    nonebot.load_plugin("nonebot_plugin_alconna")
    nonebot.load_plugin("yggdrasil.bot.plugin")

    @driver.on_startup
    async def _startup() -> None:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        from yggdrasil import runtime
        from yggdrasil.db.session import init_engine
        from yggdrasil.ingest.writer import WRITER
        from yggdrasil.moderation.review import expire_loop
        from yggdrasil.seed import seed_defaults

        init_engine()
        await seed_defaults()
        snap = await runtime.load()
        Agent.instrument_all(
            InstrumentationSettings(
                include_content=snap.settings().privacy.capture_llm_content, include_binary_content=False
            )
        )
        _tasks.extend(
            asyncio.create_task(coro, name=name)
            for name, coro in (
                ("config-listener", runtime.listen_forever()),
                ("ingest-writer", WRITER.run()),
                ("review-expiry", expire_loop()),
                ("gauges", _gauge_loop()),
            )
        )
        LOGGER.info("yggdrasil bot started on {}:{}", APP_CONFIG.host, APP_CONFIG.port)

    @driver.on_shutdown
    async def _shutdown() -> None:
        from yggdrasil.db.session import dispose_engine
        from yggdrasil.ingest.writer import WRITER

        for task in _tasks:
            task.cancel()
        for task in _tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await WRITER.flush()
        await dispose_engine()
        shutdown_telemetry()

    nonebot.run()


if __name__ == "__main__":
    main()

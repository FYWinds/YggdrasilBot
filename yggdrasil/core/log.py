"""Loguru setup: JSON lines on stdout with OTel trace correlation.

NoneBot installs its own loguru sink on import (``nonebot.log.logger_id``); we
replace it so every log line, including NoneBot's and stdlib loggers', goes
through one sink.
"""

import inspect
import logging
import sys
import traceback

import loguru
import orjson
from opentelemetry import trace

from yggdrasil.config import LOG_CONFIG

LOGGER = loguru.logger
_configured = False


def _add_trace_context(record: "loguru.Record") -> None:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        trace_id, span_id = trace.format_trace_id(ctx.trace_id), trace.format_span_id(ctx.span_id)
        record["extra"].update(trace_id=trace_id, span_id=span_id, trace=f" trace_id={trace_id}")
    else:
        record["extra"]["trace"] = ""


def _json_sink(message: "loguru.Message") -> None:
    record = message.record
    entry: dict[str, object] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    entry.update((k, v) for k, v in record["extra"].items() if k != "trace")
    if exc := record["exception"]:
        entry["exception"] = "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
    sys.stdout.write(orjson.dumps(entry, default=str).decode() + "\n")
    sys.stdout.flush()


class LoguruHandler(logging.Handler):
    """Redirect stdlib logging (uvicorn, sqlalchemy, httpx, taskiq) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = LOGGER.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        LOGGER.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """Idempotent; call once per process before anything logs."""
    global _configured
    if _configured:
        return
    _configured = True

    LOGGER.remove()
    LOGGER.configure(patcher=_add_trace_context, extra={"trace": ""})
    if LOG_CONFIG.json_output:
        LOGGER.add(_json_sink, level=LOG_CONFIG.level, diagnose=False)
    else:
        LOGGER.add(sys.stdout, level=LOG_CONFIG.level, diagnose=False, format=LOG_CONFIG.format)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [LoguruHandler()]
    for name in list(logging.root.manager.loggerDict):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Chatty libraries.
    for name in ("httpx", "httpcore", "openai._base_client", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)


__all__ = ["LOGGER", "LoguruHandler", "setup_logging"]

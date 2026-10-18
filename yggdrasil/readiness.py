"""Startup gate: wait for Postgres and Redis instead of crash-looping while they come up.

After a node reboot every pod starts at once; the database answers last.
"""

import asyncio
import time

from sqlalchemy import text

from yggdrasil.core.log import LOGGER
from yggdrasil.db.session import engine
from yggdrasil.kv import redis_client

_TIMEOUT_S = 300.0
_MAX_DELAY_S = 10.0


async def _check() -> None:
    async with engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    await redis_client().ping()


async def wait_for_dependencies(timeout_s: float = _TIMEOUT_S) -> None:
    """Return once Postgres and Redis answer; re-raise the last error after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    delay = 1.0
    while True:
        try:
            await _check()
            return
        except Exception as e:
            if time.monotonic() + delay > deadline:
                raise
            LOGGER.warning("waiting for Postgres/Redis: {}: {}", type(e).__name__, str(e)[:200])
        await asyncio.sleep(delay)
        delay = min(delay * 2, _MAX_DELAY_S)

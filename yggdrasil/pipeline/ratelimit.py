"""Fixed-window rate limits, daily token budgets (Redis) and per-process concurrency slots."""

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from yggdrasil.core.metrics import RATELIMIT_REJECTED
from yggdrasil.kv import redis_client
from yggdrasil.settings import RateLimitSettings

_TZ = ZoneInfo("Asia/Shanghai")
_semaphores: dict[tuple[str, int | None, int], asyncio.Semaphore] = {}
_waiting: Counter[tuple[int | None, int]] = Counter()


async def _hit(key: str, window_s: int, limit: int) -> bool:
    """Count one hit; True if still within ``limit`` for the window."""
    r = redis_client()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_s, nx=True)
    count, _ = await pipe.execute()
    return int(count) <= limit


def _day() -> str:
    return datetime.now(_TZ).strftime("%Y%m%d")


async def check(group_id: int | None, user_id: int, cfg: RateLimitSettings, *, superuser: bool) -> str | None:
    """Return a rejection reason, or None if allowed. Superusers bypass limits."""
    if superuser:
        return None
    g = group_id or 0
    checks = [
        (f"ygg:rl:u:{user_id}:m", 60, cfg.user_per_minute, "user_minute"),
        (f"ygg:rl:u:{user_id}:d:{_day()}", 86400, cfg.user_per_day, "user_day"),
        (f"ygg:rl:g:{g}:m", 60, cfg.group_per_minute, "group_minute"),
    ]
    for key, window, limit, scope in checks:
        if limit > 0 and not await _hit(key, window, limit):
            RATELIMIT_REJECTED.add(1, {"scope": scope})
            return scope
    if group_id is not None and cfg.group_daily_tokens > 0:
        used = await redis_client().get(f"ygg:tok:g:{g}:{_day()}")
        if used and int(used) >= cfg.group_daily_tokens:
            RATELIMIT_REJECTED.add(1, {"scope": "group_tokens"})
            return "group_tokens"
    return None


async def add_tokens(group_id: int | None, tokens: int) -> None:
    if group_id is None or tokens <= 0:
        return
    key = f"ygg:tok:g:{group_id}:{_day()}"
    pipe = redis_client().pipeline()
    pipe.incrby(key, tokens)
    pipe.expire(key, 2 * 86400, nx=True)
    await pipe.execute()


def _sem(kind: str, key: int | None, limit: int) -> asyncio.Semaphore:
    # Keyed by the limit too, so a changed setting takes effect for new requests.
    k = (kind, key, max(limit, 1))
    sem = _semaphores.get(k)
    if sem is None:
        sem = _semaphores[k] = asyncio.Semaphore(k[2])
    return sem


@contextlib.asynccontextmanager
async def slot(group_id: int | None, user_id: int, cfg: RateLimitSettings) -> AsyncIterator[bool]:
    """Wait for a run slot: at most ``max_concurrent_per_user`` runs per user and
    ``max_concurrent_per_group`` per group run at once, the rest queue. Yields False (without
    running) when the user already has ``max_queued_per_user`` requests waiting or the wait
    exceeds ``queue_timeout_s``."""
    user_key = (group_id, user_id)
    if _waiting[user_key] >= cfg.max_queued_per_user:
        RATELIMIT_REJECTED.add(1, {"scope": "user_queue"})
        yield False
        return
    user_sem = _sem("user", user_id, cfg.max_concurrent_per_user)
    group_sem = _sem("group", group_id, cfg.max_concurrent_per_group)
    _waiting[user_key] += 1
    acquired: list[asyncio.Semaphore] = []
    try:
        try:
            async with asyncio.timeout(cfg.queue_timeout_s):
                for sem in (user_sem, group_sem):
                    await sem.acquire()
                    acquired.append(sem)
        except TimeoutError:
            RATELIMIT_REJECTED.add(1, {"scope": "queue_timeout"})
            yield False
            return
        finally:
            _waiting[user_key] -= 1
        yield True
    finally:
        for sem in acquired:
            sem.release()


async def cooldown(rule_id: int, user_id: int, seconds: int) -> bool:
    """True if the user may trigger this rule now (and starts the cooldown)."""
    if seconds <= 0:
        return True
    return bool(await redis_client().set(f"ygg:cd:{rule_id}:{user_id}", "1", ex=seconds, nx=True))

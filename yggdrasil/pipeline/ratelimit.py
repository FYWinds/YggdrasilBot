"""Fixed-window rate limits and daily token budgets in Redis."""

import asyncio
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from yggdrasil.core.metrics import RATELIMIT_REJECTED
from yggdrasil.kv import redis_client
from yggdrasil.settings import RateLimitSettings

_TZ = ZoneInfo("Asia/Shanghai")
_inflight_users: set[tuple[int | None, int]] = set()
_group_slots: dict[int | None, int] = defaultdict(int)
_lock = asyncio.Lock()


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


async def acquire(group_id: int | None, user_id: int, cfg: RateLimitSettings) -> bool:
    """One in-flight run per user and at most ``max_concurrent_per_group`` per group (this process)."""
    async with _lock:
        if (group_id, user_id) in _inflight_users:
            RATELIMIT_REJECTED.add(1, {"scope": "user_inflight"})
            return False
        if _group_slots[group_id] >= max(cfg.max_concurrent_per_group, 1):
            RATELIMIT_REJECTED.add(1, {"scope": "group_concurrency"})
            return False
        _inflight_users.add((group_id, user_id))
        _group_slots[group_id] += 1
        return True


async def release(group_id: int | None, user_id: int) -> None:
    async with _lock:
        _inflight_users.discard((group_id, user_id))
        _group_slots[group_id] = max(0, _group_slots[group_id] - 1)


async def cooldown(rule_id: int, user_id: int, seconds: int) -> bool:
    """True if the user may trigger this rule now (and starts the cooldown)."""
    if seconds <= 0:
        return True
    return bool(await redis_client().set(f"ygg:cd:{rule_id}:{user_id}", "1", ex=seconds, nx=True))

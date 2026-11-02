"""The workspace records in Redis, shared by every bot replica: a hash per workspace (``Mapping``), the group's index
set, the keep hash of processes the reap spares, and the locks that serialize attaching, releasing and reserving.
A change that can race another invocation is one Lua script, applied only while the record still names the claim
the caller saw."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from yggdrasil.core.log import LOGGER
from yggdrasil.sandbox.job_model import job_key
from yggdrasil.sandbox.state import (
    INDEX_KEY_PREFIX,
    WS_KEY_PREFIX,
    Mapping,
    group_lock_key,
    index_key,
    keep_key,
    link_id,
    lock_key,
    parse_ws_key,
    ws_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable
    from contextlib import AbstractAsyncContextManager

    from redis.asyncio import Redis

_LOCK_TTL = 150
"""Seconds a workspace's Redis lock lives; covers the longest holder (creating its pod, ~``manager.READY_TIMEOUT``)."""
_GROUP_LOCK_TTL = 60
"""Seconds the per-group lock lives; reserving only reads records and deletes evicted claims."""
_PENDING_TTL = 1800
"""Seconds a pending record (no pod yet) lives unused; refreshed when its pod is being created."""
_LOCK_POLL = 0.2

# Every script takes KEYS[1] = the record, KEYS[2] = its keep hash, KEYS[3] = the group's index set where used.
# Remove a record (and its keep hash and index entry) only if it still names this claim ("" = pending).
_DROP_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return 0 end "
    "redis.call('DEL', KEYS[1], KEYS[2]) redis.call('SREM', KEYS[3], ARGV[2]) return 1"
)
# Change `active` only while the record still names this (non-empty) claim; returns the new value or -1.
_INCR_IF_CLAIM = (
    "if ARGV[1] ~= '' and redis.call('HGET', KEYS[1], 'claim') == ARGV[1] then "
    "return redis.call('HINCRBY', KEYS[1], 'active', ARGV[2]) end return -1"
)
# Write fields (ARGV[2..]) only while the record still names claim ARGV[1]: 1 = written, 0 = it moved on.
_SAVE_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return 0 end "
    "redis.call('HSET', KEYS[1], unpack(ARGV, 2)) return 1"
)
# Write fields (ARGV) only while the record exists.
_SAVE_IF_EXISTS = (
    "if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end redis.call('HSET', KEYS[1], unpack(ARGV)) return 1"
)
_PENDING_FIELDS = (
    "'claim', '', 'sandbox', '', 'pod_ip', '', 'active', '0', 'recreate', '0', 'links', '[]', "
    "'expires_at', '0', 'max_until', '0'"
)
# Destroy the pod of a record only if it still names claim ARGV[1] and at most ARGV[2] invocations use it:
# 1 = done (the record removed, or kept as pending with TTL ARGV[6] when ARGV[5] is 1), 0 = busy (and `recreate`
# marked when ARGV[3] is 1), 2 = background jobs run in it (checked only when ARGV[4] is 1; nothing marked),
# -1 = the record names another claim or is gone.
_DESTROY_IF_IDLE = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return -1 end "
    "if ARGV[4] == '1' then for _, f in ipairs(redis.call('HKEYS', KEYS[2])) do "
    "if string.sub(f, 1, 4) == 'job:' then return 2 end end end "
    "if tonumber(redis.call('HGET', KEYS[1], 'active') or '0') <= tonumber(ARGV[2]) then "
    "redis.call('DEL', KEYS[2]) "
    f"if ARGV[5] == '1' then redis.call('HSET', KEYS[1], {_PENDING_FIELDS}) "
    "redis.call('EXPIRE', KEYS[1], ARGV[6]) "
    "else redis.call('DEL', KEYS[1]) redis.call('SREM', KEYS[3], ARGV[7]) end return 1 end "
    "if ARGV[3] == '1' then redis.call('HSET', KEYS[1], 'recreate', '1') end return 0"
)
_RETIRE_ACTIVE = "1000000000"
"""``max_active`` that makes ``_DESTROY_IF_IDLE`` ignore users (retiring a record whose pod others still use)."""
# Refresh a pending record before its pod is created (TTL, LRU position); 0 when it is gone or not pending.
_TOUCH_PENDING = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= '' then return 0 end "
    "redis.call('EXPIRE', KEYS[1], ARGV[1]) redis.call('HSET', KEYS[1], 'last_used', ARGV[2]) return 1"
)
# Give a pending record its new pod (fields ARGV): 1 = done, 0 = the record was removed or claimed meanwhile.
_SET_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= '' then return 0 end "
    "redis.call('DEL', KEYS[2]) redis.call('HSET', KEYS[1], unpack(ARGV)) redis.call('PERSIST', KEYS[1]) return 1"
)
# Push max_until (ARGV[2]) out while the record names claim ARGV[1]; returns the resulting value or nil.
_EXTEND = (
    "if ARGV[1] == '' or redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return false end "
    "if tonumber(ARGV[2]) > (tonumber(redis.call('HGET', KEYS[1], 'max_until') or '0') or 0) then "
    "redis.call('HSET', KEYS[1], 'max_until', ARGV[2]) end return redis.call('HGET', KEYS[1], 'max_until')"
)
# Drop an index entry whose record is gone (KEYS[1] = record, KEYS[2] = index).
_PRUNE_INDEX = "if redis.call('EXISTS', KEYS[1]) == 0 then redis.call('SREM', KEYS[2], ARGV[1]) return 1 end return 0"
_UNLOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


class Records:
    """Every group's workspace records, through the Redis client ``redis`` returns."""

    def __init__(self, redis: Callable[[], Redis]) -> None:
        self._redis = redis

    def _r(self) -> Redis:
        return self._redis()

    # --- reading ----------------------------------------------------------------------------------------

    async def load(self, group_id: int, name: str) -> Mapping | None:
        data = cast(dict[str, str], await self._r().hgetall(ws_key(group_id, name)))  # pyright: ignore[reportGeneralTypeIssues]
        return Mapping.from_hash(group_id, data) if data else None

    async def group(self, group_id: int) -> list[Mapping]:
        """The group's records by name; index entries whose record is gone are pruned."""
        names = cast(set[str], await self._r().smembers(index_key(group_id)))  # pyright: ignore[reportGeneralTypeIssues]
        out: list[Mapping] = []
        for name in sorted(names):
            m = await self.load(group_id, name)
            if m is not None:
                out.append(m)
            else:  # expired (an unused reservation) or removed: only while the record is really gone
                await self._r().eval(_PRUNE_INDEX, 2, ws_key(group_id, name), index_key(group_id), name)  # pyright: ignore[reportGeneralTypeIssues]
        return out

    async def all(self) -> list[Mapping]:
        """Every group's records."""
        out: list[Mapping] = []
        async for key in self._r().scan_iter(match=f"{WS_KEY_PREFIX}*", count=100):
            parsed = parse_ws_key(str(key))
            if parsed is not None and (m := await self.load(*parsed)) is not None:
                out.append(m)
        return out

    async def job_ids(self, m: Mapping) -> tuple[str, ...]:
        """The background jobs running in the workspace (their entries in its keep hash)."""
        fields = cast(list[str], await self._r().hkeys(keep_key(m.group_id, m.name)))  # pyright: ignore[reportGeneralTypeIssues]
        return tuple(sorted(f.removeprefix("job:") for f in fields if f.startswith("job:")))

    async def job_titles(self, ids: Iterable[str]) -> dict[str, str]:
        return {i: str(await self._r().hget(job_key(i), "title") or "") for i in ids}  # pyright: ignore[reportGeneralTypeIssues]

    async def keep(self, group_id: int, name: str) -> dict[str, str]:
        """The workspace's keep hash: the process trees the reap spares (``kernel``, ``job:<id>`` -> pid)."""
        return cast(dict[str, str], await self._r().hgetall(keep_key(group_id, name)))  # pyright: ignore[reportGeneralTypeIssues]

    # --- writing ----------------------------------------------------------------------------------------

    async def add_pending(self, m: Mapping) -> None:
        """Store a new workspace's pending record (it expires unless its pod gets created) and index it."""
        async with self._r().pipeline(transaction=True) as pipe:
            pipe.hset(ws_key(m.group_id, m.name), mapping=m.to_hash())  # pyright: ignore[reportArgumentType]
            pipe.expire(ws_key(m.group_id, m.name), _PENDING_TTL)
            pipe.sadd(index_key(m.group_id), m.name)
            await pipe.execute()

    async def set_description(self, group_id: int, name: str, description: str) -> bool:
        done = await self._r().eval(_SAVE_IF_EXISTS, 1, ws_key(group_id, name), "description", description)  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, done)) == 1

    async def save(self, m: Mapping, *fields: str) -> bool:
        """Write ``fields`` while the record still names ``m.claim``; False when it moved on."""
        data = m.to_hash()
        flat = [x for k in fields for x in (k, data[k])]
        done = await self._r().eval(_SAVE_IF_CLAIM, 1, ws_key(m.group_id, m.name), m.claim, *flat)  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, done)) == 1

    async def incr_active(self, m: Mapping, delta: int) -> int:
        """Add ``delta`` to ``active`` while the record still names ``m.claim``: the new value, or -1."""
        value = await self._r().eval(_INCR_IF_CLAIM, 1, ws_key(m.group_id, m.name), m.claim, str(delta))  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, value))

    async def drop(self, m: Mapping) -> bool:
        """Remove the record (and its keep hash and index entry) if it still names ``m.claim``."""
        dropped = await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
            _DROP_IF_CLAIM,
            3,
            ws_key(m.group_id, m.name),
            keep_key(m.group_id, m.name),
            index_key(m.group_id),
            m.claim,
            m.name,
        )
        return bool(int(cast(int, dropped)))

    async def take_if_idle(
        self, m: Mapping, *, max_active: int, mark_recreate: bool, check_jobs: bool, keep_record: bool
    ) -> int:
        """Take the pod from the record if it still names ``m.claim`` and at most ``max_active`` invocations use it:
        1 (taken; the record removed, or left pending with ``keep_record``), 0 (in use; ``recreate`` marked with
        ``mark_recreate``), 2 (background jobs run in it, with ``check_jobs``) or -1 (the record names another
        claim or is gone)."""
        return int(
            cast(
                int,
                await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
                    _DESTROY_IF_IDLE,
                    3,
                    ws_key(m.group_id, m.name),
                    keep_key(m.group_id, m.name),
                    index_key(m.group_id),
                    m.claim,
                    str(max_active),
                    "1" if mark_recreate else "0",
                    "1" if check_jobs else "0",
                    "1" if keep_record else "0",
                    str(_PENDING_TTL),
                    m.name,
                ),
            )
        )

    async def retire(self, m: Mapping) -> None:
        """Make the record pending if it still names ``m.claim``, however many invocations use its pod."""
        await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
            _DESTROY_IF_IDLE,
            3,
            ws_key(m.group_id, m.name),
            keep_key(m.group_id, m.name),
            index_key(m.group_id),
            m.claim,
            _RETIRE_ACTIVE,
            "0",
            "0",
            "1",
            str(_PENDING_TTL),
            m.name,
        )

    async def touch_pending(self, group_id: int, name: str, now: float) -> bool:
        """Refresh a pending record whose pod is being created (TTL, LRU position); False when it is gone or no
        longer pending."""
        key = ws_key(group_id, name)
        return bool(int(cast(int, await self._r().eval(_TOUCH_PENDING, 1, key, str(_PENDING_TTL), repr(now)))))  # pyright: ignore[reportGeneralTypeIssues]

    async def set_claim(self, m: Mapping) -> bool:
        """Give the pending record its new pod (every field of ``m`` but name and description), clearing the keep
        hash; False when the record was removed or claimed meanwhile."""
        data = m.to_hash()
        flat = [x for k, v in data.items() if k not in ("name", "description") for x in (k, v)]
        key = ws_key(m.group_id, m.name)
        return bool(int(cast(int, await self._r().eval(_SET_CLAIM, 2, key, keep_key(m.group_id, m.name), *flat))))  # pyright: ignore[reportGeneralTypeIssues]

    async def extend(self, group_id: int, name: str, claim: str, until: float) -> bool:
        """Push ``max_until`` out to at least ``until`` while the record names ``claim``; False when it does not."""
        raw = await self._r().eval(_EXTEND, 1, ws_key(group_id, name), claim, repr(until))  # pyright: ignore[reportGeneralTypeIssues]
        return raw is not None

    async def set_kernel(self, group_id: int, name: str, pid: int | None) -> None:
        """Keep the workspace's IPython kernel ``pid`` from the reap; ``None`` forgets it."""
        key = keep_key(group_id, name)
        if pid is None:
            await self._r().hdel(key, "kernel")  # pyright: ignore[reportGeneralTypeIssues]
        else:
            await self._r().hset(key, "kernel", str(pid))  # pyright: ignore[reportGeneralTypeIssues]

    async def clear(self, mappings: Iterable[Mapping]) -> None:
        """Delete these records with their keep hashes, and every group's index."""
        for m in mappings:
            await self._r().delete(ws_key(m.group_id, m.name), keep_key(m.group_id, m.name))
        async for key in self._r().scan_iter(match=f"{INDEX_KEY_PREFIX}*", count=100):
            await self._r().delete(key)

    # --- locks ------------------------------------------------------------------------------------------

    @asynccontextmanager
    async def _lock(self, key: str, *, wait: float, ttl: int) -> AsyncIterator[bool]:
        """Hold a lock shared by all bot replicas, polling up to ``wait`` seconds; yields whether it was
        acquired."""
        token = link_id()
        deadline = time.monotonic() + wait
        while not (got := bool(await self._r().set(key, token, nx=True, ex=ttl))):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_LOCK_POLL)
        try:
            yield got
        finally:
            if got:
                try:
                    await asyncio.shield(self._r().eval(_UNLOCK, 1, key, token))  # pyright: ignore[reportGeneralTypeIssues, reportArgumentType]
                except Exception:
                    LOGGER.exception("releasing sandbox lock {} failed", key)

    def ws_lock(self, group_id: int, name: str, *, wait: float) -> AbstractAsyncContextManager[bool]:
        """``ygg:sandbox:lock:<gid>:<name>``: attaching and the last release run under it, so a release never
        reaps or recreates a workspace another invocation is just attaching to."""
        return self._lock(lock_key(group_id, name), wait=wait, ttl=_LOCK_TTL)

    def group_lock(self, group_id: int, *, wait: float) -> AbstractAsyncContextManager[bool]:
        """The group's lock: reserving a workspace runs under it, so the per-group cap holds across invocations
        and bot replicas."""
        return self._lock(group_lock_key(group_id), wait=wait, ttl=_GROUP_LOCK_TTL)

"""Registered worker tasks and their schedules (cron, Asia/Shanghai).

Per-group steps are isolated: one group's failure is logged and the rest still run;
the job then fails as a whole so ``ygg.job.runs{outcome="error"}`` reflects it. A Redis
lock per (job, group) keeps overlapping runs (slow LLM, several workers) from
processing the same group twice.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import LockError

from yggdrasil.core.log import LOGGER
from yggdrasil.core.telemetry import TRACER
from yggdrasil.db.partitions import ensure_partitions
from yggdrasil.db.session import engine
from yggdrasil.kv import redis_client
from yggdrasil.memory import vectors
from yggdrasil.memory.chunking import chunk_group
from yggdrasil.memory.common import memory_groups
from yggdrasil.memory.digest import digest_group
from yggdrasil.memory.glossary import define_group, mine_group
from yggdrasil.memory.profiles import update_group
from yggdrasil.memory.topics import consolidate_group
from yggdrasil.runtime import snapshot
from yggdrasil.stickers.tagging import tag_pending
from yggdrasil.worker.broker import TIMEZONE, broker


def _cron(expr: str) -> list[dict[str, str]]:
    return [{"cron": expr, "cron_offset": TIMEZONE}]


class GroupFailures(RuntimeError):
    def __init__(self, job: str, groups: list[int]) -> None:
        super().__init__(f"{job} failed for groups {groups}")
        self.groups = groups


async def for_each_group(job: str, step: Callable[[int], Awaitable[Any]], *, lock_ttl: int = 1800) -> dict[int, Any]:
    """Run ``step`` for every memory-enabled group, isolating failures."""
    results: dict[int, Any] = {}
    failed: list[int] = []
    for group_id in memory_groups(snapshot()):
        lock = redis_client().lock(f"ygg:job:{job}:{group_id}", timeout=lock_ttl, blocking=False)
        if not await lock.acquire():
            LOGGER.info("{}: group {} is still being processed; skipped", job, group_id)
            continue
        try:
            with TRACER.start_as_current_span(f"{job} group", attributes={"ygg.group_id": group_id}):
                results[group_id] = await step(group_id)
        except Exception:
            failed.append(group_id)
            LOGGER.exception("{}: group {} failed", job, group_id)
        finally:
            try:
                await lock.release()
            except LockError:
                LOGGER.warning("{}: lock for group {} expired before release", job, group_id)
    LOGGER.info("{}: {}", job, results)
    if failed:
        raise GroupFailures(job, failed)
    return results


@broker.task(task_name="maintain_partitions", schedule=_cron("10 3 * * *"))
async def maintain_partitions() -> list[str]:
    async with engine().begin() as conn:
        names = await ensure_partitions(conn, ahead=3)
    LOGGER.info("message partitions ensured: {}", names)
    return names


@broker.task(task_name="sweep_bot_events", schedule=_cron("20 3 * * *"))
async def sweep_bot_events() -> dict[str, int]:
    from yggdrasil.events import sweep

    stats = await sweep()
    LOGGER.info("bot events swept: {}", stats)
    return stats


async def _chunk_and_embed(group_id: int) -> dict[str, int]:
    stats = await chunk_group(group_id)
    space = await vectors.resolve_space()
    if space is not None:
        stats |= await vectors.embed_missing(space, group_id)
    return stats


@broker.task(task_name="chunk_and_embed", schedule=_cron("*/5 * * * *"))
async def chunk_and_embed() -> dict[int, Any]:
    return await for_each_group("chunk_and_embed", _chunk_and_embed, lock_ttl=900)


@broker.task(task_name="consolidate_topics", schedule=_cron("*/15 * * * *"))
async def consolidate_topics() -> dict[int, Any]:
    return await for_each_group("consolidate_topics", consolidate_group, lock_ttl=3600)


@broker.task(task_name="daily_digest", schedule=_cron("5 * * * *"))
async def daily_digest() -> dict[int, Any]:
    return await for_each_group("daily_digest", digest_group)


@broker.task(task_name="update_profiles", schedule=_cron("25 * * * *"))
async def update_profiles() -> dict[int, Any]:
    return await for_each_group("update_profiles", update_group, lock_ttl=3600)


@broker.task(task_name="mine_glossary", schedule=_cron("30 4 * * *"))
async def mine_glossary() -> dict[int, Any]:
    return await for_each_group("mine_glossary", mine_group)


@broker.task(task_name="define_glossary", schedule=_cron("0 5 * * *"))
async def define_glossary() -> dict[int, Any]:
    return await for_each_group("define_glossary", define_group)


@broker.task(task_name="tag_stickers", schedule=_cron("*/5 * * * *"))
async def tag_stickers() -> dict[str, int]:
    """Describe, tag and moderate newly collected stickers (task ``sticker``)."""
    lock = redis_client().lock("ygg:job:tag_stickers", timeout=1800, blocking=False)
    if not await lock.acquire():
        LOGGER.info("tag_stickers: previous run still going; skipped")
        return {}
    try:
        stats = await tag_pending()
    finally:
        try:
            await lock.release()
        except LockError:
            LOGGER.warning("tag_stickers: lock expired before release")
    LOGGER.info("tag_stickers: {}", stats)
    return stats


@broker.task(task_name="reembed_space")
async def reembed_space(space_id: int) -> dict[str, int]:
    """On demand (dashboard): embed all memory into ``space_id`` and switch to it."""
    lock = redis_client().lock("ygg:job:reembed_space", timeout=6 * 3600, blocking=False)
    if not await lock.acquire():
        raise RuntimeError("another re-embedding is running")
    try:
        return await vectors.reembed_space(space_id)
    finally:
        try:
            await lock.release()
        except LockError:
            LOGGER.warning("reembed_space: lock expired before release")

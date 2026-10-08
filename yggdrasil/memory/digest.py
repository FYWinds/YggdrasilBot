"""Daily per-group digest (task ``memory_digest``) built from that day's topics."""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import DailyDigest, Message, Topic
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import LOCAL_TZ, local
from yggdrasil.memory.vectors import digest_text, try_embed
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

TASK = "memory_digest"
_WEEKDAYS = "一二三四五六日"


def due_day(now: datetime, digest_hour: int) -> date | None:
    """The day whose digest is due at ``now``: yesterday (Asia/Shanghai) once ``digest_hour`` has passed."""
    local_now = local(now)
    if local_now.hour < digest_hour:
        return None
    return local_now.date() - timedelta(days=1)


async def build_digest(group_id: int, day: date) -> int | None:
    """Generate and store the digest of ``day``; None when there is nothing to summarize or it exists."""
    snap = snapshot()
    start = datetime.combine(day, time(), LOCAL_TZ)
    end = start + timedelta(days=1)
    async with get_session() as session:
        if await session.scalar(select(DailyDigest.id).where(DailyDigest.group_id == group_id, DailyDigest.day == day)):
            return None
        topics = (
            await session.scalars(
                select(Topic)
                .where(Topic.group_id == group_id, Topic.start_at < end, Topic.end_at >= start)
                .order_by(Topic.start_at)
            )
        ).all()
        if not topics:
            return None
        day_messages = (
            Message.group_id == group_id,
            Message.recalled.is_(False),
            Message.sent_at >= start,
            Message.sent_at < end,
        )
        count = await session.scalar(select(func.count()).select_from(Message).where(*day_messages)) or 0
        names = dict(
            (
                await session.execute(
                    select(Message.user_id, Message.sender_name)
                    .where(*day_messages)
                    .distinct(Message.user_id)
                    .order_by(Message.user_id, Message.sent_at.desc())
                )
            ).all()
        )
    prompt = render(
        TASK,
        day=day.isoformat(),
        weekday=f"周{_WEEKDAYS[day.weekday()]}",
        bot_name=snap.settings(group_id).persona.name,
        message_count=count,
        topics=[
            {
                "title": llm.fence(t.title),
                "summary": llm.fence(t.summary),
                "keywords": [llm.fence(k) for k in t.keywords],
                "start": f"{local(t.start_at):%H:%M}",
                "end": f"{local(t.end_at):%H:%M}",
                "msg_count": t.msg_count,
                "participants": [llm.fence(names.get(u) or str(u)) for u in t.participants],
            }
            for t in topics
        ],
    )
    summary = (await llm.run_structured(TASK, str, prompt)).strip()
    if not summary:
        LOGGER.warning("group {}: empty digest for {}", group_id, day)
        return None
    async with get_session() as session:
        digest_id = await session.scalar(
            pg_insert(DailyDigest)
            .values(group_id=group_id, day=day, summary=summary, topic_ids=[t.id for t in topics])
            .on_conflict_do_nothing(index_elements=["group_id", "day"])
            .returning(DailyDigest.id)
        )
    if digest_id is not None:
        await try_embed("digest", [(digest_id, group_id, digest_text(day, summary))])
    return digest_id


async def digest_group(group_id: int, *, now: datetime | None = None) -> int | None:
    """Hourly entry point: builds yesterday's digest once ``digest_hour`` has passed."""
    day = due_day(now or datetime.now(UTC), snapshot().settings(group_id).memory.digest_hour)
    if day is None:
        return None
    if not llm.available(TASK):
        LOGGER.debug("group {}: task {} not configured", group_id, TASK)
        return None
    return await build_digest(group_id, day)

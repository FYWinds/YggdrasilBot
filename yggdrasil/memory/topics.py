"""Topic segmentation (task ``memory_topic``).

New messages after the watermark are given to the model together with the open
topics; it answers with ops (continue an open topic / start a new one). Idle topics
are closed, and topics marked dirty by a recall are recomputed without the recalled
messages.
"""

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Topic, TopicMessage
from yggdrasil.db.session import get_session
from yggdrasil.memory import llm
from yggdrasil.memory.common import format_line, pipeline_state
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.memory.vectors import delete_vectors, topic_text, try_embed
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot

PIPELINE = "topic"
TASK = "memory_topic"
MAX_WINDOWS_PER_RUN = 5
MAX_OPEN_TOPICS = 10
MAX_DIRTY_PER_RUN = 10
LINE_MAX_CHARS = 300
TOPIC_CONTEXT_LINES = 3


class ContinueTopic(BaseModel):
    """Messages that continue an open topic."""

    op: Literal["continue"] = "continue"
    topic_id: int
    message_ids: list[int]
    title: str | None = Field(default=None, description="新标题；不变则省略")
    summary: str | None = Field(default=None, description="覆盖整个话题的新摘要；无新进展则省略")
    keywords: list[str] | None = Field(default=None, description="新关键词；不变则省略")


class NewTopic(BaseModel):
    """A new topic started in this window."""

    op: Literal["new"] = "new"
    title: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    participants: list[int] = Field(default_factory=list, description="发言者 QQ 号")
    message_ids: list[int]


TopicOp = Annotated[ContinueTopic | NewTopic, Field(discriminator="op")]


class TopicOps(BaseModel):
    ops: list[TopicOp]


class TopicRewrite(BaseModel):
    title: str
    summary: str
    keywords: list[str] = Field(default_factory=list)


def validate_ops(
    ops: Sequence[ContinueTopic | NewTopic], window_ids: Collection[int], open_topic_ids: Collection[int]
) -> list[ContinueTopic | NewTopic]:
    """Drop message ids outside the window or already assigned, ops on unknown topics, untitled
    new topics, and ops left without messages. Earlier ops win a contested message."""
    window, open_ids = set(window_ids), set(open_topic_ids)
    assigned: set[int] = set()
    out: list[ContinueTopic | NewTopic] = []
    for op in ops:
        if isinstance(op, ContinueTopic) and op.topic_id not in open_ids:
            continue
        if isinstance(op, NewTopic) and not op.title.strip():
            continue
        ids = [i for i in dict.fromkeys(op.message_ids) if i in window and i not in assigned]
        if not ids:
            continue
        assigned.update(ids)
        out.append(op.model_copy(update={"message_ids": ids}))
    return out


def participants_of(
    ids: Sequence[int], views: Mapping[int, MessageView], claimed: Sequence[int] = (), *, existing: Sequence[int] = ()
) -> list[int]:
    """Senders of ``ids`` (bot excluded) plus model-claimed users that actually spoke in the window."""
    speakers = {v.user_id for v in views.values() if not v.is_self}
    out = list(existing)
    for uid in [*(views[i].user_id for i in ids if not views[i].is_self), *claimed]:
        if uid in speakers and uid not in out:
            out.append(uid)
    return out


def _line(v: MessageView) -> str:
    return llm.fence(format_line(v, with_id=True, with_uid=True)[:LINE_MAX_CHARS])


async def _topic_messages(session: AsyncSession, topic: Topic, *, last: int | None = None) -> list[MessageView]:
    q = (
        select(*VIEW_COLUMNS)
        .join(TopicMessage, TopicMessage.message_id == Message.id)
        .where(
            TopicMessage.topic_id == topic.id,
            Message.group_id == topic.group_id,
            Message.recalled.is_(False),
            Message.sent_at.between(topic.start_at, topic.end_at),
        )
    )
    if last is not None:
        rows = (await session.execute(q.order_by(Message.id.desc()).limit(last))).all()
        return [MessageView(*r) for r in reversed(rows)]
    return [MessageView(*r) for r in (await session.execute(q.order_by(Message.id))).all()]


async def _link(session: AsyncSession, topic_id: int, ids: Sequence[int]) -> None:
    await session.execute(
        pg_insert(TopicMessage).values([{"topic_id": topic_id, "message_id": i} for i in ids]).on_conflict_do_nothing()
    )


async def _apply(
    session: AsyncSession, group_id: int, ops: Sequence[ContinueTopic | NewTopic], views: Mapping[int, MessageView]
) -> list[Topic]:
    changed: list[Topic] = []
    for op in ops:
        msgs = [views[i] for i in op.message_ids]
        first, last = min(m.id for m in msgs), max(m.id for m in msgs)
        start, end = min(m.sent_at for m in msgs), max(m.sent_at for m in msgs)
        if isinstance(op, NewTopic):
            topic = Topic(
                group_id=group_id,
                title=op.title.strip()[:60],
                summary=op.summary.strip(),
                keywords=[k.strip() for k in op.keywords if k.strip()][:10],
                participants=participants_of(op.message_ids, views, op.participants),
                start_at=start,
                end_at=end,
                first_msg_id=first,
                last_msg_id=last,
                msg_count=len(msgs),
                status="open",
            )
            session.add(topic)
            await session.flush()
            await _link(session, topic.id, op.message_ids)
        else:
            topic = await session.get(Topic, op.topic_id)
            if topic is None or topic.group_id != group_id:
                continue
            await _link(session, topic.id, op.message_ids)
            topic.start_at, topic.end_at = min(topic.start_at, start), max(topic.end_at, end)
            topic.first_msg_id, topic.last_msg_id = min(topic.first_msg_id, first), max(topic.last_msg_id, last)
            topic.msg_count = (
                await session.scalar(
                    select(func.count()).select_from(TopicMessage).where(TopicMessage.topic_id == topic.id)
                )
                or 0
            )
            topic.participants = participants_of(op.message_ids, views, existing=topic.participants)
            if op.title and op.title.strip():
                topic.title = op.title.strip()[:60]
            if op.summary and op.summary.strip():
                topic.summary = op.summary.strip()
            if op.keywords:
                topic.keywords = [k.strip() for k in op.keywords if k.strip()][:10]
            topic.status = "open"
        changed.append(topic)
    return changed


async def _segment_window(group_id: int, force: bool) -> tuple[int, int, int] | None:
    """Process one window after the watermark. Returns (messages, new, continued) or None if not due."""
    snap = snapshot()
    mem = snap.settings(group_id).memory
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, group_id)
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .where(Message.group_id == group_id, Message.recalled.is_(False), Message.id > st.last_msg_id)
            .order_by(Message.id)
            .limit(mem.topic_window_messages)
        )
        loaded = [MessageView(*r) for r in rows]
        window = [v for v in loaded if v.text.strip()]
        if not loaded or (len(window) < mem.topic_min_new_messages and not force):
            return None
        open_rows = (
            await session.scalars(
                select(Topic)
                .where(Topic.group_id == group_id, Topic.status == "open")
                .order_by(Topic.end_at.desc())
                .limit(MAX_OPEN_TOPICS)
            )
        ).all()
        open_topics = [
            {
                "id": t.id,
                "title": llm.fence(t.title),
                "summary": llm.fence(t.summary),
                "keywords": [llm.fence(k) for k in t.keywords],
                "last_lines": [_line(v) for v in await _topic_messages(session, t, last=TOPIC_CONTEXT_LINES)],
            }
            for t in open_rows
        ]
    views = {v.id: v for v in window}
    ops: list[ContinueTopic | NewTopic] = []
    if window:
        prompt = render(
            TASK,
            mode="segment",
            bot_name=snap.settings(group_id).persona.name,
            open_topics=open_topics,
            messages="\n".join(_line(v) for v in window),
        )
        result = await llm.run_structured(TASK, TopicOps, prompt)
        ops = validate_ops(result.ops, views.keys(), [t["id"] for t in open_topics])
    async with get_session() as session:
        changed = await _apply(session, group_id, ops, views)
        await delete_vectors(session, "topic", [t.id for t in changed])
        st = await pipeline_state(session, PIPELINE, group_id)
        st.last_msg_id = loaded[-1].id
    await try_embed("topic", [(t.id, group_id, topic_text(t.title, t.summary, t.keywords)) for t in changed])
    new = sum(isinstance(op, NewTopic) for op in ops)
    return len(window), new, len(ops) - new


async def _recompute_dirty(group_id: int) -> tuple[int, int]:
    """Rewrite topics that lost messages to recalls. Returns (recomputed, deleted)."""
    snap = snapshot()
    recomputed = deleted = 0
    async with get_session() as session:
        dirty = (
            await session.scalars(
                select(Topic).where(Topic.group_id == group_id, Topic.dirty.is_(True)).limit(MAX_DIRTY_PER_RUN)
            )
        ).all()
    for topic in dirty:
        async with get_session() as session:
            remaining = await _topic_messages(session, topic)
        rewrite: TopicRewrite | None = None
        if remaining:
            prompt = render(
                TASK,
                mode="recompute",
                bot_name=snap.settings(group_id).persona.name,
                topic={"id": topic.id, "title": llm.fence(topic.title), "summary": llm.fence(topic.summary)},
                messages="\n".join(_line(v) for v in remaining),
            )
            rewrite = await llm.run_structured(TASK, TopicRewrite, prompt)
        async with get_session() as session:
            await delete_vectors(session, "topic", [topic.id])
            if not remaining or rewrite is None:
                await session.execute(delete(Topic).where(Topic.id == topic.id))
                deleted += 1
                continue
            keep = [v.id for v in remaining]
            await session.execute(
                delete(TopicMessage).where(TopicMessage.topic_id == topic.id, TopicMessage.message_id.notin_(keep))
            )
            speakers = list(dict.fromkeys(v.user_id for v in remaining if not v.is_self))
            await session.execute(
                update(Topic)
                .where(Topic.id == topic.id)
                .values(
                    title=rewrite.title.strip()[:60] or topic.title,
                    summary=rewrite.summary.strip() or topic.summary,
                    keywords=[k.strip() for k in rewrite.keywords if k.strip()][:10] or topic.keywords,
                    participants=[u for u in topic.participants if u in speakers],
                    start_at=min(v.sent_at for v in remaining),
                    end_at=max(v.sent_at for v in remaining),
                    first_msg_id=remaining[0].id,
                    last_msg_id=remaining[-1].id,
                    msg_count=len(remaining),
                    dirty=False,
                )
            )
            text = topic_text(rewrite.title, rewrite.summary, rewrite.keywords)
        recomputed += 1
        await try_embed("topic", [(topic.id, group_id, text)])
    return recomputed, deleted


async def consolidate_group(group_id: int, *, now: datetime | None = None) -> dict[str, int]:
    """One consolidation pass for a group (see module docstring). Only runs when due:
    enough new messages, idle open topics, or dirty topics."""
    now = now or datetime.now(UTC)
    mem = snapshot().settings(group_id).memory
    idle_before = now - timedelta(minutes=mem.topic_close_after_minutes)
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, group_id)
        pending = await session.scalar(
            select(func.count()).select_from(
                select(Message.id)
                .where(Message.group_id == group_id, Message.recalled.is_(False), Message.id > st.last_msg_id)
                .limit(mem.topic_min_new_messages)
                .subquery()
            )
        )
        idle = await session.scalar(
            select(exists().where(Topic.group_id == group_id, Topic.status == "open", Topic.end_at < idle_before))
        )
        dirty = await session.scalar(select(exists().where(Topic.group_id == group_id, Topic.dirty.is_(True))))
    stats = {"messages": 0, "new": 0, "continued": 0, "recomputed": 0, "deleted": 0, "closed": 0}
    if not (pending or 0) >= mem.topic_min_new_messages and not idle and not dirty:
        return stats
    if not llm.available(TASK):
        LOGGER.info("group {}: task {} not configured; only closing idle topics", group_id, TASK)
    else:
        force = bool(idle and pending)
        for _ in range(MAX_WINDOWS_PER_RUN):
            done = await _segment_window(group_id, force)
            if done is None:
                break
            force = False
            stats["messages"] += done[0]
            stats["new"] += done[1]
            stats["continued"] += done[2]
        if dirty:
            stats["recomputed"], stats["deleted"] = await _recompute_dirty(group_id)
    async with get_session() as session:
        result = await session.execute(
            update(Topic)
            .where(Topic.group_id == group_id, Topic.status == "open", Topic.end_at < idle_before)
            .values(status="closed")
        )
        stats["closed"] = result.rowcount or 0  # type: ignore[attr-defined]
    return stats

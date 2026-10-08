"""Conversation chunks: the unit of vector search over raw history.

Messages are cut into chunks at time gaps and size limits. The trailing chunk stays
open until its last message is older than the gap, so a conversation in progress is
re-chunked on a later run instead of being split mid-way.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.db.models import Message, MessageChunk
from yggdrasil.db.session import get_session
from yggdrasil.memory.common import format_line, pipeline_state
from yggdrasil.memory.search import VIEW_COLUMNS, MessageView
from yggdrasil.memory.vectors import delete_vectors
from yggdrasil.runtime import snapshot

PIPELINE = "chunk"
BATCH = 2000
RECALL_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Chunk:
    messages: tuple[MessageView, ...]
    text: str

    @property
    def first_id(self) -> int:
        return self.messages[0].id

    @property
    def last_id(self) -> int:
        return self.messages[-1].id

    @property
    def start_at(self) -> datetime:
        return min(m.sent_at for m in self.messages)

    @property
    def end_at(self) -> datetime:
        return max(m.sent_at for m in self.messages)


@dataclass(slots=True)
class ChunkPlan:
    chunks: list[Chunk] = field(default_factory=list)
    # Id of the last message not belonging to the open tail; None if nothing was consumed.
    watermark: int | None = None


def render(messages: Sequence[MessageView]) -> list[str]:
    return [format_line(m) for m in messages]


def split_chunks(
    messages: Sequence[MessageView],
    *,
    gap: timedelta,
    max_messages: int,
    max_chars: int,
    now: datetime,
    more_pending: bool = False,
) -> ChunkPlan:
    """Cut id-ordered, non-recalled messages into closed chunks.

    A chunk ends when the next message is more than ``gap`` later, or when adding it would
    exceed ``max_messages`` / ``max_chars``. The last chunk is closed only if nothing follows
    (``more_pending`` False) and its last message is older than ``gap`` relative to ``now``.
    Messages with empty text are skipped.
    """
    plan = ChunkPlan()
    current: list[tuple[int, MessageView, str]] = []  # (index in input, message, line)
    chars = 0

    def close() -> None:
        plan.chunks.append(Chunk(tuple(m for _, m, _ in current), "\n".join(line for _, _, line in current)))

    for i, m in enumerate(messages):
        if not m.text.strip():
            continue
        line = format_line(m)[:max_chars]
        if current and (
            m.sent_at - current[-1][1].sent_at > gap
            or len(current) >= max_messages
            or chars + len(line) + 1 > max_chars
        ):
            close()
            current, chars = [], 0
        current.append((i, m, line))
        chars += len(line) + 1
    if current and not more_pending and now - current[-1][1].sent_at > gap:
        close()
        current = []
    # Everything before the open tail is consumed (including skipped empty messages).
    if current:
        first_open = current[0][0]
        plan.watermark = messages[first_open - 1].id if first_open > 0 else None
    elif messages:
        plan.watermark = messages[-1].id
    return plan


async def _rechunk_recalled(session: AsyncSession, group_id: int, checked_at: datetime) -> int:
    """Rebuild chunks containing messages recalled since ``checked_at``; returns chunks touched."""
    q = select(Message.id).where(
        Message.group_id == group_id,
        Message.recalled.is_(True),
        Message.recalled_at > checked_at,
        Message.sent_at >= checked_at - RECALL_LOOKBACK,
    )
    recalled_ids = list((await session.scalars(q)).all())
    if not recalled_ids:
        return 0
    chunks: dict[int, MessageChunk] = {}
    for mid in recalled_ids:
        for c in await session.scalars(
            select(MessageChunk).where(
                MessageChunk.group_id == group_id,
                MessageChunk.first_msg_id <= mid,
                MessageChunk.last_msg_id >= mid,
            )
        ):
            chunks[c.id] = c
    for c in chunks.values():
        rows = await session.execute(
            select(*VIEW_COLUMNS)
            .where(
                Message.group_id == group_id,
                Message.recalled.is_(False),
                Message.id.between(c.first_msg_id, c.last_msg_id),
                Message.sent_at.between(c.start_at, c.end_at),
            )
            .order_by(Message.id)
        )
        views = [MessageView(*r) for r in rows if r.plain_text.strip()]
        await delete_vectors(session, "chunk", [c.id])
        if not views:
            await session.execute(delete(MessageChunk).where(MessageChunk.id == c.id))
            continue
        c.text = "\n".join(render(views))
        c.first_msg_id, c.last_msg_id, c.msg_count = views[0].id, views[-1].id, len(views)
        c.start_at, c.end_at = min(v.sent_at for v in views), max(v.sent_at for v in views)
    return len(chunks)


async def chunk_group(group_id: int, *, now: datetime | None = None) -> dict[str, int]:
    """Persist newly closed chunks for one group and rebuild chunks hit by recalls.

    Vectors are filled afterwards by :func:`yggdrasil.memory.vectors.embed_missing`.
    """
    mem = snapshot().settings(group_id).memory
    gap = timedelta(minutes=mem.chunk_gap_minutes)
    created = 0
    while True:
        started = now or datetime.now(UTC)
        async with get_session() as session:
            st = await pipeline_state(session, PIPELINE, group_id)
            rows = await session.execute(
                select(*VIEW_COLUMNS)
                .where(Message.group_id == group_id, Message.recalled.is_(False), Message.id > st.last_msg_id)
                .order_by(Message.id)
                .limit(BATCH)
            )
            views = [MessageView(*r) for r in rows]
            plan = split_chunks(
                views,
                gap=gap,
                max_messages=mem.chunk_max_messages,
                max_chars=mem.chunk_max_chars,
                now=started,
                more_pending=len(views) == BATCH,
            )
            session.add_all(
                MessageChunk(
                    group_id=group_id,
                    first_msg_id=c.first_id,
                    last_msg_id=c.last_id,
                    start_at=c.start_at,
                    end_at=c.end_at,
                    msg_count=len(c.messages),
                    text=c.text,
                )
                for c in plan.chunks
            )
            if plan.watermark is not None:
                st.last_msg_id = plan.watermark
        created += len(plan.chunks)
        if len(views) < BATCH or plan.watermark is None:
            break

    rebuilt = 0
    async with get_session() as session:
        st = await pipeline_state(session, PIPELINE, group_id)
        checked = st.state.get("recall_checked_at")
        started = datetime.now(UTC)
        # On the first run every chunk was just built without recalled messages.
        if checked:
            rebuilt = await _rechunk_recalled(session, group_id, datetime.fromisoformat(checked))
        st.state = {**st.state, "recall_checked_at": started.isoformat()}
    return {"created": created, "rebuilt": rebuilt}

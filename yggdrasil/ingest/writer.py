"""Batched persistence of group messages (10k+/day) plus recall marking."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import MESSAGES_INGESTED
from yggdrasil.db.models import Message, Sticker, UserAlias
from yggdrasil.db.partitions import ensure_month
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import normalize


@dataclass(slots=True)
class IncomingMessage:
    group_id: int
    user_id: int
    qq_message_id: int
    message_seq: int | None
    sent_at: datetime
    sender_name: str
    segments: list[dict[str, Any]]
    is_self: bool = False


def _kind(segments: list[dict[str, Any]]) -> str:
    types = {s.get("type") for s in segments}
    for kind in ("forward", "file", "image", "mface", "record", "video", "json"):
        if kind in types:
            return kind
    return "text"


class IngestWriter:
    def __init__(self, flush_interval: float = 1.0, max_batch: int = 200) -> None:
        self._queue: list[IncomingMessage] = []
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._flush_interval = flush_interval
        self._max_batch = max_batch

    def submit(self, msg: IncomingMessage) -> None:
        self._queue.append(msg)
        MESSAGES_INGESTED.add(1, {"group": str(msg.group_id), "kind": _kind(msg.segments)})
        if len(self._queue) >= self._max_batch:
            self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("ingest flush failed; {} messages kept for retry", len(self._queue))

    async def flush(self) -> int:
        async with self._lock:
            if not self._queue:
                return 0
            batch, self._queue = self._queue, []
            try:
                await self._write(batch)
            except BaseException:
                self._queue[:0] = batch  # keep order, retry next tick
                raise
            return len(batch)

    async def _write(self, batch: list[IncomingMessage]) -> None:
        rows, aliases, stickers = [], {}, {}
        for m in batch:
            norm = normalize(m.segments)
            rows.append(
                {
                    "sent_at": m.sent_at,
                    "group_id": m.group_id,
                    "user_id": m.user_id,
                    "qq_message_id": m.qq_message_id,
                    "message_seq": m.message_seq,
                    "reply_to_qq_id": norm.reply_to,
                    "sender_name": m.sender_name,
                    "plain_text": norm.text,
                    "segments": m.segments,
                    "is_self": m.is_self,
                }
            )
            if m.sender_name and not m.is_self:
                aliases[(m.group_id, m.user_id, m.sender_name)] = m.sent_at
            for att in norm.attachments:
                d = att.data
                if att.kind == "mface" and d.get("emoji_id") and d.get("emoji_package_id"):
                    stickers[(str(d["emoji_package_id"]), str(d["emoji_id"]))] = (
                        str(d.get("key", "")),
                        str(d.get("summary") or "").strip("[]"),
                    )
        async with get_session() as session:
            conn = await session.connection()
            for month in {r["sent_at"].replace(day=1, hour=0, minute=0, second=0, microsecond=0) for r in rows}:
                await ensure_month(conn, month)
            await session.execute(insert(Message), rows)
            if aliases:
                stmt = insert(UserAlias).values(
                    [{"group_id": g, "user_id": u, "name": n, "last_seen": t} for (g, u, n), t in aliases.items()]
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["group_id", "user_id", "name"], set_={"last_seen": stmt.excluded.last_seen}
                    )
                )
            if stickers:
                stmt = insert(Sticker).values(
                    [{"package_id": p, "emoji_id": e, "key": k, "summary": s} for (p, e), (k, s) in stickers.items()]
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["package_id", "emoji_id"],
                        set_={
                            "seen_count": Sticker.seen_count + 1,
                            "last_seen": func.now(),
                            "key": stmt.excluded.key,
                            "summary": stmt.excluded.summary,
                        },
                    )
                )

    async def mark_recalled(self, group_id: int, qq_message_id: int) -> None:
        await self.flush()
        async with get_session() as session:
            # Recent window only: qq ids repeat over long periods.
            result = await session.execute(
                update(Message)
                .where(
                    Message.group_id == group_id,
                    Message.qq_message_id == qq_message_id,
                    Message.sent_at > func.now() - text("interval '7 days'"),
                )
                .values(recalled=True, recalled_at=datetime.now(UTC))
                .returning(Message.id)
            )
            ids = [r[0] for r in result]
            if ids:
                # Topics containing the message get recomputed by the next consolidation pass.
                await session.execute(
                    text(
                        "UPDATE topics SET dirty = true WHERE id IN "
                        "(SELECT topic_id FROM topic_messages WHERE message_id = ANY(:ids))"
                    ),
                    {"ids": ids},
                )


WRITER = IngestWriter()

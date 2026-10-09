"""Import recent group history via NapCat ``get_group_msg_history`` (throttled)."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from yggdrasil.bot.state import any_bot
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Group, Message
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.runtime import snapshot

_PAGE = 50
_PAUSE_S = 1.5
_running: dict[int, asyncio.Task] = {}


def request_backfill(group_id: int, *, before: datetime | None = None) -> bool:
    """Start a background backfill; False if one is already running for the group."""
    task = _running.get(group_id)
    if task and not task.done():
        return False
    _running[group_id] = asyncio.create_task(backfill(group_id, before=before), name=f"backfill-{group_id}")
    return True


async def backfill(group_id: int, *, before: datetime | None = None) -> int:
    """Import up to ``memory.backfill_messages`` of history, skipping what is stored.

    With ``before`` it is a catch-up after a reconnect: only messages sent before then (newer ones
    arrive live), stopping at the first page that reaches stored history.
    """
    bot = any_bot()
    if bot is None:
        LOGGER.warning("backfill {}: no bot connected", group_id)
        return 0
    api = NapCatAPI(bot)
    target = snapshot().settings(group_id).memory.backfill_messages
    # Our own sends are recorded without a message_seq, so match on message id too.
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Message.message_seq, Message.qq_message_id).where(Message.group_id == group_id)
            )
        ).all()
    known = {r.message_seq for r in rows if r.message_seq is not None}
    known_ids = {r.qq_message_id for r in rows}
    seen: set[int] = set()
    imported = 0
    cursor: int | None = None
    while len(seen) < target:
        try:
            history = await api.get_group_msg_history(group_id=group_id, count=_PAGE, message_seq=cursor)
        except Exception as e:
            LOGGER.warning("backfill {} page failed: {}", group_id, e)
            break
        batch = [m for m in history.messages if m.message_seq is not None and m.message_seq not in seen]
        if not batch:
            break
        reached_known = False
        for m in batch:
            seq = m.message_seq
            assert seq is not None
            seen.add(seq)
            if seq in known or (m.message_id is not None and m.message_id in known_ids):
                reached_known = True
                continue
            if m.message_id is None or m.user_id is None:
                continue
            if before is not None and m.time >= before.timestamp():
                continue
            WRITER.submit(
                IncomingMessage(
                    group_id=group_id,
                    user_id=m.user_id,
                    qq_message_id=m.message_id,
                    message_seq=seq,
                    sent_at=datetime.fromtimestamp(m.time, UTC),
                    sender_name=m.sender.card or m.sender.nickname,
                    segments=[s.model_dump() for s in m.segments],
                    is_self=str(m.user_id) == bot.self_id,
                )
            )
            imported += 1
        oldest = min(m.message_seq for m in batch if m.message_seq is not None)
        if (cursor is not None and oldest >= cursor) or (before is not None and reached_known):
            break
        cursor = oldest
        await WRITER.flush()
        await asyncio.sleep(_PAUSE_S)
    await WRITER.flush()
    if before is None:
        async with get_session() as session:
            await session.execute(
                update(Group).where(Group.group_id == group_id).values(backfilled_at=datetime.now(UTC))
            )
    LOGGER.info("backfill {}: imported {} messages ({} scanned)", group_id, imported, len(seen))
    return imported

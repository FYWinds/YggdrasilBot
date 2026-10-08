"""Shared helpers for the memory pipelines: local time, transcript lines, watermarks."""

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.db.models import PipelineState
from yggdrasil.runtime import Snapshot

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def format_line(m: "MessageView", *, with_id: bool = False, with_uid: bool = False) -> str:
    """``[MM-DD HH:MM] 昵称: 内容``, optionally prefixed with ``#<message id>`` and the sender's QQ."""
    name = m.sender_name or str(m.user_id)
    if with_uid:
        name = f"{name}({m.user_id})"
    line = f"[{local(m.sent_at):%m-%d %H:%M}] {name}: {m.text}"
    return f"#{m.id} {line}" if with_id else line


def memory_groups(snap: Snapshot) -> list[int]:
    """Whitelisted groups whose effective settings keep memory enabled."""
    return sorted(g.group_id for g in snap.groups.values() if g.enabled and snap.settings(g.group_id).memory.enabled)


async def pipeline_state(session: AsyncSession, pipeline: str, group_id: int) -> PipelineState:
    """Watermark row for ``(pipeline, group)``; created (pending flush) when missing."""
    st = await session.get(PipelineState, (pipeline, group_id))
    if st is None:
        st = PipelineState(pipeline=pipeline, group_id=group_id, last_msg_id=0, state={})
        session.add(st)
    return st

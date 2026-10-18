"""Shared helpers for the memory pipelines: local time, transcript lines, watermarks."""

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.db.models import PipelineState
from yggdrasil.people import People, resolve
from yggdrasil.runtime import Snapshot

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def format_line(m: "MessageView", people: People, *, with_id: bool = False) -> str:
    """``[MM-DD HH:MM] 名字(QQ): 内容`` (the bot as ``{bot_name}(你)``, mentions as ``@名字(QQ)``),
    optionally prefixed with ``#<message id>``."""
    who = people.who(m.user_id, m.sender_name, is_self=m.is_self)
    line = f"[{local(m.sent_at):%m-%d %H:%M}] {who}: {people.mentions(m.text)}"
    return f"#{m.id} {line}" if with_id else line


async def people_for(
    group_id: int, views: "Iterable[MessageView]", *, bot_name: str, user_ids: Iterable[int] = ()
) -> People:
    """Names for rendering ``views``: their senders, their @mentions and ``user_ids``, in one lookup."""
    views = list(views)
    self_id = next((v.user_id for v in views if v.is_self), None)
    return await resolve(
        group_id,
        [*(v.user_id for v in views), *user_ids],
        bot_name=bot_name,
        self_id=self_id,
        texts=[v.text for v in views],
    )


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

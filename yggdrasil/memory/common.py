"""Shared helpers for the memory pipelines: local time, transcript lines, watermarks, curation weights."""

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, ColumnExpressionArgument, Float, cast, extract, func
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.db.models import PipelineState, UserProfileFact
from yggdrasil.people import People, resolve
from yggdrasil.runtime import Snapshot

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
# Ranking decays weights with age: × (DECAY_FLOOR + (1 - DECAY_FLOOR) · ½^(age / half-life)), so the oldest
# rows keep half their weight and importance still outranks recency.
DECAY_FLOOR = 0.5
# Facts not scored yet rank like a fact of importance 3 backed by one message.
UNSCORED_FACT_WEIGHT = 3.0


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


def topic_weight(importance: int, msg_count: int, participants: int) -> float:
    """Curation weight of a topic: its importance scaled up by size (×1.6 for 50 messages from 9 people)."""
    return round(importance * (1 + 0.1 * math.log1p(msg_count) + 0.1 * math.log1p(participants)), 2)


def fact_weight(importance: int, sources: int) -> float:
    """Curation weight of a fact: its importance, a little more when several messages back it."""
    return round(importance * (1 + 0.1 * math.log(max(sources, 1))), 2)


def decay(age: timedelta, half_life_days: float) -> float:
    """Recency factor of a weight: 1 when new, 0.75 after ``half_life_days``, never below ``DECAY_FLOOR``."""
    days = max(age.total_seconds(), 0.0) / 86400
    return DECAY_FLOOR + (1 - DECAY_FLOOR) * 0.5 ** (days / half_life_days)


def sql_decay(at: ColumnExpressionArgument[datetime], half_life_days: float) -> ColumnElement[float]:
    """:func:`decay` of ``now() - at``, in SQL."""
    days = func.greatest(cast(extract("epoch", func.now() - at), Float) / 86400, 0.0)
    return DECAY_FLOOR + (1 - DECAY_FLOOR) * func.power(0.5, days / half_life_days)


def fact_rank(half_life_days: float) -> ColumnElement[float]:
    """How facts are ranked everywhere (profile tool, profile updater, per-user cap): weight with recency decay."""
    return func.coalesce(UserProfileFact.weight, UNSCORED_FACT_WEIGHT) * sql_decay(
        UserProfileFact.created_at, half_life_days
    )


async def pipeline_state(session: AsyncSession, pipeline: str, group_id: int) -> PipelineState:
    """Watermark row for ``(pipeline, group)``; created (pending flush) when missing."""
    st = await session.get(PipelineState, (pipeline, group_id))
    if st is None:
        st = PipelineState(pipeline=pipeline, group_id=group_id, last_msg_id=0, state={})
        session.add(st)
    return st

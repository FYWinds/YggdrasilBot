"""Who may make the bot act: decided in code from the event, never by the model."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Literal

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from sqlalchemy import select

from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session

Role = Literal["member", "admin", "owner", "superuser"]
# QQ message ids are 32-bit hashes that repeat; only trust recent matches.
_RECENT = timedelta(days=7)


def speaker_role(event: MessageEvent, superusers: Collection[int]) -> Role:
    if event.user_id in superusers:
        return "superuser"
    if isinstance(event, GroupMessageEvent) and event.sender.role in ("owner", "admin"):
        return event.sender.role
    return "member"


async def bot_message_asker(group_id: int, qq_message_id: int) -> int | None:
    """User whose request produced this bot message, if it was a recent answer."""
    async with get_session() as s:
        return await s.scalar(
            select(Invocation.user_id)
            .where(
                Invocation.group_id == group_id,
                Invocation.reply_qq_message_id == qq_message_id,
                Invocation.created_at > datetime.now(UTC) - _RECENT,
            )
            .order_by(Invocation.created_at.desc())
            .limit(1)
        )


async def may_recall(role: Role, user_id: int, group_id: int, qq_message_id: int) -> bool:
    """Group owner/admins and superusers may recall any bot message; others only answers they asked for."""
    if role != "member":
        return True
    return await bot_message_asker(group_id, qq_message_id) == user_id

"""Held replies: stored as ReviewItem, sent to superusers by private message, decided by them.

Superusers answer in private chat (``通过 R12`` / ``拒绝 R12`` / ``改 R12 新内容``; trigger words are
configurable) or from the dashboard. State lives in the database, so restarts don't lose reviews.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from nonebot.adapters.onebot.v11 import Bot
from sqlalchemy import func, select, update

from yggdrasil.agent.schema import BotReply
from yggdrasil.bot.state import any_bot, identity
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, ReviewItem
from yggdrasil.db.session import get_session
from yggdrasil.delivery.send import send_forward, send_text
from yggdrasil.moderation.guard import Verdict
from yggdrasil.napcat.api import NapCatAPI, node_seg, text_seg
from yggdrasil.runtime import snapshot

pending_count = 0


class ReviewError(Exception):
    pass


def parse_review_id(raw: str) -> int:
    raw = raw.strip().lstrip("#").upper().removeprefix("R")
    if not raw.isdigit():
        raise ReviewError(f"无效的审核编号：{raw}")
    return int(raw)


async def notify_admins(bot: Bot, text: str) -> None:
    api = NapCatAPI(bot)
    for uid in snapshot().superusers:
        try:
            await api.send_private_msg(user_id=uid, message=[text_seg(text)])
        except Exception as e:
            LOGGER.warning("notify admin {} failed: {}", uid, e)


async def create_review(
    bot: Bot,
    *,
    invocation_id: UUID | None,
    group_id: int,
    user_id: int,
    trigger_qq_message_id: int | None,
    request_text: str,
    reply: BotReply,
    verdict: Verdict,
    expire_minutes: int,
) -> int:
    async with get_session() as session:
        item = ReviewItem(
            invocation_id=invocation_id,
            group_id=group_id,
            user_id=user_id,
            trigger_qq_message_id=trigger_qq_message_id,
            request_text=request_text[:4000],
            reply=reply.model_dump(),
            verdict=verdict.as_dict(),
            expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
        )
        session.add(item)
        await session.flush()
        review_id = item.id

    ident = await identity(bot)
    api = NapCatAPI(bot)
    group = snapshot().groups.get(group_id)
    header = (
        f"待审核 R{review_id}\n群：{group.name if group else ''}({group_id})  发起人：{user_id}\n"
        f"判定：{verdict.verdict} {verdict.score:.2f} {'、'.join(verdict.categories) or ''}\n"
        f"{expire_minutes} 分钟内回复：通过 R{review_id} / 拒绝 R{review_id} / 改 R{review_id} 新内容"
    )
    nodes = [
        node_seg(ident.self_id, ident.nickname, [text_seg(header)]),
        node_seg(ident.self_id, ident.nickname, [text_seg("【请求】\n" + request_text[:1500])]),
        node_seg(ident.self_id, ident.nickname, [text_seg("【候选回复】\n" + reply.full_text()[:3000])]),
    ]
    for uid in snapshot().superusers:
        try:
            await api.send_private_forward_msg(
                user_id=uid,
                messages=nodes,
                source=f"审核 R{review_id}",
                prompt=f"[审核 R{review_id}]",
                summary="待审核回复",
            )
            await api.send_private_msg(user_id=uid, message=[text_seg(header)])
        except Exception as e:
            LOGGER.warning("review notify to {} failed: {}", uid, e)
    await refresh_pending()
    return review_id


async def decide(review_id: int, action: str, actor: str, edited_text: str | None = None) -> ReviewItem:
    """approve | reject | edit. Raises ReviewError with a user-facing message."""
    if action not in ("approve", "reject", "edit"):
        raise ReviewError(f"未知操作：{action}")
    if action == "edit" and not (edited_text or "").strip():
        raise ReviewError("修改需要附带新内容")
    async with get_session() as session:
        item = await session.get(ReviewItem, review_id, with_for_update=True)
        if item is None:
            raise ReviewError(f"R{review_id} 不存在")
        if item.status != "pending":
            raise ReviewError(f"R{review_id} 已经是 {item.status}")
        if item.expires_at < datetime.now(UTC):
            item.status = "expired"
            raise ReviewError(f"R{review_id} 已过期")
        bot = any_bot()
        if action != "reject":
            if bot is None:
                raise ReviewError("bot 未连接，无法发送")
            api = NapCatAPI(bot)
            ident = await identity(bot)
            settings = snapshot().settings(item.group_id)
            if action == "edit":
                await send_text(
                    api,
                    group_id=item.group_id,
                    user_id=item.user_id,
                    reply_to=item.trigger_qq_message_id,
                    text=edited_text or "",
                    self_id=ident.self_id,
                    nickname=ident.nickname,
                )
            else:
                reply = BotReply.model_validate(item.reply)
                await send_forward(
                    api,
                    group_id=item.group_id,
                    user_id=item.user_id,
                    reply=reply,
                    meta_text=f"⚙ 已由管理员审核通过（R{review_id}）",
                    cfg=settings.interaction,
                    self_id=ident.self_id,
                    nickname=ident.nickname,
                )
        item.status = {"approve": "approved", "reject": "rejected", "edit": "approved"}[action]
        item.decided_by = actor
        item.decided_at = datetime.now(UTC)
        if item.invocation_id is not None:
            await session.execute(
                update(Invocation)
                .where(Invocation.id == item.invocation_id)
                .values(status="ok" if action != "reject" else "rejected")
            )
    await refresh_pending()
    return item


async def refresh_pending() -> int:
    global pending_count
    async with get_session() as session:
        pending_count = int(
            await session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending"))
            or 0
        )
    return pending_count


async def expire_loop() -> None:
    while True:
        try:
            async with get_session() as session:
                await session.execute(
                    update(ReviewItem)
                    .where(ReviewItem.status == "pending", ReviewItem.expires_at < func.now())
                    .values(status="expired")
                )
            await refresh_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("review expiry pass failed")
        await asyncio.sleep(60)

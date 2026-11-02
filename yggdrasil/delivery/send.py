"""Sending messages: text and images (each recorded as the bot's own message), reactions, and the notice when QQ
drops a message. Merged forwards are sent by ``delivery.forward``."""

import base64
from datetime import UTC, datetime
from typing import Any

from nonebot.adapters.onebot.v11 import ActionFailed, NetworkError

from yggdrasil.core.log import LOGGER
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.napcat.api import NapCatAPI, image_seg, reply_seg, text_seg


def record_self(group_id: int, self_id: int, nickname: str, message_id: int, segments: list[dict[str, Any]]) -> None:
    """Log a message the bot sent as its own, so the history (and a later reply to it) can read it."""
    WRITER.submit(
        IncomingMessage(
            group_id=group_id,
            user_id=self_id,
            qq_message_id=message_id,
            message_seq=None,
            sent_at=datetime.now(UTC),
            sender_name=nickname,
            segments=segments,
            is_self=True,
        )
    )


async def react(api: NapCatAPI, message_id: int, emoji_id: str) -> None:
    try:
        await api.set_msg_emoji_like(message_id=message_id, emoji_id=emoji_id)
    except Exception as e:
        LOGGER.warning("reaction {} on {} failed: {}", emoji_id, message_id, e)


class MessageDropped(Exception):
    """QQ took the send but never posted it (no group sequence in its answer); a short notice went out instead."""


# SnowLuma's wording when QQ answers a group send with result 0 but no group sequence. On 2026-11-01 both a forward and
# its plain-text copy describing a proxy client were dropped this way within a second: QQ's content filter, as far
# as we can tell, so sending the same content again in another form doesn't help.
_DROPPED = "no message receipt"
DROPPED_NOTICE = "这条回复没能发出来：QQ 收下了但没有发到群里，可能是内容触发了 QQ 的拦截。"


def is_dropped(e: Exception) -> bool:
    """Whether QQ took the group send but dropped the message (``MessageDropped``)."""
    return isinstance(e, ActionFailed) and _DROPPED in str(e.info.get("message") or e.info.get("wording") or "")


async def notify_dropped(api: NapCatAPI, group_id: int, self_id: int, nickname: str) -> None:
    """Say in the group that the answer didn't get through (the notice carries none of the dropped content)."""
    segments = [text_seg(DROPPED_NOTICE)]
    try:
        res = await api.send_group_msg(group_id=group_id, message=segments)
    except (ActionFailed, NetworkError) as e:
        LOGGER.warning("the dropped-reply notice to {} didn't go out either: {}", group_id, e)
        return
    record_self(group_id, self_id, nickname, res.message_id, segments)


async def send_text(
    api: NapCatAPI, *, group_id: int | None, user_id: int, reply_to: int | None, text: str, self_id: int, nickname: str
) -> int:
    """Raises :class:`MessageDropped` (after posting a notice) when QQ swallows the group message."""
    segments = ([reply_seg(reply_to)] if reply_to else []) + [text_seg(text)]
    if group_id is None:
        res = await api.send_private_msg(user_id=user_id, message=segments)
    else:
        try:
            res = await api.send_group_msg(group_id=group_id, message=segments)
        except ActionFailed as e:
            if not is_dropped(e):
                raise
            LOGGER.warning("text to {} was dropped by QQ: {}", group_id, e)
            await notify_dropped(api, group_id, self_id, nickname)
            raise MessageDropped(str(e)) from e
        record_self(group_id, self_id, nickname, res.message_id, segments)
    return res.message_id


# Uploading a picture can take QQ well over the adapter's 30 s api_timeout.
SEND_IMAGE_TIMEOUT_S = 120.0


async def send_image(
    api: NapCatAPI,
    *,
    group_id: int | None,
    user_id: int,
    reply_to: int | None,
    images: list[bytes],
    text: str,
    self_id: int,
    nickname: str,
) -> int | None:
    """Images (with an optional line of text) as one plain message, replying to the trigger.

    None: QQ didn't confirm within ``SEND_IMAGE_TIMEOUT_S``. The message almost always went out anyway (it did when
    the first drawing hit the default timeout), so callers must not send it again.
    """
    segments = [reply_seg(reply_to)] if reply_to else []
    if text:
        segments.append(text_seg(text))
    segments += [image_seg("base64://" + base64.b64encode(png).decode()) for png in images]
    try:
        if group_id is None:
            res = await api.send_private_msg(user_id=user_id, message=segments, reply_timeout=SEND_IMAGE_TIMEOUT_S)
            return res.message_id
        res = await api.send_group_msg(group_id=group_id, message=segments, reply_timeout=SEND_IMAGE_TIMEOUT_S)
    except NetworkError as e:
        if not (e.msg or "").endswith("timeout"):
            raise
        LOGGER.warning("sending images to {} was not confirmed in {}s", group_id or user_id, SEND_IMAGE_TIMEOUT_S)
        return None
    # Record the message as QQ stored it (image segments with file and url, not our base64), so a later reply to it
    # can load the picture again (e.g. "make it bluer").
    try:
        stored = [s.model_dump() for s in (await api.get_msg(message_id=res.message_id)).segments]
    except Exception as e:
        LOGGER.warning("get_msg after sending images failed: {}", e)
        stored = [s if s["type"] != "image" else text_seg("[图片]") for s in segments]
    record_self(group_id, self_id, nickname, res.message_id, stored)
    return res.message_id

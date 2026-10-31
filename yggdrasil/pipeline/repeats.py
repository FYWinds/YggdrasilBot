"""Repeated tldr requests: the same target asked for again within ``ratelimit.repeat_window_s`` is answered once.

People ask twice: two members reply "tldr" to the same message a second apart, or someone pastes the same link
again a minute later. A tldr depends only on its target and focus words, so the answer already in the chat serves
the repeat too. Chat requests are never repeats: an answer is a conversation with its asker.
"""

import hashlib

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from yggdrasil.ingest.normalize import normalize
from yggdrasil.kv import redis_client
from yggdrasil.triggers.index import Resolved


def key(event: MessageEvent, resolved: Resolved, handler: str, request_text: str) -> str | None:
    """A tldr request's identity: its group, the quoted message and the words after the keyword (a link, or what
    to focus on). None for a request that is never a repeat: a bare "tldr", whose target (the recent chat) moves
    on, or one with attachments, whose placeholders don't tell two images apart.
    """
    if handler != "tldr" or not isinstance(event, GroupMessageEvent):
        return None
    if normalize([{"type": seg.type, "data": dict(seg.data)} for seg in event.original_message]).attachments:
        return None
    words = " ".join(request_text.split())  # no casefold: short links and video ids differ by case alone
    reply = event.reply if resolved.target in ("quoted_or_self", "quoted") else None  # as context.target_message
    target = str(reply.message_id) if reply is not None else ""
    if not target and not words:
        return None
    return f"ygg:repeat:{event.group_id}:{target}:{hashlib.blake2b(words.encode(), digest_size=16).hexdigest()}"


async def first(key: str, seconds: int) -> bool:
    """True when nobody made this request in the last ``seconds`` (and records this one)."""
    if seconds <= 0:
        return True
    return bool(await redis_client().set(key, "1", ex=seconds, nx=True))


async def forget(key: str) -> None:
    """The request got no answer (rate limited, failed): asking again must work."""
    await redis_client().delete(key)

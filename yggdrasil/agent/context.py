"""Assemble the user prompt for chat / tldr runs from the event and memory."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import ahocorasick
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from sqlalchemy import select

from yggdrasil.agent.deps import Handles
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import normalize
from yggdrasil.ingest.writer import WRITER
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved


@dataclass(slots=True)
class TargetMessage:
    qq_message_id: int
    user_id: int
    sender_name: str
    text: str
    segments: list[dict[str, Any]]
    is_self: bool
    sent_at: datetime | None = None


@dataclass
class BuiltContext:
    prompt: str
    target: TargetMessage
    trigger: TargetMessage
    handles: Handles
    request_text: str
    mentioned: list[int] = field(default_factory=list)


def _segments(message: Any) -> list[dict[str, Any]]:
    return [{"type": seg.type, "data": dict(seg.data)} for seg in message]


def _sender_name(sender: Any) -> str:
    return (getattr(sender, "card", None) or getattr(sender, "nickname", None) or "").strip()


def _ts(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M") if dt else ""


def trigger_message(event: MessageEvent, self_id: int) -> TargetMessage:
    return TargetMessage(
        qq_message_id=event.message_id,
        user_id=event.user_id,
        sender_name=_sender_name(event.sender) or str(event.user_id),
        text=normalize(_segments(event.original_message)).text,
        segments=_segments(event.original_message),
        is_self=event.user_id == self_id,
        sent_at=datetime.fromtimestamp(event.time).astimezone(),
    )


async def _own_segments(group_id: int, qq_message_id: int) -> list[dict[str, Any]] | None:
    """Segments we stored when sending (incl. forward node texts that QQ won't give back)."""
    async with get_session() as session:
        return await session.scalar(
            select(Message.segments)
            .where(
                Message.group_id == group_id,
                Message.qq_message_id == qq_message_id,
                Message.is_self,
                Message.sent_at > datetime.now(UTC) - timedelta(days=7),
            )
            .order_by(Message.sent_at.desc())
            .limit(1)
        )


async def target_message(event: MessageEvent, resolved: Resolved, self_id: int) -> TargetMessage:
    if resolved.target in ("quoted_or_self", "quoted") and event.reply is not None:
        r = event.reply
        segs = _segments(r.message)
        is_self = r.sender.user_id == self_id
        if is_self and isinstance(event, GroupMessageEvent):
            await WRITER.flush()
            segs = await _own_segments(event.group_id, r.message_id) or segs
        return TargetMessage(
            qq_message_id=r.message_id,
            user_id=r.sender.user_id or 0,
            sender_name=_sender_name(r.sender) or str(r.sender.user_id),
            text=normalize(segs).text,
            segments=segs,
            is_self=is_self,
            sent_at=datetime.fromtimestamp(r.time).astimezone(),
        )
    return trigger_message(event, self_id)


# --- glossary matching -------------------------------------------------------

_automata: dict[int, tuple[float, Any]] = {}
_GLOSSARY_TTL = 300


async def _glossary_hits(group_id: int, texts: list[str]) -> list[Any]:
    from yggdrasil.memory.search import glossary_terms

    cached = _automata.get(group_id)
    if cached is None or time.monotonic() - cached[0] > _GLOSSARY_TTL:
        terms = await glossary_terms(group_id)
        auto = ahocorasick.Automaton()
        for t in terms:
            for word in [t.term, *t.aliases]:
                if word and len(word) >= 2:
                    auto.add_word(word.lower(), t)
        if len(auto):
            auto.make_automaton()
        cached = (time.monotonic(), auto)
        _automata[group_id] = cached
    auto = cached[1]
    if not len(auto):
        return []
    found: dict[str, Any] = {}
    for text in texts:
        for _, term in auto.iter(text.lower()):
            found.setdefault(term.term, term)
    return list(found.values())[:12]


# --- assembly ----------------------------------------------------------------


def _attach(handles: Handles, msg: TargetMessage) -> str:
    """Register a message's attachments; returns ' (img#1, url#1)' style suffix."""
    norm = normalize(msg.segments)
    keys = []
    for att in norm.attachments:
        if key := handles.add(att.kind, att.data, msg.qq_message_id):
            keys.append(key)
    for url in norm.urls:
        if key := handles.add("url", {"url": url}, msg.qq_message_id):
            keys.append(key)
    return f" ({', '.join(dict.fromkeys(keys))})" if keys else ""


async def build_context(
    event: MessageEvent,
    resolved: Resolved,
    settings: Settings,
    *,
    self_id: int,
    bot_name: str,
    request_text: str,
) -> BuiltContext:
    from yggdrasil.memory import search

    handles = Handles()
    trigger = trigger_message(event, self_id)
    target = await target_message(event, resolved, self_id)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    cfg = settings.context
    blocks: list[str] = []

    def name_of(user_id: int, name: str, is_self: bool) -> str:
        return f"{bot_name}(你)" if is_self else f"{name}({user_id})"

    mentioned = normalize(trigger.segments).mentions
    if group_id is not None:
        await WRITER.flush()
        try:
            recent = await search.recent_messages(group_id, limit=cfg.recent_messages, minutes=cfg.recent_minutes)
        except Exception:
            LOGGER.exception("recent_messages failed")
            recent = []
        if recent:
            lines = [
                f"[{_ts(m.sent_at)}] #{m.id} {name_of(m.user_id, m.sender_name, m.is_self)}: {m.text}" for m in recent
            ]
            text = "\n".join(lines)
            if len(text) > cfg.max_context_chars:
                text = text[-cfg.max_context_chars :]
                text = text[text.find("\n") + 1 :]
            blocks.append(f"<最近群聊>\n{text}\n</最近群聊>")

    if target.qq_message_id != trigger.qq_message_id:
        suffix = _attach(handles, target)
        blocks.append(
            f'<被引用的消息 发送者="{name_of(target.user_id, target.sender_name, target.is_self)}" '
            f'时间="{_ts(target.sent_at)}">\n{target.text}{suffix}\n</被引用的消息>'
        )
    trigger_suffix = _attach(handles, trigger)

    if group_id is not None:
        people = {trigger.user_id, target.user_id, *mentioned} - {self_id}
        if cfg.include_profiles and people:
            try:
                profiles = list((await search.profiles(group_id, sorted(people))).values())
            except Exception:
                LOGGER.exception("profiles lookup failed")
                profiles = []
            if profiles:
                lines = [f"- {p.display_name}({p.user_id})：{p.summary}" for p in profiles if p.summary]
                if lines:
                    blocks.append("<相关群友画像>\n" + "\n".join(lines) + "\n</相关群友画像>")
        if cfg.include_glossary:
            try:
                hits = await _glossary_hits(group_id, [trigger.text, target.text, request_text])
            except Exception:
                LOGGER.exception("glossary lookup failed")
                hits = []
            if hits:
                blocks.append("<群内黑话>\n" + "\n".join(f"- {t.term}：{t.definition}" for t in hits) + "\n</群内黑话>")
        if cfg.include_open_topics:
            try:
                topics = await search.open_topics(group_id, limit=5)
            except Exception:
                LOGGER.exception("open_topics failed")
                topics = []
            if topics:
                blocks.append(
                    "<进行中的话题>\n" + "\n".join(f"- {t.title}：{t.summary}" for t in topics) + "\n</进行中的话题>"
                )

    available = ", ".join(f"{k}({v.kind})" for k, v in handles.items.items())
    blocks.append(
        f'<请求 发送者="{name_of(trigger.user_id, trigger.sender_name, False)}">\n'
        f"{request_text or trigger.text}{trigger_suffix}\n</请求>"
    )
    if available:
        blocks.append(f"可用句柄：{available}")
    return BuiltContext("\n\n".join(blocks), target, trigger, handles, request_text, mentioned)

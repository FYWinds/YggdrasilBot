"""Assemble the user prompt for chat / tldr runs from the event and memory."""

import secrets
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
from yggdrasil.people import People, resolve
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
    # Random suffix of every data block's tag (<最近群聊-ab12cd>); the system prompt names it so
    # a pasted "</最近群聊>" can't fake the end of a block.
    nonce: str = ""


class _Blocks:
    def __init__(self) -> None:
        self.nonce = secrets.token_hex(3)
        self.parts: list[str] = []

    def add(self, tag: str, body: str, attrs: str = "") -> None:
        name = f"{tag}-{self.nonce}"
        body = body.replace(self.nonce, "*" * len(self.nonce))
        self.parts.append(f"<{name}{attrs}>\n{body}\n</{name}>")

    def note(self, text: str) -> None:
        self.parts.append(text)


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
        sender_name=_sender_name(event.sender),
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
            sender_name=_sender_name(r.sender),
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
    workspace: bool = False,
) -> BuiltContext:
    """``workspace``: this run has the sandbox suite, so describe the group's live workspace (if any)."""
    from yggdrasil.memory import search

    handles = Handles()
    trigger = trigger_message(event, self_id)
    target = await target_message(event, resolved, self_id)
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
    cfg = settings.context
    blocks = _Blocks()
    # tldr summarizes the target itself: no group chat, profiles or topics up front (they slant it toward
    # the asker or the group's theme); it can pull recent chat with the recent_chat tool when needed.
    background = resolved.handler != "tldr"

    mentioned = normalize(trigger.segments).mentions
    if group_id is not None:
        await WRITER.flush()  # also lets recent_chat / search tools see the latest messages
    recent: list[search.MessageView] = []
    if group_id is not None and background:
        try:
            recent = await search.recent_messages(group_id, limit=cfg.recent_messages, minutes=cfg.recent_minutes)
        except Exception:
            LOGGER.exception("recent_messages failed")
    # One alias lookup for everyone this prompt names: senders, @mentions, quoted authors.
    try:
        people = await resolve(
            group_id,
            {trigger.user_id, target.user_id, *mentioned, *normalize(target.segments).mentions},
            bot_name=bot_name,
            self_id=self_id,
            texts=[request_text, *(m.text for m in recent)],
        )
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    # Model-facing texts: mentions as 名字(QQ), the bot as {bot_name}(你).
    trigger.text = normalize(trigger.segments, people.mention).text
    target.text = trigger.text if target is trigger else normalize(target.segments, people.mention).text
    request_text = people.mentions(request_text)

    if recent:
        lines = [
            f"[{_ts(m.sent_at)}] #{m.id} {people.who(m.user_id, m.sender_name, is_self=m.is_self)}: "
            f"{people.mentions(m.text)}"
            for m in recent
        ]
        text = "\n".join(lines)
        if len(text) > cfg.max_context_chars:
            text = text[-cfg.max_context_chars :]
            text = text[text.find("\n") + 1 :]
        blocks.add("最近群聊", text)

    if target.qq_message_id != trigger.qq_message_id:
        suffix = _attach(handles, target)
        blocks.add(
            "被引用的消息",
            f"{target.text}{suffix}",
            f' 发送者="{people.who(target.user_id, target.sender_name, is_self=target.is_self)}" '
            f'时间="{_ts(target.sent_at)}"',
        )
    trigger_suffix = _attach(handles, trigger)

    if group_id is not None:
        others = {trigger.user_id, target.user_id, *mentioned} - {self_id}
        if cfg.include_profiles and background and others:
            try:
                profiles = list((await search.profiles(group_id, sorted(others))).values())
            except Exception:
                LOGGER.exception("profiles lookup failed")
                profiles = []
            if profiles:
                lines = [f"- {people.who(p.user_id, p.display_name)}：{p.summary}" for p in profiles if p.summary]
                if lines:
                    blocks.add("相关群友画像", "\n".join(lines))
        if cfg.include_glossary:
            try:
                hits = await _glossary_hits(group_id, [trigger.text, target.text, request_text])
            except Exception:
                LOGGER.exception("glossary lookup failed")
                hits = []
            if hits:
                blocks.add("群内黑话", "\n".join(f"- {t.term}：{t.definition}" for t in hits))
        if cfg.include_open_topics and background:
            try:
                topics = await search.open_topics(group_id, limit=5)
            except Exception:
                LOGGER.exception("open_topics failed")
                topics = []
            if topics:
                blocks.add("进行中的话题", "\n".join(f"- {t.title}：{t.summary}" for t in topics))
        if workspace:
            from yggdrasil.sandbox import manager

            try:
                described = await manager().describe(group_id)
            except Exception:
                LOGGER.exception("workspace describe failed")
                described = None
            if described:
                blocks.add("工作区", described)

    available = ", ".join(f"{k}({v.kind})" for k, v in handles.items.items())
    sender = f' 发送者="{people.who(trigger.user_id, trigger.sender_name)}"'
    focused = resolved.handler == "tldr" and target.qq_message_id != trigger.qq_message_id and bool(request_text)
    if focused:
        # "tldr <words>" on a quoted message: the words say what to focus on.
        blocks.add("关注点", f"{request_text}{trigger_suffix}", sender)
    else:
        blocks.add("请求", f"{request_text or trigger.text}{trigger_suffix}", sender)
    if available:
        blocks.note(f"可用句柄：{available}")
    if resolved.handler == "tldr":
        ia = settings.interaction
        blocks.note(
            f"篇幅：只回答关注点，正文不超过 {ia.tldr_focus_max_chars} 字。"
            if focused
            else f"篇幅：正文不超过 {ia.tldr_max_chars} 字，挑最重要的说，不要罗列所有细节。"
        )
    return BuiltContext(
        "\n\n".join(blocks.parts), target, trigger, handles, request_text, mentioned, nonce=blocks.nonce
    )

"""Assemble the user prompt for chat / tldr runs from the event and memory."""

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

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
from yggdrasil.transcript.schema import QQContextDisplay, QQMessage
from yggdrasil.triggers.index import Resolved

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView


@dataclass(slots=True)
class TargetMessage:
    qq_message_id: int
    user_id: int
    sender_name: str
    text: str
    segments: list[dict[str, Any]]
    is_self: bool
    sent_at: datetime | None = None

    def snapshot(self) -> QQMessage:
        return QQMessage(
            qq_message_id=self.qq_message_id,
            user_id=self.user_id,
            sender_name=self.sender_name,
            sent_at=self.sent_at,
            segments=self.segments,
            is_self=self.is_self,
        )


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
    display: QQContextDisplay | None = None  # what the prompt contained, for the console's transcript
    # Handles of the messages the request is about (trigger, quoted message and what it replies to), in prompt
    # order; their images are attached up front. Background chat registers handles too, but not here.
    focus: list[str] = field(default_factory=list)


class _Blocks:
    def __init__(self) -> None:
        self.nonce = secrets.token_hex(3)
        self.parts: list[str] = []
        self.tags: list[str] = []

    def add(self, tag: str, body: str, attrs: str = "") -> None:
        self.tags.append(tag)
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

# The quoted message plus up to two messages up its own reply chain: "tldr" on a remark that replies to a forward
# must see the forward.
_QUOTE_LEVELS = 3


def chat_line(m: "MessageView", people: People, handles: Handles) -> str:
    """A history message as the model sees it everywhere: ``[time] #id who: text (fwd#1, img#2)``.

    ``#id`` is the database id every history tool and ``#<id>`` ref takes; the handles make what the line shows
    usable as is.
    """
    who = people.who(m.user_id, m.sender_name, is_self=m.is_self)
    return f"[{_ts(m.sent_at)}] #{m.id} {who}: {people.mentions(m.text)}{handles.register(m.segments or [])}"


async def _quoted_row(group_id: int | None, target: "TargetMessage") -> "MessageView | None":
    from yggdrasil.memory import search

    if group_id is None:
        return None
    try:
        return await search.find_by_qq_id(group_id, target.qq_message_id)
    except Exception:
        LOGGER.exception("quoted message lookup failed")
        return None


async def _reply_chain(group_id: int, reply_to: int | None) -> list["MessageView"]:
    """The messages a quoted message replies to, up its chain (oldest first), as far as the history has them."""
    from yggdrasil.memory import search

    chain: list[MessageView] = []
    while reply_to is not None and len(chain) < _QUOTE_LEVELS - 1:
        try:
            m = await search.find_by_qq_id(group_id, reply_to)
        except Exception:
            LOGGER.exception("reply chain lookup failed")
            break
        if m is None or any(c.id == m.id for c in chain):
            break
        chain.append(m)
        reply_to = m.reply_to_qq_id
    return chain[::-1]


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
    """``workspace``: this run has the sandbox suite, so list the group's workspaces (the model picks one)."""
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

    quoted = target.qq_message_id != trigger.qq_message_id
    mentioned = normalize(trigger.segments).mentions
    if group_id is not None:
        await WRITER.flush()  # also lets recent_chat / search tools see the latest messages
    recent: list[search.MessageView] = []
    if group_id is not None and background:
        try:
            recent = await search.recent_messages(group_id, limit=cfg.recent_messages, minutes=cfg.recent_minutes)
        except Exception:
            LOGGER.exception("recent_messages failed")
    quoted_row = await _quoted_row(group_id, target) if quoted else None
    chain: list[search.MessageView] = []
    if quoted and group_id is not None:
        reply_to = quoted_row.reply_to_qq_id if quoted_row else normalize(target.segments).reply_to
        chain = await _reply_chain(group_id, reply_to)
    # One alias lookup for everyone this prompt names: senders, @mentions, quoted authors.
    try:
        people = await resolve(
            group_id,
            {
                trigger.user_id,
                target.user_id,
                *mentioned,
                *normalize(target.segments).mentions,
                *(m.user_id for m in chain),
                *(u for m in chain for u in normalize(m.segments or []).mentions),
            },
            bot_name=bot_name,
            self_id=self_id,
            texts=[request_text, *(m.text for m in recent), *(m.text for m in chain)],
        )
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    # Model-facing texts: mentions as 名字(QQ), the bot as {bot_name}(你).
    trigger.text = normalize(trigger.segments, people.mention).text
    target.text = trigger.text if target is trigger else normalize(target.segments, people.mention).text
    request_text = people.mentions(request_text)

    # The messages the request is about register first: their handles are focus (attached up front).
    chain_lines = [chat_line(m, people, handles) for m in chain]
    target_suffix = handles.register(target.segments, urls=True) if quoted else ""
    trigger_suffix = handles.register(trigger.segments, urls=True)
    focus = list(handles.items)

    recent_ids: list[int] = []
    if recent:
        lines = [chat_line(m, people, handles) for m in recent]
        text = "\n".join(lines)
        first = 0
        if len(text) > cfg.max_context_chars:
            text = text[-cfg.max_context_chars :]
            text = text[text.find("\n") + 1 :]
            # The messages whose lines survived the cut (a message's own text may contain newlines).
            kept, first = 0, len(lines)
            while first > 0 and kept + len(lines[first - 1]) <= len(text):
                kept += len(lines[first - 1]) + 1
                first -= 1
        recent_ids = [m.id for m in recent[first:]]
        blocks.add("最近群聊", text)

    if chain_lines:
        blocks.add("被引用消息回复的消息", "\n".join(chain_lines))
    if quoted:
        number = f' 编号="#{quoted_row.id}"' if quoted_row else ""
        blocks.add(
            "被引用的消息",
            f"{target.text}{target_suffix}",
            f' 发送者="{people.who(target.user_id, target.sender_name, is_self=target.is_self)}" '
            f'时间="{_ts(target.sent_at)}"{number}',
        )

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
                listing = await manager().listing(group_id)
            except Exception:
                LOGGER.exception("workspace listing failed")
                listing = None
            if listing is not None:
                blocks.add("工作区", listing or "本群还没有工作区。")

    sender = f' 发送者="{people.who(trigger.user_id, trigger.sender_name)}"'
    focused = resolved.handler == "tldr" and quoted and bool(request_text)
    if focused:
        # "tldr <words>" on a quoted message: the words say what to focus on.
        blocks.add("关注点", f"{request_text}{trigger_suffix}", sender)
    else:
        blocks.add("请求", f"{request_text or trigger.text}{trigger_suffix}", sender)
    if resolved.handler == "tldr":
        ia = settings.interaction
        blocks.note(
            f"篇幅：只回答关注点，正文不超过 {ia.tldr_focus_max_chars} 字。"
            if focused
            else f"篇幅：正文不超过 {ia.tldr_max_chars} 字，挑最重要的说，不要罗列所有细节。"
        )
    display = QQContextDisplay(
        recent=recent_ids,
        quoted=target.snapshot() if quoted else None,
        trigger=trigger.snapshot(),
        blocks=blocks.tags,
        chain=[
            QQMessage(
                id=m.id,
                qq_message_id=m.qq_message_id,
                user_id=m.user_id,
                sender_name=m.sender_name,
                sent_at=m.sent_at,
                segments=m.segments or [],
                is_self=m.is_self,
            )
            for m in chain
        ],
    )
    return BuiltContext(
        "\n\n".join(blocks.parts),
        target,
        trigger,
        handles,
        request_text,
        mentioned,
        nonce=blocks.nonce,
        display=display,
        focus=focus,
    )

"""Group history and memory tools: ``search_history``, ``get_messages_around``, ``recent_chat``,
``lookup_glossary``, ``get_profile``."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

from yggdrasil.agent.context import chat_line, chat_tail, reply_targets
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool, people_for
from yggdrasil.tools.errors import ToolError

if TYPE_CHECKING:
    from yggdrasil.memory.search import MessageView

GLOSSARY_LIST_MAX = 200  # term names an empty lookup_glossary lists


async def _chat_lines(ctx: RunContext[Deps], msgs: "Sequence[MessageView]") -> list[str]:
    """``chat_line`` of each message, with one name lookup and one reply-target lookup for all of them."""
    people = await people_for(ctx, [m.user_id for m in msgs], [m.text for m in msgs])
    replies = await reply_targets(ctx.deps.group_id, msgs)
    return [chat_line(m, people, ctx.deps.handles, replies) for m in msgs]


@chat_tool("search_history")
async def search_history(ctx: RunContext[Deps], query: str, days: int | None = None, user_id: int | None = None) -> str:
    """检索本群历史聊天记录和话题摘要（关键词+语义）。days 限定最近几天，user_id 限定发言人。"""
    from yggdrasil.memory.search import find_by_qq_id, message_segments
    from yggdrasil.memory.search import search_history as _search

    if ctx.deps.group_id is None:
        raise ToolError("私聊里不能检索群历史")
    since = None
    if days:
        since = datetime.now().astimezone() - timedelta(days=days)
    # Only what came before the request: the asking message itself is no search result.
    asked = await find_by_qq_id(ctx.deps.group_id, ctx.deps.trigger_message_id) if ctx.deps.trigger_message_id else None
    hits = await _search(
        ctx.deps.group_id,
        query,
        since=since,
        until=asked.sent_at if asked else None,
        user_id=user_id,
        top_k=ctx.deps.settings.tools.history_top_k,
    )
    if not hits:
        return "没有找到相关记录"
    ctx.deps.notes.messages(ctx.tool_call_id, [h.msg_id for h in hits if h.msg_id])
    people = await people_for(ctx, [h.user_id for h in hits if h.user_id], [h.text for h in hits])
    segments = await message_segments(ctx.deps.group_id, [h.msg_id for h in hits if h.msg_id])
    out = []
    for h in hits:
        when = h.when.astimezone().strftime("%Y-%m-%d %H:%M") if h.when else ""
        ref = f"#{h.msg_id}" if h.msg_id else f"{h.kind}:{h.ref_id}"
        who = f"{people.who(h.user_id, h.sender_name)}: " if h.user_id else ""
        keys = ctx.deps.handles.register(segments.get(h.msg_id) or []) if h.msg_id else ""
        body = f"{who}{people.mentions(h.text)}{keys}"
        out.append(f"[{when}] ({ref})" + (f"\n{body}" if h.kind == "chunk" else f" {body}"))
    return "\n".join(out)


@chat_tool("get_messages_around")
async def get_messages_around(ctx: RunContext[Deps], message_id: int, before: int = 10, after: int = 10) -> str:
    """取某条历史消息（#id）前后的上下文。"""
    from yggdrasil.memory.search import messages_around

    if ctx.deps.group_id is None:
        raise ToolError("私聊里不能检索群历史")
    msgs = await messages_around(ctx.deps.group_id, message_id, min(before, 40), min(after, 40))
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in msgs])
    return "\n".join(await _chat_lines(ctx, msgs)) or "无"


@chat_tool("recent_chat")
async def recent_chat(ctx: RunContext[Deps], minutes: int = 30) -> str:
    """取本群最近的聊天记录（最近 minutes 分钟，至少包含最近若干条）。只在目标需要群里的上下文才能讲清时用，
    比如短词、黑话、截图、引用的一句聊天；链接、文件、合并转发直接读原文，不需要它。"""
    from yggdrasil.memory.search import recent_messages

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群聊记录")
    cfg = ctx.deps.settings.context
    msgs = await recent_messages(ctx.deps.group_id, limit=cfg.recent_messages, minutes=min(max(minutes, 1), 24 * 60))
    if not msgs:
        return "这段时间没有聊天记录"
    lines = await _chat_lines(ctx, msgs)
    first, text = chat_tail(lines, cfg.max_context_chars)
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in msgs[first:]])
    return text


@chat_tool("lookup_glossary")
async def lookup_glossary(ctx: RunContext[Deps], term: str) -> str:
    """查本群黑话/梗词典。"""
    from yggdrasil.memory.search import glossary_terms
    from yggdrasil.memory.search import lookup_glossary as _lookup

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群词典")
    if not term.strip():
        entries = await glossary_terms(ctx.deps.group_id)
        names = sorted((f"{t.term}（{'、'.join(t.aliases)}）" if t.aliases else t.term for t in entries), key=str.lower)
        if not names:
            return "本群词典还是空的"
        shown = names[:GLOSSARY_LIST_MAX]
        more = f"\n（共 {len(names)} 条，只列出前 {len(shown)} 条）" if len(names) > len(shown) else ""
        return f"本群词典的词条（查释义时传入词条名）：\n{'、'.join(shown)}{more}"
    terms = await _lookup(ctx.deps.group_id, term)
    if not terms:
        return f"词典里没有「{term}」"
    return "\n".join(f"{t.term}（别名：{'、'.join(t.aliases) or '无'}）：{t.definition}" for t in terms)


@chat_tool("get_profile")
async def get_profile(ctx: RunContext[Deps], user_id: int) -> str:
    """查群友画像（昵称、兴趣、常聊话题、最重要的几条事实）。user_id 是 QQ 号，即「名字(QQ号)」括号里的数字。"""
    from yggdrasil.memory.search import get_profile as _get

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群友画像")
    p = await _get(ctx.deps.group_id, user_id)
    if p is None:
        return "没有这个人的画像（可能选择了不被记录）"
    who = (await people_for(ctx, [user_id])).who(p.user_id, p.display_name)
    out = f"{who}：{p.summary}\n兴趣：{'、'.join(p.interests)}\n特点：{'、'.join(p.traits)}"
    if p.facts:
        out += "\n事实：\n" + "\n".join(f"- {f}" for f in p.facts)
    return out

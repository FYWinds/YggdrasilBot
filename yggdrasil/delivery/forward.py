"""Replies as merged forwards: Markdown stripping and splitting, the nodes (sections, pictures, sources, metadata),
the send, and recovering a forward QQ didn't confirm from the group's history; when it failed, the answer goes out
once as plain text."""

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from typing import Any, Literal

from nonebot.adapters.onebot.v11 import ActionFailed, NetworkError

from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.send import (
    SEND_IMAGE_TIMEOUT_S,
    MessageDropped,
    is_dropped,
    notify_dropped,
    record_self,
    send_text,
)
from yggdrasil.ingest.normalize import multimsg_resid
from yggdrasil.napcat.api import NapCatAPI, image_seg, markdown_seg, node_seg, text_seg
from yggdrasil.napcat.models import Segment
from yggdrasil.settings import InteractionSettings

_MD_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_MD_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_BULLET = re.compile(r"^(\s*)[*+]\s+", re.MULTILINE)


def strip_markdown(text: str) -> str:
    text = _MD_FENCE.sub("", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BOLD.sub(r"\2", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1 (\2)", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_BULLET.sub(r"\1- ", text)
    return text.strip()


def split_text(text: str, limit: int) -> list[str]:
    """Split on paragraph, then line boundaries, hard-cutting only if a single line is too long."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(current) + len(para) <= limit:
            current += para
            continue
        if current.strip():
            chunks.append(current.strip())
        current = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(para[:cut].strip())
            para = para[cut:]
        current = para
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def build_nodes(
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    *,
    self_id: int,
    nickname: str,
    images: list[tuple[bytes, str]] | None = None,
) -> list[dict[str, Any]]:
    bodies: list[str] = []
    sections = list(reply.sections)
    if len(sections) > cfg.max_sections:
        # Hard cap behind the prompt's "at most two sections": fold the rest into the last kept one.
        keep, extra = sections[: cfg.max_sections - 1], sections[cfg.max_sections - 1 :]
        merged = "\n\n".join(s.content for s in extra)
        sections = [*keep, extra[0].model_copy(update={"content": merged})]
    for section in sections:
        content = section.content if cfg.forward_render == "nested_markdown" else strip_markdown(section.content)
        bodies.extend(split_text(content, cfg.node_max_chars))
    if reply.sources:
        bodies.append(_sources_text(reply))
    limit = cfg.forward_max_nodes - (1 if meta_text else 0)
    if len(bodies) > limit:
        bodies = [*bodies[: limit - 1], "\n\n".join(bodies[limit - 1 :])]

    def node(text: str) -> dict[str, Any]:
        if cfg.forward_render == "nested_markdown":
            # NapCat only renders markdown inside a nested (double-layer) forward.
            inner = node_seg(self_id, nickname, [markdown_seg(text)])
            return node_seg(self_id, nickname, [inner])
        return node_seg(self_id, nickname, [text_seg(text)])

    nodes = [node(b) for b in bodies]
    for png, caption in images or []:
        segs = [image_seg("base64://" + base64.b64encode(png).decode())]
        if caption:
            segs.insert(0, text_seg(caption + "\n"))
        # Keep sources last among text nodes but before metadata: insert images after the sections.
        nodes.insert(len(nodes) - (1 if reply.sources else 0), node_seg(self_id, nickname, segs))
    if meta_text:
        nodes.append(node_seg(self_id, nickname, [text_seg(meta_text)]))
    return nodes


def _sources_text(reply: BotReply) -> str:
    return "来源\n" + "\n".join(f"- {s.label}" + (f"\n  {s.url}" if s.url else "") for s in reply.sources[:10])


_CUT = "\n……（后文过长，已省略）"


def reply_text(reply: BotReply, cfg: InteractionSettings) -> str:
    """The answer as one plain message, for when the forward can't be sent: cut to ``node_max_chars``."""
    parts = [reply.title or "", *(strip_markdown(s.content) for s in reply.sections)]
    if reply.sources:
        parts.append(_sources_text(reply))
    chunks = split_text("\n\n".join(p for p in parts if p.strip()), cfg.node_max_chars - len(_CUT))
    return chunks[0] + _CUT if len(chunks) > 1 else chunks[0]


# Forwards carry rendered pictures too (render_markdown, render_mermaid), which QQ uploads before it confirms.
SEND_FORWARD_TIMEOUT_S = SEND_IMAGE_TIMEOUT_S
# An unconfirmed forward is looked for in the group's latest history: checks after these pauses (QQ may still be
# posting it), on a page this long, allowing for this much clock difference between us and QQ.
FORWARD_CHECK_DELAYS_S = (0.0, 5.0, 10.0)
_HISTORY_PAGE = 30
_CLOCK_SKEW_S = 5
# SnowLuma reports a send packet that got no reply, or an answer it couldn't decode, as a failed action although the
# message may be in the group (it says "check the conversation before retrying"); other failures are definite. An answer
# without a receipt (``send.is_dropped``) is handled before this: QQ dropped that message.
_MAYBE_SENT = re.compile(r"message failed: |not confirmed|failed to decode SendMessageResponse")


def _maybe_sent(e: ActionFailed | NetworkError) -> bool:
    if isinstance(e, NetworkError):
        return (e.msg or "").endswith("timeout")
    return bool(_MAYBE_SENT.search(str(e.info.get("message") or e.info.get("wording") or "")))


def _forward_id(segments: list[Segment]) -> str | None:
    """Resid of the forward in a history message: a ``forward`` segment (SnowLuma) or the ``com.tencent.multimsg``
    json card NapCat returns for the bot's own forwards."""
    for s in segments:
        if s.type == "forward" and s.data.get("id"):
            return str(s.data["id"])
        if s.type == "json" and (resid := multimsg_resid(str(s.data.get("data", "")))):
            return resid
    return None


def _text_of(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for s in segments:
        data = s.get("data") or {}
        match s.get("type"):
            case "text":
                parts.append(str(data.get("text", "")))
            case "markdown":
                parts.append(str(data.get("content", "")))
            case "node" | "forward" if isinstance(data.get("content"), list):
                parts.append(_text_of(data["content"]))
            case _:
                pass
    return "".join(parts)


def _text_key(text: str) -> str:
    return "".join(text.split())[:60]


async def _is_ours(api: NapCatAPI, resid: str, first: str) -> bool | None:
    """Whether the forward ``resid`` opens with our first node (another run may have posted one meanwhile).
    None: can't tell."""
    try:
        records = (await api.get_forward_msg(id=resid)).messages
    except Exception as e:
        LOGGER.warning("get_forward_msg {} after an unconfirmed forward: {}", resid, e)
        return None
    theirs = _text_key(_text_of([s.model_dump() for s in records[0].segments])) if records else ""
    if not theirs or not first:
        return None
    return theirs == first


async def _find_own_forward(
    api: NapCatAPI, group_id: int, self_id: int, since: float, nodes: list[dict[str, Any]]
) -> tuple[int | None, str] | Literal["absent", "unknown"]:
    """(QQ id, resid) of our forward in the group posted at ``since`` or later. "absent" only when the history
    reaches back to the send and no bot forward on it could be ours; else "unknown"."""
    first = _text_key(_text_of(nodes[:1]))
    checked: dict[str, bool | None] = {}
    covered = False
    for delay in FORWARD_CHECK_DELAYS_S:
        await asyncio.sleep(delay)
        try:
            page = (await api.get_group_msg_history(group_id=group_id, count=_HISTORY_PAGE)).messages
        except Exception as e:
            LOGGER.warning("history of {} after an unconfirmed forward: {}", group_id, e)
            covered = False
            continue
        for m in page:
            if (m.user_id or m.sender.user_id) != self_id or m.time < since - _CLOCK_SKEW_S:
                continue
            resid = _forward_id(m.segments)
            if resid is None or resid in checked:
                continue
            checked[resid] = mine = await _is_ours(api, resid, first)
            if mine:
                return m.message_id, resid
        covered = bool(page) and min(m.time for m in page) <= since - _CLOCK_SKEW_S
    return "absent" if covered and None not in checked.values() else "unknown"


def _record_forward(
    group_id: int,
    self_id: int,
    nickname: str,
    message_id: int,
    res_id: str,
    title: str,
    nodes: list[dict[str, Any]],
    meta_text: str | None,
) -> None:
    # Keep the node texts with our own record: QQ hands self-sent forwards back as an opaque
    # card, so this is how a later "reply to the bot's forward" can read what was in it.
    stored = _stored_nodes(nodes, meta_text)
    record_self(
        group_id,
        self_id,
        nickname,
        message_id,
        [{"type": "forward", "data": {"id": res_id, "title": title, "content": stored}}],
    )


async def send_forward(
    api: NapCatAPI,
    *,
    group_id: int | None,
    user_id: int,
    reply: BotReply,
    meta_text: str | None,
    cfg: InteractionSettings,
    self_id: int,
    nickname: str,
    images: list[tuple[bytes, str]] | None = None,
) -> int | None:
    """The reply as a merged forward; returns the QQ id of what was sent.

    A group forward QQ didn't confirm (no reply within ``SEND_FORWARD_TIMEOUT_S``, or SnowLuma's "maybe delivered"
    failures) is looked for in the group's history; found, and opening with our first node, it counts as sent. Missing
    from history that reaches back to the send (and carrying no pictures that may still be uploading), or rejected by
    QQ outright, it failed and the answer goes out once as plain text instead. None: most likely sent (an unconfirmed
    private forward, or history that can't tell), so it must not be sent again; like ``send_image``.
    """
    nodes = build_nodes(reply, meta_text, cfg, self_id=self_id, nickname=nickname, images=images)
    title = (reply.title or "回复")[:30]  # only for our own message record; never sent as a preview
    target = group_id or user_id
    started = time.time()
    try:
        if group_id is None:
            res = await api.send_private_forward_msg(
                user_id=user_id, messages=nodes, reply_timeout=SEND_FORWARD_TIMEOUT_S
            )
            return res.message_id
        res = await api.send_group_forward_msg(group_id=group_id, messages=nodes, reply_timeout=SEND_FORWARD_TIMEOUT_S)
    except (ActionFailed, NetworkError) as e:
        if group_id is not None and is_dropped(e):
            # Not "maybe delivered": QQ answered and assigned no sequence. The text copy would carry the same content.
            LOGGER.warning("forward to {} was dropped by QQ: {}", target, e)
            await notify_dropped(api, group_id, self_id, nickname)
            raise MessageDropped(str(e)) from e
        maybe_sent = _maybe_sent(e)
        if isinstance(e, NetworkError) and not maybe_sent:
            raise
        if not maybe_sent:
            LOGGER.warning("forward to {} failed, sending the answer as text: {}", target, e)
        elif group_id is None:
            LOGGER.warning("forward to {} was not confirmed ({}); counting it as sent", target, e)
            return None
        else:
            match await _find_own_forward(api, group_id, self_id, started, nodes):
                case (message_id, resid):
                    LOGGER.info(
                        "forward to {} was not confirmed ({}) but is in the history as {}", target, e, message_id
                    )
                    if message_id is not None:
                        _record_forward(group_id, self_id, nickname, message_id, resid, title, nodes, meta_text)
                    return message_id
                case "unknown":
                    LOGGER.warning(
                        "forward to {} was not confirmed ({}) and the history can't tell; counting it as sent",
                        target,
                        e,
                    )
                    return None
                case "absent" if images:
                    # Its pictures may still be uploading (SnowLuma has no overall limit on that): a text copy now
                    # could be followed by the forward.
                    LOGGER.warning(
                        "forward to {} with pictures was not confirmed ({}) and isn't in the history yet; "
                        "counting it as sent",
                        target,
                        e,
                    )
                    return None
                case "absent":
                    LOGGER.warning(
                        "forward to {} was not confirmed ({}) and isn't in the history, sending the answer as text",
                        target,
                        e,
                    )
    else:
        _record_forward(group_id, self_id, nickname, res.message_id, res.res_id or "", title, nodes, meta_text)
        return res.message_id
    return await send_text(
        api,
        group_id=group_id,
        user_id=user_id,
        reply_to=None,
        text=reply_text(reply, cfg),
        self_id=self_id,
        nickname=nickname,
    )


def _stored_nodes(nodes: list[dict[str, Any]], meta_text: str | None) -> list[dict[str, Any]]:
    """What our message record keeps of a sent forward: its nodes stamped with the send time, images as
    placeholders, and no metadata node (it describes the run, not the answer, so a later expand_forward
    must not hand it to the model as content)."""
    sent = int(datetime.now(UTC).timestamp())
    meta = [text_seg(meta_text)] if meta_text else None
    stored = []
    for n in nodes:
        if meta is not None and (n.get("data") or {}).get("content") == meta:
            continue
        node = _textual_node(n)
        node["data"]["time"] = sent
        stored.append(node)
    return stored


def _textual_node(node: dict[str, Any]) -> dict[str, Any]:
    """Node with images replaced by placeholders (don't store base64 PNGs in the message log)."""
    data = dict(node.get("data") or {})
    content = []
    for seg in data.get("content") or []:
        if seg.get("type") == "image":
            content.append({"type": "text", "data": {"text": "[图片]"}})
        elif seg.get("type") == "node":
            content.append(_textual_node(seg))
        else:
            content.append(seg)
    data["content"] = content
    return {"type": "node", "data": data}

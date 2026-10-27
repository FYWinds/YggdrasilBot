"""Sending replies: inline replies, merged forwards with a metadata node, reactions."""

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.napcat.api import NapCatAPI, image_seg, markdown_seg, node_seg, reply_seg, text_seg
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


@dataclass
class Meta:
    models: list[str] = field(default_factory=list)  # provider/model labels, for the invocation record
    model_names: list[str] = field(default_factory=list)  # what the reply shows (no provider)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    tool_seconds: dict[str, float] = field(default_factory=dict)
    slow_tools: frozenset[str] = frozenset()  # tools whose total time is shown
    ack_s: float | None = None
    ttft_s: float | None = None
    tokens_per_s: float | None = None
    total_s: float = 0.0
    moderation: str = ""
    trace_id: str | None = None

    def render(self, fields: list[str]) -> str:
        lines = []
        if "model" in fields and self.model_names:
            lines.append("模型  " + " · ".join(dict.fromkeys(self.model_names)))
        if "tokens" in fields:
            cached = f" / C {self.tokens_cached / 1000:.1f}k" if self.tokens_cached else ""
            lines.append(f"tokens  I {self.tokens_in / 1000:.1f}k / O {self.tokens_out}{cached}")
        if "tools" in fields:
            used = " · ".join(self._tool(k, v) for k, v in sorted(self.tools.items()))
            lines.append("工具  " + (used or "无"))
        if "latency" in fields:
            parts = [f"TTFT {self.ttft_s:.1f}s"] if self.ttft_s is not None else []
            parts.append(f"完成 {self.total_s:.1f}s")
            if self.tokens_per_s is not None:
                parts.append(f"{self.tokens_per_s:.0f} tok/s")
            lines.append("耗时  " + " · ".join(parts))
        if "moderation" in fields and self.moderation:
            lines.append(f"审核  {self.moderation}")
        if "trace" in fields and self.trace_id:
            lines.append(f"trace  {self.trace_id[:16]}")
        return "⚙ 元数据\n" + "\n".join(lines)

    def _tool(self, name: str, count: int) -> str:
        seconds = self.tool_seconds.get(name)
        took = f" {seconds:.1f}s" if name in self.slow_tools and seconds is not None else ""
        return f"{name}×{count}{took}"


def _record_self(group_id: int, self_id: int, nickname: str, message_id: int, segments: list[dict[str, Any]]) -> None:
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


async def send_text(
    api: NapCatAPI, *, group_id: int | None, user_id: int, reply_to: int | None, text: str, self_id: int, nickname: str
) -> int:
    segments = ([reply_seg(reply_to)] if reply_to else []) + [text_seg(text)]
    if group_id is None:
        res = await api.send_private_msg(user_id=user_id, message=segments)
    else:
        res = await api.send_group_msg(group_id=group_id, message=segments)
        _record_self(group_id, self_id, nickname, res.message_id, segments)
    return res.message_id


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
) -> int:
    """Images (with an optional line of text) as one plain message, replying to the trigger."""
    segments = [reply_seg(reply_to)] if reply_to else []
    if text:
        segments.append(text_seg(text))
    segments += [image_seg("base64://" + base64.b64encode(png).decode()) for png in images]
    if group_id is None:
        return (await api.send_private_msg(user_id=user_id, message=segments)).message_id
    res = await api.send_group_msg(group_id=group_id, message=segments)
    # Record the message as QQ stored it (image segments with file and url, not our base64), so a later reply to it
    # can load the picture again (e.g. "make it bluer").
    try:
        stored = [s.model_dump() for s in (await api.get_msg(message_id=res.message_id)).segments]
    except Exception as e:
        LOGGER.warning("get_msg after sending images failed: {}", e)
        stored = [s if s["type"] != "image" else text_seg("[图片]") for s in segments]
    _record_self(group_id, self_id, nickname, res.message_id, stored)
    return res.message_id


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
        bodies.append(
            "来源\n" + "\n".join(f"- {s.label}" + (f"\n  {s.url}" if s.url else "") for s in reply.sources[:10])
        )
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
) -> int:
    nodes = build_nodes(reply, meta_text, cfg, self_id=self_id, nickname=nickname, images=images)
    title = (reply.title or "回复")[:30]  # only for our own message record; never sent as a preview
    if group_id is None:
        res = await api.send_private_forward_msg(user_id=user_id, messages=nodes)
    else:
        res = await api.send_group_forward_msg(group_id=group_id, messages=nodes)
        # Keep the node texts with our own record: QQ hands self-sent forwards back as an opaque
        # card, so this is how a later "reply to the bot's forward" can read what was in it.
        stored = [_textual_node(n) for n in nodes]
        _record_self(
            group_id,
            self_id,
            nickname,
            res.message_id,
            [{"type": "forward", "data": {"id": res.res_id or "", "title": title, "content": stored}}],
        )
    return res.message_id


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

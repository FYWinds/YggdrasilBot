"""Sending replies: inline replies, merged forwards with a metadata node, reactions, stickers."""

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from yggdrasil.agent.schema import BotReply
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.napcat.api import NapCatAPI, image_seg, markdown_seg, mface_seg, node_seg, reply_seg, text_seg
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
    models: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tools: dict[str, int] = field(default_factory=dict)
    ack_s: float | None = None
    total_s: float = 0.0
    moderation: str = ""
    trace_id: str | None = None

    def render(self, fields: list[str]) -> str:
        lines = []
        if "model" in fields and self.models:
            lines.append("模型  " + " · ".join(dict.fromkeys(self.models)))
        if "tokens" in fields:
            cached = f" / 缓存 {self.tokens_cached / 1000:.1f}k" if self.tokens_cached else ""
            lines.append(f"tokens  输入 {self.tokens_in / 1000:.1f}k / 输出 {self.tokens_out}{cached}")
        if "tools" in fields:
            lines.append(
                "工具  " + (" · ".join(f"{k}×{v}" for k, v in sorted(self.tools.items())) if self.tools else "无")
            )
        if "latency" in fields:
            ack = f"首响应 {self.ack_s:.1f}s · " if self.ack_s is not None else ""
            lines.append(f"耗时  {ack}完成 {self.total_s:.1f}s")
        if "moderation" in fields and self.moderation:
            lines.append(f"审核  {self.moderation}")
        if "trace" in fields and self.trace_id:
            lines.append(f"trace  {self.trace_id[:16]}")
        return "⚙ 元数据\n" + "\n".join(lines)


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


async def send_sticker(api: NapCatAPI, *, group_id: int, sticker_id: int, self_id: int, nickname: str) -> bool:
    async with get_session() as session:
        sticker = await session.get(Sticker, sticker_id)
    if sticker is None or not sticker.enabled:
        return False
    segments = [mface_seg(sticker.emoji_id, sticker.package_id, sticker.key, f"[{sticker.summary}]")]
    res = await api.send_group_msg(group_id=group_id, message=segments)
    _record_self(group_id, self_id, nickname, res.message_id, segments)
    return True


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
    for section in reply.sections:
        content = section.content if cfg.forward_render == "nested_markdown" else strip_markdown(section.content)
        title = f"【{section.title}】\n" if section.title else ""
        for i, part in enumerate(split_text(content, cfg.node_max_chars)):
            bodies.append((title if i == 0 else "") + part)
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
    title = (reply.title or "回复")[:30]
    news = [s.title or strip_markdown(s.content)[:30] for s in reply.sections[:4]] or [title]
    kwargs = {
        "messages": nodes,
        "source": title,
        "summary": f"查看 {len(nodes)} 条消息",
        "prompt": f"[{title}]",
        "news": news,
    }
    if group_id is None:
        res = await api.send_private_forward_msg(user_id=user_id, **kwargs)
    else:
        res = await api.send_group_forward_msg(group_id=group_id, **kwargs)
        _record_self(
            group_id, self_id, nickname, res.message_id, [{"type": "forward", "data": {"id": res.res_id or ""}}]
        )
    return res.message_id

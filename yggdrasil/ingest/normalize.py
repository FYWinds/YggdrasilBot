"""Turn OneBot segments into placeholder text (the memory/LLM view of a message).

Non-text content is never expanded at ingest time: images, files, forwards and
cards become short placeholders, and the raw segments are kept in ``messages.segments``
so tools can fetch the real content on demand.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_URL = re.compile(r"https?://[^\s\u3000，。！？、）」』\])>\"']+")
_RECORD_TYPES = {"record": "语音", "video": "视频"}


@dataclass(slots=True)
class Attachment:
    kind: str  # image | file | forward | record | video | card | mface
    data: dict[str, Any]


@dataclass(slots=True)
class Normalized:
    text: str
    reply_to: int | None = None
    mentions: list[int] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def is_sticker_image(data: dict[str, Any]) -> bool:
    """Custom stickers arrive as image segments with ``sub_type`` 1 and a summary such as ``[动画表情]``."""
    summary = str(data.get("summary") or "").strip("[]")
    return str(data.get("sub_type")) == "1" or bool(summary and summary != "图片")


def _card(raw: str) -> tuple[str, str | None]:
    """Title and URL of a json card (mini-program, share link, news)."""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return "卡片", None
    title = doc.get("prompt") or ""
    url = None
    meta = doc.get("meta") or {}
    for item in meta.values() if isinstance(meta, dict) else []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("desc") or title
        url = item.get("qqdocurl") or item.get("jumpUrl") or item.get("url") or url
        if url:
            break
    return str(title).strip() or "卡片", url


def multimsg_resid(raw: str) -> str | None:
    """Merged forwards the bot itself sent come back from get_msg as a ``com.tencent.multimsg``
    json card instead of a ``forward`` segment; its ``resid`` works with get_forward_msg."""
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("app") != "com.tencent.multimsg":
        return None
    detail = (doc.get("meta") or {}).get("detail") or {}
    resid = detail.get("resid") if isinstance(detail, dict) else None
    return str(resid) if resid else None


def normalize(segments: list[dict[str, Any]], mention: Callable[[int], str] | None = None) -> Normalized:
    """``mention`` renders an at target for a model (``名字(QQ)``); without it the raw ``@<qq>`` is kept,
    which is what ingest stores in ``plain_text``."""
    out = Normalized(text="")
    parts: list[str] = []
    rendered_at = False  # previous part is a mention rendered through ``mention``
    # QQ follows a markdown segment or a market-face image with a plain-text fallback of the same content
    # (the markdown source, the ``[summary]``); that copy is dropped so the content shows once.
    echo: str | None = None
    for seg in segments:
        after_at, rendered_at = rendered_at, False
        fallback, echo = echo, None
        kind = seg.get("type", "")
        data = seg.get("data") or {}
        match kind:
            case "text":
                text = str(data.get("text", ""))
                if text == fallback:
                    continue
                if after_at and text[:1].isspace():
                    text = text[1:]  # the at already ends with a space (the stored form keeps both)
                parts.append(text)
            case "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                    continue
                uid = int(qq) if qq.isdigit() else None
                if uid is not None:
                    out.mentions.append(uid)
                if uid is not None and mention is not None:
                    parts.append(f"@{mention(uid)} ")
                    rendered_at = True
                    continue
                name = data.get("name") or qq
                parts.append(f"@{str(name).lstrip('@')} ")
            case "reply":
                rid = str(data.get("id", ""))
                out.reply_to = int(rid) if rid.lstrip("-").isdigit() else None
            case "face":
                raw = data.get("raw") or {}
                label = (raw.get("faceText") if isinstance(raw, dict) else None) or f"表情{data.get('id', '')}"
                parts.append(f"[表情:{str(label).lstrip('/')}]")
            case "mface":
                summary = str(data.get("summary") or "表情包").strip("[]")
                parts.append(f"[表情包:{summary}]")
                out.attachments.append(Attachment("mface", data))
                echo = data.get("summary")
            case "image":
                summary = str(data.get("summary") or "").strip("[]")
                parts.append(f"[表情包:{summary or '动画表情'}]" if is_sticker_image(data) else "[图片]")
                out.attachments.append(Attachment("image", data))
                if data.get("emoji_id"):
                    echo = data.get("summary")
            case "record" | "video":
                parts.append(f"[{_RECORD_TYPES[kind]}]")
                out.attachments.append(Attachment(kind, data))
            case "file":
                parts.append(f"[文件:{data.get('file') or data.get('name') or '未命名'}]")
                out.attachments.append(Attachment("file", data))
            case "forward":
                parts.append("[合并转发]")
                out.attachments.append(Attachment("forward", data))
            case "json" if resid := multimsg_resid(str(data.get("data", ""))):
                parts.append("[合并转发]")
                out.attachments.append(Attachment("forward", {"id": resid}))
            case "json":
                title, url = _card(str(data.get("data", "")))
                parts.append(f"[卡片:{title}]")
                out.attachments.append(Attachment("card", {"title": title, "url": url}))
                if url:
                    out.urls.append(url)
            case "xml":
                parts.append("[卡片]")
            case "poke":
                parts.append("[戳一戳]")
            case "dice" | "rps":
                parts.append(f"[{kind}:{data.get('result', '')}]")
            case "markdown":
                content = str(data.get("content", ""))
                parts.append(f"[markdown]{content}")
                echo = content
            case _:
                parts.append(f"[{kind}]")
    out.text = "".join(parts).strip()
    texts = [str(s.get("data", {}).get("text", "")) for s in segments if s.get("type") == "text"]
    for url in _URL.findall(" ".join(texts)):
        if url not in out.urls:
            out.urls.append(url)
    return out

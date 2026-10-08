"""Turn OneBot segments into placeholder text (the memory/LLM view of a message).

Non-text content is never expanded at ingest time: images, files, forwards and
cards become short placeholders, and the raw segments are kept in ``messages.segments``
so tools can fetch the real content on demand.
"""

import json
import re
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


def normalize(segments: list[dict[str, Any]], names: dict[int, str] | None = None) -> Normalized:
    names = names or {}
    out = Normalized(text="")
    parts: list[str] = []
    for seg in segments:
        kind = seg.get("type", "")
        data = seg.get("data") or {}
        match kind:
            case "text":
                parts.append(str(data.get("text", "")))
            case "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                    continue
                uid = int(qq) if qq.isdigit() else None
                if uid is not None:
                    out.mentions.append(uid)
                name = data.get("name") or (names.get(uid) if uid is not None else None) or qq
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
            case "image":
                summary = str(data.get("summary") or "").strip("[]")
                # NapCat reports market faces as images with sub_type 1 and a summary.
                if summary and summary != "图片":
                    parts.append(f"[表情包:{summary}]")
                else:
                    parts.append("[图片]")
                out.attachments.append(Attachment("image", data))
            case "record" | "video":
                parts.append(f"[{_RECORD_TYPES[kind]}]")
                out.attachments.append(Attachment(kind, data))
            case "file":
                parts.append(f"[文件:{data.get('file') or data.get('name') or '未命名'}]")
                out.attachments.append(Attachment("file", data))
            case "forward":
                parts.append("[合并转发]")
                out.attachments.append(Attachment("forward", data))
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
                parts.append(f"[markdown]{data.get('content', '')}")
            case _:
                parts.append(f"[{kind}]")
    out.text = "".join(parts).strip()
    texts = [str(s.get("data", {}).get("text", "")) for s in segments if s.get("type") == "text"]
    for url in _URL.findall(" ".join(texts)):
        if url not in out.urls:
            out.urls.append(url)
    return out

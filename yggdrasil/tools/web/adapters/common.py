"""Helpers shared by the site adapters: China time, counts and durations, secondary requests, transcripts, raw
source files and HTML fragments."""

import asyncio
import re
from collections.abc import Awaitable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.tools.docs import parse_document
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.model import MAX_HTML_BYTES, AdapterResult, Extracted

CN_TZ = timezone(timedelta(hours=8))


def fmt_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def cn_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


async def optional_get(request: Awaitable[httpx.Response], what: str) -> httpx.Response | None:
    """Await a request for secondary content (comments, subtitles, READMEs): the response if it is a 200, else
    ``None``. Network errors are logged, never raised: they must not lose the page already read."""
    try:
        resp = await request
    except (httpx.HTTPError, ToolError) as exc:
        LOGGER.warning("{} failed: {!r}", what, exc)
        return None
    return resp if resp.status_code == 200 else None


async def optional_json(request: Awaitable[httpx.Response], what: str) -> Any:
    """:func:`optional_get`, decoded as JSON (``None`` if it is not JSON)."""
    resp = await optional_get(request, what)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        LOGGER.warning("{} answered non-JSON", what)
        return None


_TRANSCRIPT_PARAGRAPH_S = 60


def transcript(cues: Iterable[tuple[float, str]]) -> str:
    """Timed subtitle lines as paragraphs of about a minute, each led by its start time."""
    paragraphs: list[str] = []
    texts: list[str] = []
    start = 0.0
    for at, raw in cues:
        if not (line := " ".join(raw.split())):
            continue
        if texts and at - start >= _TRANSCRIPT_PARAGRAPH_S:
            paragraphs.append(f"[{fmt_duration(int(start))}] {' '.join(texts)}")
            texts = []
        if not texts:
            start = at
        texts.append(line)
    if texts:
        paragraphs.append(f"[{fmt_duration(int(start))}] {' '.join(texts)}")
    return "\n\n".join(paragraphs)


def no_transcript(why: str) -> str:
    return f"（{why}，只有标题和简介，无法得知视频里具体说了什么）"


async def raw_file(
    client: httpx.AsyncClient, raw: str, title: str, via: str, headers: dict[str, str] | None = None
) -> AdapterResult:
    """A source file from its raw URL, read like a document link; ``None`` (generic path) when it is not there."""
    resp = await client.get(raw, headers=headers)
    if resp.status_code != 200 or len(resp.content) > MAX_HTML_BYTES:
        return None
    doc = await asyncio.to_thread(parse_document, resp.content, raw.rsplit("/", 1)[-1])
    return Extracted(raw, title, doc.text, via, capped=doc.truncated)


def cn_datetime(stamp: object) -> str:
    """An API timestamp (ISO 8601, UTC) in China time."""
    if not stamp:
        return "未知时间"
    return f"{datetime.fromisoformat(str(stamp)).astimezone(CN_TZ):%Y-%m-%d %H:%M}"


def fragment_text(html: str) -> str:
    """Markdown text of a small trusted-structure HTML fragment (comment bodies, post text)."""
    from markdownify import markdownify

    text = markdownify(html, heading_style="ATX", strip=["img"], escape_underscores=False, escape_asterisks=False)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

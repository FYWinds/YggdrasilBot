"""``read_url``: fetch a web page (or a document link) and return readable, paginated text.

Pipeline: site adapter (official APIs, over httpx) -> Chrome-impersonating curl_cffi + trafilatura
(readability-lxml + markdownify fallback) -> the browser service (real Chrome: patchright, then nodriver) when the
page is a JS app, answers 401/403/429/503, or is an anti-bot block page / JS challenge. Captcha and login walls
raise :class:`ToolError` instead of returning junk; we never try to defeat captchas.
"""

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

import httpx
from curl_cffi.requests.exceptions import RequestException as CurlRequestError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from yggdrasil.core.log import LOGGER
from yggdrasil.core.telemetry import TRACER
from yggdrasil.tools import x
from yggdrasil.tools.docs import decode_html, parse_document
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import GuardedResponse, check_url, guarded_client, impersonated_stream
from yggdrasil.tools.web.blocks import JS_CHALLENGES, detect_wall, is_challenge, raise_if_blocked, raise_if_wall
from yggdrasil.tools.web.extract import MIN_TEXT_CHARS, extract_html, json_text
from yggdrasil.tools.web.model import (
    API_USER_AGENT,
    MAX_HTML_BYTES,
    MB,
    AdapterFn,
    AdapterResult,
    Extracted,
    PageResult,
    RemoteFetch,
    Unreadable,
    read_capped,
)
from yggdrasil.tools.web.page import Anchor, find_anchor, fragment_view, paginate

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": _ACCEPT_LANGUAGE,
}
MAX_DOC_BYTES = 20 * MB
_BROWSER_RETRY_STATUSES = frozenset({401, 403, 429, 503})
# Network timeouts apply to single reads, so a server that trickles bytes never trips them: each adapter call and
# each page download gets at most this many request timeouts in all.
_DEADLINE_TIMEOUTS = 3
# Browser service engines, in escalation order (see yggdrasil.browser.service).
_BROWSER_ENGINES = ("patchright", "nodriver")
_CACHE_TTL = 600.0
_CACHE_SIZE = 32
_DOC_TYPES = (
    "application/pdf",
    "application/msword",
    "application/vnd.ms-",
    "application/vnd.openxmlformats-officedocument.",
    "application/epub+zip",
    "application/zip",
    "application/json",
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/x-",
)
_DOC_EXTS = frozenset(".pdf .docx .pptx .xlsx .epub .zip .txt .md .csv .json .doc .xls .ppt".split())
_HTML_TYPES = frozenset({"", "text/html", "application/xhtml+xml", "application/octet-stream"})
_CN_TZ = timezone(timedelta(hours=8))


_ADAPTERS: dict[str, AdapterFn] = {}
_PATH_ADAPTERS: list[tuple[re.Pattern[str], AdapterFn]] = []
type _CacheKey = tuple[str, str, bool, bool]
"""(url, fragment, browser tier on, remote tier on)."""
_cache: OrderedDict[_CacheKey, tuple[float, Extracted, Anchor | None]] = OrderedDict()


def site_adapter(*hosts: str) -> Callable[[AdapterFn], AdapterFn]:
    """Register an adapter for exact host names (lower-case)."""

    def register(fn: AdapterFn) -> AdapterFn:
        for host in hosts:
            _ADAPTERS[host] = fn
        return fn

    return register


def path_adapter(pattern: re.Pattern[str]) -> Callable[[AdapterFn], AdapterFn]:
    """Register an adapter for URL paths matching ``pattern`` on hosts without a host adapter (hosted platforms
    such as Statuspage serve their customers' own domains)."""

    def register(fn: AdapterFn) -> AdapterFn:
        _PATH_ADAPTERS.append((pattern, fn))
        return fn

    return register


def _adapter_for(url: httpx.URL) -> AdapterFn | None:
    if (adapter := _ADAPTERS.get(url.host.lower())) is not None:
        return adapter
    return next((fn for pattern, fn in _PATH_ADAPTERS if pattern.match(url.path)), None)


async def read_url(
    url: str,
    *,
    max_chars: int = 16000,
    page: int = 1,
    browser_url: str | None = None,
    remote: RemoteFetch | None = None,
    timeout: float = 20,  # noqa: ASYNC109 - per-request network timeout, not a cancel scope
) -> PageResult:
    """Read ``url`` and return page ``page`` of its text split into ``max_chars`` pages.

    A ``#fragment`` pointing into a text longer than a page starts the pages there; page 1 then opens with a note
    saying so (or why the fragment could not be followed)."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if page < 1:
        raise ToolError("页码从 1 开始")
    target = await check_url(url)
    # A text fragment (``#:~:text=...``, added by search engines) only highlights text; the page is read whole.
    fragment = unquote(url.partition("#")[2].partition(":~:")[0]).strip()
    key = (target, fragment, browser_url is not None, remote is not None)
    cached = _cache_get(key)
    with TRACER.start_as_current_span("tool.read_url") as span:
        if cached is None:
            extracted = await _read(target, browser_url, remote, timeout)
            anchor = await asyncio.to_thread(find_anchor, extracted, fragment) if fragment else None
            extracted.html = ""
            cached = extracted, anchor
            _cache_put(key, extracted, anchor)
        extracted, anchor = cached
        span.set_attribute("ygg.read_url.via", extracted.via)
    text, note = fragment_view(extracted.text, fragment, anchor, max_chars)
    pages = paginate(text, max_chars)
    if page > len(pages):
        raise ToolError(f"页码超出范围，共 {len(pages)} 页")
    return PageResult(
        url=target,
        final_url=extracted.final_url,
        title=extracted.title,
        text=f"{note}\n\n{pages[0]}" if note and page == 1 else pages[page - 1],
        page=page,
        pages=len(pages),
        truncated=page < len(pages) or extracted.capped,
        via=extracted.via,
        images=extracted.images if page == 1 else [],
    )


async def _read(url: str, browser_url: str | None, remote: RemoteFetch | None, timeout_s: float) -> Extracted:
    """The local tiers, then ``remote`` when they could not read the page or only got a shell."""
    try:
        local = await _fetch(url, browser_url, timeout_s)
    except Unreadable:
        if remote is None or (fetched := await _remote(remote, url)) is None:
            raise
        return fetched
    generic = local.via == "http" or local.via.startswith("browser")  # adapters' short answers are complete
    if remote is not None and generic and len(local.text) < MIN_TEXT_CHARS:
        fetched = await _remote(remote, url)
        if fetched is not None and len(fetched.text) > len(local.text):
            return fetched
    return local


async def _remote(remote: RemoteFetch, url: str) -> Extracted | None:
    try:
        fetched = await remote(url)
    except Exception as exc:
        LOGGER.warning("remote fetch failed for {}: {!r}", httpx.URL(url).host, exc)
        return None
    if (
        fetched is None
        or not fetched.text
        or detect_wall(fetched.title, fetched.text)
        or is_challenge(fetched.title, fetched.text)
    ):
        return None  # the remote copy is a wall / block page too
    return fetched


# --------------------------------------------------------------------------- fetch pipeline


async def _fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
    try:
        async with guarded_client(timeout=timeout_s, headers=_HEADERS) as client:
            for _ in range(3):
                adapter = _adapter_for(httpx.URL(url))
                if adapter is None:
                    break
                async with asyncio.timeout(timeout_s * _DEADLINE_TIMEOUTS):
                    result = await adapter(client, httpx.URL(url))
                if isinstance(result, str):
                    url = await check_url(result)
                    continue
                if result is not None:
                    return result
                break
        return await _fetch_generic(url, browser_url, timeout_s)
    except httpx.TooManyRedirects as exc:
        raise ToolError("重定向次数过多") from exc
    except (httpx.TimeoutException, CurlTimeout, TimeoutError) as exc:
        raise Unreadable("网页加载超时") from exc
    except (httpx.HTTPError, CurlRequestError) as exc:
        raise Unreadable("网页无法访问") from exc


@dataclass(slots=True)
class _Body:
    filename: str
    is_doc: bool
    is_json: bool
    limit: int
    data: bytes
    capped: bool
    charset: str | None


async def _fetch_generic(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
    # curl_cffi owns the browser header set (UA, client hints) so it matches the impersonated TLS fingerprint.
    async with (
        asyncio.timeout(timeout_s * _DEADLINE_TIMEOUTS),
        impersonated_stream(url, timeout=timeout_s, headers={"Accept-Language": _ACCEPT_LANGUAGE}) as resp,
    ):
        final_url, status = resp.url, resp.status_code
        body = await _download(resp) if status < 400 else None
    if body is None:
        if status in _BROWSER_RETRY_STATUSES and browser_url:
            return await _render_or_raise(final_url, browser_url, timeout_s, None)
        raise Unreadable(f"网页返回错误（HTTP {status}）")

    if body.is_doc or body.data[:1024].lstrip().startswith(b"%PDF-"):
        if body.capped:
            raise ToolError(f"文件超过 {body.limit // MB} MB，无法读取")
        if body.is_json and (compact := await asyncio.to_thread(json_text, body.data, body.charset)):
            text, clipped = compact
            return Extracted(final_url, body.filename, text, "document:json", capped=clipped)
        doc = await asyncio.to_thread(parse_document, body.data, body.filename)
        if not doc.text.strip():
            if doc.scanned_pages:
                raise ToolError("这是扫描版 PDF，没有文字层，无法直接读取")
            raise ToolError("文档中没有可读取的文字")
        return Extracted(final_url, body.filename, doc.text, f"document:{doc.kind}", capped=doc.truncated)

    html = decode_html(body.data, body.charset)
    title, text = await asyncio.to_thread(extract_html, html, final_url)
    extracted = Extracted(final_url, title, text, "http", capped=body.capped, html=html)
    challenged = is_challenge(title, text)
    thin = len(text) < MIN_TEXT_CHARS and detect_wall(title, text) is None
    if browser_url and (challenged or thin):
        return await _render_or_raise(final_url, browser_url, timeout_s, None if challenged else extracted)
    raise_if_wall(extracted)
    raise_if_blocked(extracted)
    if not text:
        raise Unreadable("页面没有可读取的正文")
    return extracted


async def _download(resp: GuardedResponse) -> _Body:
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    filename = _filename(resp.headers, resp.url)
    if ctype.startswith("image/"):
        raise ToolError("这是图片链接，不是网页，请用看图工具查看")
    if ctype.startswith(("audio/", "video/")):
        raise ToolError("这是音视频文件链接，无法读取内容")
    json_type = ctype == "application/json" or ctype.endswith("+json")
    is_json = json_type or _ext(filename) == ".json"
    is_doc = json_type or ctype.startswith(_DOC_TYPES) or (ctype in _HTML_TYPES and _ext(filename) in _DOC_EXTS)
    if not is_doc and ctype not in _HTML_TYPES:
        raise ToolError(f"不支持的内容类型：{ctype}")
    limit = MAX_DOC_BYTES if is_doc else MAX_HTML_BYTES
    declared = resp.headers.get("content-length", "")
    if is_doc and declared.isdigit() and int(declared) > limit:
        raise ToolError(f"文件超过 {limit // MB} MB，无法读取")
    data, capped = await read_capped(resp.chunks, limit)
    return _Body(filename, is_doc, is_json, limit, data, capped, resp.charset)


def _filename(headers: dict[str, str], final_url: str) -> str:
    disposition = headers.get("content-disposition", "")
    if match := re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.IGNORECASE):
        return unquote(match.group(1).strip().strip('"'))
    if match := re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE):
        return match.group(1).strip()
    return unquote(httpx.URL(final_url).path.rsplit("/", 1)[-1])


def _ext(filename: str) -> str:
    _, dot, ext = filename.lower().rpartition(".")
    return f".{ext}" if dot else ""


async def _render_or_raise(url: str, browser_url: str, timeout_s: float, fallback: Extracted | None) -> Extracted:
    """Render with the browser service, moving to the next engine on failure, block pages and JS challenges.

    Captcha / login walls raise at once; if no engine produced text, the plain-http ``fallback`` is kept.
    """
    rendered: Extracted | None = None
    for engine in _BROWSER_ENGINES:
        try:
            final_url, html = await render_page(url, browser_url, engine, timeout=timeout_s)
        except ToolError:
            raise
        except Exception as exc:
            LOGGER.warning("browser render ({}) failed for {}: {!r}", engine, httpx.URL(url).host, exc)
            continue
        title, text = await asyncio.to_thread(extract_html, html, final_url)
        via = "browser" if engine == _BROWSER_ENGINES[0] else f"browser:{engine}"
        rendered = Extracted(final_url, title, text, via, html=html)
        if text and not is_challenge(title, text):
            break
        if detect_wall(title, text) not in (None, *JS_CHALLENGES):
            break  # captcha / login wall: another engine will not help
    if rendered is not None:
        raise_if_wall(rendered)
        raise_if_blocked(rendered)
        if rendered.text:
            return rendered
    if fallback is not None and fallback.text:
        return fallback
    raise Unreadable("网页渲染失败，无法读取" if rendered is None else "页面没有可读取的正文")


async def render_page(url: str, browser_url: str, engine: str, *, timeout: float = 20) -> tuple[str, str]:  # noqa: ASYNC109
    """Load ``url`` with ``engine`` in the browser service and return ``(final_url, html)``.

    The service SSRF-checks the URL and every request the page makes; a rejected URL raises :class:`ToolError`.
    """
    # Plain client: the service is internal; nodriver starts a fresh Chrome, hence the margin.
    async with httpx.AsyncClient(timeout=timeout + 15) as client:
        resp = await client.post(
            f"{browser_url.rstrip('/')}/render", json={"url": url, "engine": engine, "timeout": timeout}
        )
    if resp.status_code == 400:
        raise ToolError(str(resp.json().get("error") or "不允许访问该链接"))
    resp.raise_for_status()
    data = resp.json()
    return str(data["final_url"]), str(data["html"])


def _cache_get(key: _CacheKey) -> tuple[Extracted, Anchor | None] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, extracted, anchor = entry
    if time.monotonic() - stored_at > _CACHE_TTL:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return extracted, anchor


def _cache_put(key: _CacheKey, extracted: Extracted, anchor: Anchor | None) -> None:
    _cache[key] = (time.monotonic(), extracted, anchor)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)


# --------------------------------------------------------------------------- site adapters

_BILI_VIDEO_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", re.IGNORECASE)
_BILI_GONE_CODES = frozenset({-404, 62002, 62004, 62012})


@site_adapter("b23.tv", "www.b23.tv", "bili2233.cn")
async def _bilibili_short_link(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    resp = await client.get(url, follow_redirects=False)
    location = resp.headers.get("location")
    if resp.is_redirect and location:
        return str(resp.url.join(location))
    return None


@site_adapter("www.bilibili.com", "bilibili.com", "m.bilibili.com")
async def _bilibili_video(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    match = _BILI_VIDEO_RE.search(url.path)
    if match is None:
        return None
    vid = match.group(1)
    params = {"bvid": vid} if vid[:2].upper() == "BV" else {"aid": vid[2:]}
    resp = await client.get(
        "https://api.bilibili.com/x/web-interface/view", params=params, headers={"User-Agent": API_USER_AGENT}
    )
    try:
        payload = resp.json()
    except ValueError:
        return None
    code = payload.get("code")
    if code in _BILI_GONE_CODES:
        raise ToolError("B 站视频不存在、已删除或需要登录才能看")
    if code != 0 or not isinstance(payload.get("data"), dict):
        return None  # risk control etc.; the generic path may still get something
    data = payload["data"]
    bvid = data.get("bvid") or vid
    p = url.params.get("p", "")
    part = int(p) if p.isdigit() and 0 < int(p) <= len(data.get("pages") or ()) else 1
    return Extracted(
        final_url=f"https://www.bilibili.com/video/{bvid}" + (f"?p={part}" if part > 1 else ""),
        title=str(data.get("title", "")),
        text=f"{format_bilibili_video(data)}\n\n{await _bilibili_subtitles(client, data, part)}",
        via="bilibili",
    )


async def _bilibili_subtitles(client: httpx.AsyncClient, data: dict, part: int) -> str:
    """Subtitles of part ``part`` as a transcript, or a note saying why there are none.

    The player APIs hide subtitles from anonymous visitors (``need_login_subtitle``); the danmaku view API still
    lists them with their URLs, AI-generated ones included.
    """
    parts = data.get("pages") or []
    cid = parts[part - 1].get("cid") if 0 < part <= len(parts) else data.get("cid")
    view = await _optional_json(
        client.get(
            "https://api.bilibili.com/x/v2/dm/view",
            params={"type": 1, "oid": cid, "pid": data.get("aid")},
            headers={"User-Agent": API_USER_AGENT},
        ),
        "bilibili subtitle list",
    )
    if not isinstance(view, dict) or view.get("code") != 0:
        return _no_transcript("字幕读取失败")
    subtitle = (view.get("data") or {}).get("subtitle") or {}
    tracks = [t for t in subtitle.get("subtitles") or [] if isinstance(t, dict) and t.get("subtitle_url")]
    if not tracks:
        listed = (data.get("subtitle") or {}).get("list")
        return _no_transcript("这个视频有字幕，但 B 站要求登录才能读取" if listed else "这个视频没有字幕")
    track = min(tracks, key=_bilibili_track_rank)
    src = str(track["subtitle_url"])
    src = f"https:{src}" if src.startswith("//") else src.replace("http://", "https://", 1)
    body = await _optional_json(client.get(src, headers={"User-Agent": API_USER_AGENT}), "bilibili subtitles")
    cues = body.get("body") if isinstance(body, dict) else None
    text = _transcript(
        (float(c.get("from") or 0), str(c.get("content") or "")) for c in cues or () if isinstance(c, dict)
    )
    if not text:
        return _no_transcript("字幕读取失败")
    label = str(track.get("lan_doc") or track.get("lan") or "")
    return f"字幕（{f'P{part} · ' if len(parts) > 1 else ''}{label}）：\n\n{text}"


def _bilibili_track_rank(track: dict) -> tuple[bool, bool, bool]:
    """Uploaded subtitles before AI ones, AI transcriptions before AI translations, then Chinese first."""
    lang = str(track.get("lan", "")).removeprefix("ai-")
    return track.get("type") != 0, track.get("ai_type") != 0, not lang.startswith("zh")


def format_bilibili_video(data: dict) -> str:
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    lines = [f"UP主：{owner.get('name', '未知')}"]
    if pubdate := data.get("pubdate"):
        lines.append(f"发布时间：{datetime.fromtimestamp(pubdate, UTC).astimezone(_CN_TZ):%Y-%m-%d %H:%M}")
    if duration := data.get("duration"):
        lines.append(f"时长：{_fmt_duration(int(duration))}")
    if tname := data.get("tname") or data.get("tname_v2"):
        lines.append(f"分区：{tname}")
    counters = (
        ("播放", "view"),
        ("弹幕", "danmaku"),
        ("点赞", "like"),
        ("投币", "coin"),
        ("收藏", "favorite"),
        ("分享", "share"),
        ("评论", "reply"),
    )
    lines.append("数据：" + " · ".join(f"{label} {_cn_count(stat[key])}" for label, key in counters if key in stat))
    desc = str(data.get("desc") or "").strip()
    if desc and desc != "-":
        lines += ["", "简介：", desc]
    parts = data.get("pages") or []
    if len(parts) > 1:
        lines += ["", f"分P（共 {len(parts)} 个）："]
        lines += [
            f"P{p.get('page', i)} {p.get('part', '')}（{_fmt_duration(int(p.get('duration', 0)))}）"
            for i, p in enumerate(parts[:50], 1)
        ]
    return "\n".join(lines)


def _fmt_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _cn_count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


async def _optional_get(request: Awaitable[httpx.Response], what: str) -> httpx.Response | None:
    """Await a request for secondary content (comments, subtitles, READMEs): the response if it is a 200, else
    ``None``. Network errors are logged, never raised: they must not lose the page already read."""
    try:
        resp = await request
    except (httpx.HTTPError, ToolError) as exc:
        LOGGER.warning("{} failed: {!r}", what, exc)
        return None
    return resp if resp.status_code == 200 else None


async def _optional_json(request: Awaitable[httpx.Response], what: str) -> Any:
    """:func:`_optional_get`, decoded as JSON (``None`` if it is not JSON)."""
    resp = await _optional_get(request, what)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        LOGGER.warning("{} answered non-JSON", what)
        return None


_TRANSCRIPT_PARAGRAPH_S = 60


def _transcript(cues: Iterable[tuple[float, str]]) -> str:
    """Timed subtitle lines as paragraphs of about a minute, each led by its start time."""
    paragraphs: list[str] = []
    texts: list[str] = []
    start = 0.0
    for at, raw in cues:
        if not (line := " ".join(raw.split())):
            continue
        if texts and at - start >= _TRANSCRIPT_PARAGRAPH_S:
            paragraphs.append(f"[{_fmt_duration(int(start))}] {' '.join(texts)}")
            texts = []
        if not texts:
            start = at
        texts.append(line)
    if texts:
        paragraphs.append(f"[{_fmt_duration(int(start))}] {' '.join(texts)}")
    return "\n\n".join(paragraphs)


def _no_transcript(why: str) -> str:
    return f"（{why}，只有标题和简介，无法得知视频里具体说了什么）"


@site_adapter(*x.HOSTS)
async def _x_post(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    pid = x.post_id(str(url))
    if pid is None:
        return None
    from yggdrasil.runtime import snapshot

    try:
        post = await x.read_post(pid, x.provider(snapshot().search_providers), client=client)
    except x.PostGone:
        raise
    except ToolError as exc:
        LOGGER.warning("x post {} unreadable: {}", pid, exc)
        return None  # FxTwitter down and no X API: the generic path may still get the text
    return Extracted(
        final_url=post.url,
        title=f"{x.who(post)} 在 X 上的帖子",
        text=x.format_post(post),
        via=post.via,
        images=[m.url for m in post.images()],
    )


_GITHUB_RESERVED = frozenset(
    "about advisories apps codespaces collections copilot dashboard enterprise events explore features issues login "
    "marketplace new notifications organizations orgs pricing pulls readme search security settings solutions "
    "sponsors team topics trending users".split()
)
# Unauthenticated: 60 requests an hour per IP. Over the limit, adapters fall back to the generic path.
_GITHUB_API = "https://api.github.com"
_GITHUB_PAGE_SIZE = 100
_GITHUB_USER_REPOS = 30
# Branch names may contain "/" and tree URLs do not say where the ref ends: at most this many splits are tried.
_GITHUB_REF_TRIES = 3
_GITHUB_MAINTAINERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_GITHUB_CLOSE_REASONS = {"completed": "已完成", "not_planned": "不计划处理", "duplicate": "重复"}
_GITHUB_REVIEW_STATES = {
    "APPROVED": "审查：批准",
    "CHANGES_REQUESTED": "审查：要求修改",
    "COMMENTED": "审查",
    "DISMISSED": "审查（已撤销）",
}
# "Moved to #123", "superseded by <PR link>", "迁移到 #12": the thread points at its successor.
_GITHUB_SUCCESSOR_RE = re.compile(
    r"(?:\b(?:moved|migrated|continued|reopened|recreated)\s+(?:to|in|as|at)|\b(?:superseded|replaced)\s+by"
    r"|\bin\s+favou?r\s+of|\bduplicate\s+of|(?:迁移|移|转)(?:到|至))"
    r"\s*(#\d+|https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+)",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@site_adapter("github.com", "www.github.com")
async def _github(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    segments = [s for s in url.path.split("/") if s]
    if not segments:
        return None
    if segments[0].lower() == "orgs" and len(segments) >= 2:
        if segments[2:] not in ([], ["repositories"]):
            return None
        return await _github_user(client, segments[1], readme=len(segments) == 2)
    if segments[0].lower() in _GITHUB_RESERVED:
        return None
    if len(segments) == 1:
        tab = url.params.get("tab", "")
        if tab not in ("", "overview", "repositories"):
            return None  # stars, followers, ...: generic HTML extraction
        return await _github_user(client, segments[0], readme=tab != "repositories")
    owner, repo = segments[0], segments[1].removesuffix(".git")
    kind, rest = (segments[2], segments[3:]) if len(segments) > 2 else ("", [])
    if kind == "blob" and len(rest) >= 2:
        return await _github_blob(client, owner, repo, rest[0], "/".join(rest[1:]))
    if kind == "tree" and len(rest) >= 2:
        return await _github_tree(client, owner, repo, rest)
    if kind in ("issues", "pull") and len(rest) == 1 and rest[0].isdigit():
        thread = _github_issue if kind == "issues" else _github_pull
        return await thread(client, owner, repo, int(rest[0]))
    if kind and not (kind == "tree" and len(rest) == 1):
        return None  # commits, actions, PR diffs, discussions, ...: generic HTML extraction handles them
    return await _github_repo(client, owner, repo, rest[0] if rest else None)


async def _github_api(client: httpx.AsyncClient, path: str, **params: str | int) -> httpx.Response:
    return await client.get(f"{_GITHUB_API}{path}", params=params, headers={"Accept": "application/vnd.github+json"})


async def _github_list(client: httpx.AsyncClient, path: str, **params: str | int) -> list[dict] | None:
    """A secondary API list (comments, reviews, repos, files); ``None`` when it could not be read."""
    items = await _optional_json(_github_api(client, path, **params), "github api")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None


async def _github_blob(client: httpx.AsyncClient, owner: str, repo: str, ref: str, path: str) -> AdapterResult:
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    return await _raw_file(client, raw, f"{owner}/{repo}: {path}", "github")


async def _raw_file(
    client: httpx.AsyncClient, raw: str, title: str, via: str, headers: dict[str, str] | None = None
) -> AdapterResult:
    """A source file from its raw URL, read like a document link; ``None`` (generic path) when it is not there."""
    resp = await client.get(raw, headers=headers)
    if resp.status_code != 200 or len(resp.content) > MAX_HTML_BYTES:
        return None
    doc = await asyncio.to_thread(parse_document, resp.content, raw.rsplit("/", 1)[-1])
    return Extracted(raw, title, doc.text, via, capped=doc.truncated)


async def _github_tree(client: httpx.AsyncClient, owner: str, repo: str, rest: list[str]) -> AdapterResult:
    """A folder page (``rest`` is ``[ref, *path]``): its entries from the contents API, then its README."""
    for cut in range(1, min(len(rest), _GITHUB_REF_TRIES) + 1):
        ref, path = "/".join(rest[:cut]), "/".join(rest[cut:])
        if not path:
            return await _github_repo(client, owner, repo, ref)
        resp = await _github_api(client, f"/repos/{owner}/{repo}/contents/{path}", ref=ref)
        if resp.status_code == 404 and "No commit found for the ref" in resp.text:
            continue  # the ref goes on past this "/"
        if resp.status_code == 404:
            raise ToolError("GitHub 上没有这个目录，或仓库是私有的")
        if resp.status_code != 200:
            return None  # rate limited: the generic page still shows the folder README
        entries = resp.json()
        if isinstance(entries, dict):
            return await _github_blob(client, owner, repo, ref, path)  # a file, not a folder
        return await _github_dir(client, owner, repo, ref, path, [e for e in entries if isinstance(e, dict)])
    raise ToolError("GitHub 仓库里没有这个分支")


async def _github_dir(
    client: httpx.AsyncClient, owner: str, repo: str, ref: str, path: str, entries: list[dict]
) -> Extracted:
    lines = [
        f"仓库：{owner}/{repo}（{ref}）",
        f"目录：{path}",
        "",
        f"内容（{len(entries)} 项）：",
        *_github_entry_lines(entries),
        "",
        f"读取文件：https://github.com/{owner}/{repo}/blob/{ref}/{path}/<文件名>",
    ]
    readmes = (e for e in entries if e.get("type") == "file" and str(e.get("name")).lower().startswith("readme"))
    readme = next(readmes, {})
    if readme.get("download_url") and int(readme.get("size") or 0) <= MAX_HTML_BYTES:
        resp = await _optional_get(client.get(str(readme["download_url"])), "github readme")
        if resp is not None and (text := resp.text.strip()):
            lines += ["", f"{readme['name']}：", "", text]
    return Extracted(
        f"https://github.com/{owner}/{repo}/tree/{ref}/{path}", f"{owner}/{repo}: {path}", "\n".join(lines), "github"
    )


def _github_entry_lines(entries: list[dict]) -> list[str]:
    """One line per contents-API entry, folders first as on GitHub."""
    return [
        f"{e.get('name')}/" if e.get("type") == "dir" else f"{e.get('name')}（{_fmt_size(int(e.get('size') or 0))}）"
        for e in sorted(entries, key=lambda e: e.get("type") != "dir")
    ]


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < MB:
        return f"{size / 1024:.1f} KB"
    return f"{size / MB:.1f} MB"


async def _github_repo(client: httpx.AsyncClient, owner: str, repo: str, ref: str | None) -> AdapterResult:
    api = f"/repos/{owner}/{repo}"
    info_resp = await _github_api(client, api)
    if info_resp.status_code == 404:
        raise ToolError("GitHub 仓库不存在或是私有仓库")
    info = info_resp.json() if info_resp.status_code == 200 else {}
    readme = ""
    raw = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/README.md")
    if raw.status_code == 200:
        readme = raw.text
    else:
        params = {"ref": ref} if ref else None
        api_readme = await client.get(
            f"{_GITHUB_API}{api}/readme", params=params, headers={"Accept": "application/vnd.github.raw"}
        )
        if api_readme.status_code == 200:
            readme = api_readme.text
    if not info and not readme:
        return None
    files = None if readme.strip() else await _github_list(client, f"{api}/contents", **({"ref": ref} if ref else {}))
    full_name = info.get("full_name") or f"{owner}/{repo}"
    lines = [f"仓库：{full_name}"]
    if description := info.get("description"):
        lines.append(f"简介：{description}")
    if homepage := info.get("homepage"):
        lines.append(f"主页：{homepage}")
    if info:
        facts = [
            f"语言 {info.get('language') or '未知'}",
            f"★ {info.get('stargazers_count', 0)}",
            f"Fork {info.get('forks_count', 0)}",
            f"开放 issue {info.get('open_issues_count', 0)}",
        ]
        lines.append(" · ".join(facts))
        if (license_ := info.get("license")) and license_.get("spdx_id"):
            lines.append(f"许可证：{license_['spdx_id']}")
        if topics := info.get("topics"):
            lines.append("主题：" + "、".join(topics))
        if pushed := info.get("pushed_at"):
            lines.append(f"最近推送：{pushed[:10]}")
        if info.get("archived"):
            lines.append("（仓库已归档）")
    if readme.strip():
        lines += ["", "README：", "", readme.strip()]
    elif files:
        lines += ["", f"仓库没有 README。根目录（{len(files)} 项）：", *_github_entry_lines(files)]
    return Extracted(f"https://github.com/{full_name}", full_name, "\n".join(lines), "github")


async def _github_user(client: httpx.AsyncClient, name: str, *, readme: bool) -> AdapterResult:
    """A user or organization page: profile, recently pushed repos and (overview only) the profile README."""
    resp = await _github_api(client, f"/users/{name}")
    if resp.status_code != 200:
        return None  # not an account (some other GitHub page), or rate limited: generic path
    user = resp.json()
    login = str(user.get("login") or name)
    org = user.get("type") == "Organization"
    repos = await _github_list(client, f"/users/{login}/repos", sort="pushed", per_page=_GITHUB_USER_REPOS)
    profile = ""
    if readme:
        home = f"{login}/.github/HEAD/profile" if org else f"{login}/{login}/HEAD"
        page = await _optional_get(client.get(f"https://raw.githubusercontent.com/{home}/README.md"), "github profile")
        profile = page.text if page is not None else ""
    return Extracted(
        f"https://github.com/{login}",
        f"GitHub {'组织' if org else '用户'} {login}",
        format_github_user(user, repos, profile),
        "github",
    )


def format_github_user(user: dict, repos: list[dict] | None, readme: str) -> str:
    org = user.get("type") == "Organization"
    head = f"GitHub {'组织' if org else '用户'}：{user.get('login', '')}"
    lines = [f"{head}（{user['name']}）" if user.get("name") else head]
    if bio := str(user.get("bio") or "").strip():
        lines.append(f"简介：{bio}")
    twitter = user.get("twitter_username")
    contacts = [
        f"{label} {value}"
        for label, value in (
            ("公司", user.get("company")),
            ("位置", user.get("location")),
            ("网站", user.get("blog")),
            ("X", f"@{twitter}" if twitter else None),
        )
        if value
    ]
    if contacts:
        lines.append(" · ".join(contacts))
    counts = [f"公开仓库 {user.get('public_repos', 0)}", f"关注者 {user.get('followers', 0)}"]
    if not org:
        counts.append(f"关注 {user.get('following', 0)}")
    if created := user.get("created_at"):
        counts.append(f"注册于 {str(created)[:10]}")
    lines.append(" · ".join(counts))
    if repos is None:
        lines += ["", "（仓库列表没能读取）"]
    elif repos:
        lines += ["", "最近推送的仓库："]
        for repo in repos:
            tags = [tag for tag, on in (("fork", repo.get("fork")), ("已归档", repo.get("archived"))) if on]
            facts = [f"{repo.get('name')}（{'，'.join(tags)}）" if tags else str(repo.get("name"))]
            if language := repo.get("language"):
                facts.append(str(language))
            facts.append(f"★ {repo.get('stargazers_count', 0)}")
            if pushed := repo.get("pushed_at"):
                facts.append(f"推送于 {str(pushed)[:10]}")
            lines.append("- " + " · ".join(facts))
            if description := str(repo.get("description") or "").strip():
                lines.append(f"  {description}")
    if readme.strip():
        lines += ["", "主页 README：", "", readme.strip()]
    return "\n".join(lines)


async def _github_issue(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> AdapterResult:
    resp = await _github_api(client, f"/repos/{owner}/{repo}/issues/{number}")
    if resp.status_code in (404, 410):
        raise ToolError("GitHub 上没有这个 issue 或 PR（可能已删除），或仓库是私有的")
    if resp.status_code != 200:
        return None  # rate limited: the generic page is noisy but readable
    issue = resp.json()
    if issue.get("pull_request"):
        return await _github_pull(client, owner, repo, number)
    return Extracted(
        final_url=str(issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}"),
        title=f"{issue.get('title', '')} · Issue #{number} · {owner}/{repo}",
        text=format_github_issue(issue, await _github_comments(client, owner, repo, issue)),
        via="github",
    )


async def _github_pull(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> AdapterResult:
    resp = await _github_api(client, f"/repos/{owner}/{repo}/pulls/{number}")
    if resp.status_code == 404:
        return f"https://github.com/{owner}/{repo}/issues/{number}"  # an issue number: GitHub redirects too
    if resp.status_code != 200:
        return None
    pull = resp.json()
    comments = await _github_comments(client, owner, repo, pull)
    reviews = await _github_list(client, f"/repos/{owner}/{repo}/pulls/{number}/reviews", per_page=_GITHUB_PAGE_SIZE)
    return Extracted(
        final_url=str(pull.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"),
        title=f"{pull.get('title', '')} · PR #{number} · {owner}/{repo}",
        text=format_github_pull(pull, comments, reviews),
        via="github",
    )


async def _github_comments(client: httpx.AsyncClient, owner: str, repo: str, item: dict) -> list[dict] | None:
    """The conversation comments of an issue or PR (the first page)."""
    if not item.get("comments"):
        return []
    path = f"/repos/{owner}/{repo}/issues/{item.get('number')}/comments"
    return await _github_list(client, path, per_page=_GITHUB_PAGE_SIZE)


def format_github_issue(issue: dict, comments: list[dict] | None) -> str:
    if issue.get("state") == "closed":
        reason = _GITHUB_CLOSE_REASONS.get(str(issue.get("state_reason")))
        state = f"已关闭（{reason}）" if reason else "已关闭"
    else:
        state = "开放中"
    return _github_thread(issue, f"Issue #{issue.get('number')} · {state}", [], comments, [])


def format_github_pull(pull: dict, comments: list[dict] | None, reviews: list[dict] | None) -> str:
    """The PR's state, branches and size, its description and conversation: comments and review summaries, not
    the inline code review threads."""
    if pull.get("merged_at"):
        merger = f"，合并者 {_gh_login(pull['merged_by'])}" if pull.get("merged_by") else ""
        state = f"已合并（{_gh_time(pull['merged_at'])}{merger}）"
    elif pull.get("state") == "closed":
        state = "已关闭，未合并"
    else:
        state = "开放中"
    if pull.get("draft"):
        state += " · 草稿"
    head, base = pull.get("head") or {}, pull.get("base") or {}
    facts = [
        f"分支：{head.get('label', '?')} → {base.get('label', '?')}",
        f"改动：{pull.get('commits', 0)} 个提交 · {pull.get('changed_files', 0)} 个文件 · "
        f"+{pull.get('additions', 0)} −{pull.get('deletions', 0)}",
    ]
    inline = int(pull.get("review_comments") or 0)
    return _github_thread(pull, f"PR #{pull.get('number')} · {state}", facts, comments, reviews, inline)


def _github_thread(
    item: dict, status: str, facts: list[str], comments: list[dict] | None, reviews: list[dict] | None, inline: int = 0
) -> str:
    """An issue or PR: status, people, labels, body, then comments and reviews in time order."""
    author = _gh_login(item.get("user"))
    lines = [status, *_github_successors(item, [*(comments or ()), *(reviews or ())])]
    people = [f"作者：{author}", f"创建于 {_gh_time(item.get('created_at'))}"]
    if item.get("closed_at") and not item.get("merged_at"):
        people.append(f"关闭于 {_gh_time(item['closed_at'])}")
    lines += [" · ".join(people), *facts]
    if labels := [str(label.get("name")) for label in item.get("labels") or () if isinstance(label, dict)]:
        lines.append("标签：" + "、".join(labels))
    if assignees := [_gh_login(a) for a in item.get("assignees") or ()]:
        lines.append("指派给：" + "、".join(assignees))
    if milestone := (item.get("milestone") or {}).get("title"):
        lines.append(f"里程碑：{milestone}")
    body = _gh_markdown(item.get("body"))
    lines += ["", "正文：", "", body] if body else ["", "（没有正文）"]
    entries = [(str(c.get("created_at") or ""), _gh_who(c, author), _gh_comment_body(c)) for c in comments or ()]
    for review in reviews or ():
        state = _GITHUB_REVIEW_STATES.get(str(review.get("state")))
        text = _gh_markdown(review.get("body"))
        if state is None or (review.get("state") == "COMMENTED" and not text):
            continue  # pending reviews, and the empty wrappers of inline comments
        entries.append((str(review.get("submitted_at") or ""), f"{_gh_who(review, author)} · {state}", text))
    entries.sort(key=lambda entry: entry[0])
    notes = []
    if comments is None:
        notes.append("评论没能读取")
    elif (total := int(item.get("comments") or 0)) > len(comments):
        notes.append(f"共 {total} 条评论，只列出前 {len(comments)} 条")
    if inline:
        notes.append(f"另有 {inline} 条代码行内审查评论未列出")
    if entries or notes:
        lines += ["", f"评论（{len(entries)} 条{''.join(f'；{n}' for n in notes)}）："]
        for at, who, text in entries:
            lines += ["", f"【{who} · {_gh_time(at)}】", *([text] if text else [])]
    return "\n".join(lines)


def _github_successors(item: dict, replies: list[dict]) -> list[str]:
    """Notes for "Moved to #123" / "superseded by ..." in the body or replies, with the target's link."""
    repo_url = str(item.get("html_url") or "").rsplit("/", 2)[0]
    kind = "pull" if item.get("head") else "issues"
    found: dict[str, str] = {}
    for entry in (item, *replies):
        for match in _GITHUB_SUCCESSOR_RE.finditer(_gh_markdown(entry.get("body"))):
            target = match.group(1)
            link = f"{repo_url}/{kind}/{target[1:]}" if target.startswith("#") else target
            who = _gh_login(entry.get("user"))
            found.setdefault(link, f"提示：{who} 说「{match.group(0)}」，已转移或被取代：{link}")
    return list(found.values())[:3]


def _gh_markdown(body: object) -> str:
    """A Markdown body without template comments (``<!-- -->``) and CRLFs.

    Comments are only looked for up to the last ``-->``. Past it no ``<!--`` can close, and the lazy regex would scan
    from each of them to the end of the text (quadratic: seconds for a 64k body). An unclosed ``<!--`` stays as text.
    """
    text = str(body or "")
    end = text.rfind("-->")
    if end >= 0:
        text = _HTML_COMMENT_RE.sub("", text[: end + 3]) + text[end + 3 :]
    return text.replace("\r\n", "\n").strip()


def _gh_comment_body(comment: dict) -> str:
    if isinstance(minimized := comment.get("minimized"), dict):
        return f"（已被折叠：{minimized.get('reason') or '隐藏'}）"
    return _gh_markdown(comment.get("body"))


def _gh_who(entry: dict, author: str) -> str:
    """A comment's author with the badges GitHub shows (author, maintainer, bot)."""
    login = _gh_login(entry.get("user"))
    badges = (
        ("作者", login == author),
        ("维护者", entry.get("author_association") in _GITHUB_MAINTAINERS),
        ("机器人", (entry.get("user") or {}).get("type") == "Bot"),
    )
    tags = [tag for tag, on in badges if on]
    return f"{login}（{'、'.join(tags)}）" if tags else login


def _gh_login(user: object) -> str:
    return str(user.get("login") or "未知") if isinstance(user, dict) else "未知"


def _gh_time(stamp: object) -> str:
    """An API timestamp (ISO 8601, UTC) in China time."""
    if not stamp:
        return "未知时间"
    return f"{datetime.fromisoformat(str(stamp)).astimezone(_CN_TZ):%Y-%m-%d %H:%M}"


_TANGLED_BLOB_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")


@site_adapter("tangled.org", "tangled.sh")
async def _tangled_blob(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A code page on Tangled (git hosting on AT Protocol): the raw file, so lines and indentation survive."""
    match = _TANGLED_BLOB_RE.match(url.path)
    if match is None:
        return None
    owner, repo, ref, path = match.groups()
    raw = f"https://tangled.org/{owner}/{repo}/raw/{ref}/{path}"
    # Tangled turns away browser user agents without a browser TLS fingerprint, but serves honest clients.
    return await _raw_file(client, raw, f"{owner}/{repo}: {path}", "tangled", {"User-Agent": API_USER_AGENT})


def _fragment_text(html: str) -> str:
    """Markdown text of a small trusted-structure HTML fragment (comment bodies, post text)."""
    from markdownify import markdownify

    text = markdownify(html, heading_style="ATX", strip=["img"], escape_underscores=False, escape_asterisks=False)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _cn_time(epoch: int) -> str:
    return f"{datetime.fromtimestamp(epoch, UTC).astimezone(_CN_TZ):%Y-%m-%d %H:%M}"


@site_adapter("news.ycombinator.com")
async def _hacker_news(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    item_id = url.params.get("id", "")
    if url.path != "/item" or not item_id.isdigit():
        return None
    resp = await client.get(f"https://hn.algolia.com/api/v1/items/{item_id}", headers={"User-Agent": API_USER_AGENT})
    if resp.status_code == 404:
        raise ToolError("Hacker News 条目不存在")
    if resp.status_code != 200:
        return None
    item = resp.json()
    return Extracted(
        final_url=f"https://news.ycombinator.com/item?id={item_id}",
        title=item.get("title") or f"Hacker News 评论（{item.get('author') or '未知'}）",
        text=format_hacker_news(item),
        via="hackernews",
    )


def format_hacker_news(item: dict) -> str:
    """The item header, its text and the whole comment tree, indented by depth."""
    comments = _hn_count(item) - 1
    facts = [f"作者 {item.get('author') or '未知'}"]
    if item.get("points") is not None:
        facts.append(f"{item['points']} 分")
    facts.append(f"{comments} 条评论")
    if created := item.get("created_at_i"):
        facts.append(_cn_time(int(created)))
    lines = [" · ".join(facts)]
    if link := item.get("url"):
        lines.append(f"链接：{link}")
    if body := item.get("text"):
        lines += ["", _fragment_text(body)]
    if comments:
        lines += ["", "评论："]
        for child in item.get("children") or []:
            thread: list[str] = []
            _hn_comment_lines(child, 0, thread)
            if thread:
                lines += ["", *thread]  # blank lines only between threads: pagination strips paragraph indents
    return "\n".join(lines)


def _hn_count(node: dict) -> int:
    return 1 + sum(_hn_count(child) for child in node.get("children") or [])


def _hn_comment_lines(node: dict, depth: int, out: list[str]) -> None:
    children = node.get("children") or []
    text = node.get("text")
    if text is None and not children:
        return  # deleted leaf
    body = _fragment_text(text) if text is not None else "[已删除]"
    pad = "  " * depth
    head, *rest = [line for line in body.splitlines() if line.strip()] or [""]
    out.append(f"{pad}- {node.get('author') or '[已删除]'}：{head}")
    out.extend(f"{pad}  {line}" for line in rest)
    for child in children:
        _hn_comment_lines(child, depth + 1, out)


_TME_POST_RE = re.compile(r"^/(?:s/)?([A-Za-z0-9_]{3,32})/(\d+)/?$")


@site_adapter("t.me", "telegram.me", "www.t.me")
async def _telegram_post(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    match = _TME_POST_RE.match(url.path)
    if match is None:
        return None
    channel, post = match.groups()
    resp = await client.get(f"https://t.me/{channel}/{post}", params={"embed": "1", "mode": "tme"})
    if resp.status_code != 200:
        return None
    return parse_telegram_embed(resp.text, f"https://t.me/{channel}/{post}")


def parse_telegram_embed(html: str, final_url: str) -> Extracted | None:
    """Turn a t.me ``?embed=1`` widget page into the post text, or ``None`` if it has nothing to show."""
    from lxml import html as lxml_html

    root = lxml_html.fromstring(html)

    def first_text(xpath: str) -> str:
        nodes = root.xpath(xpath)
        return nodes[0].text_content().strip() if nodes else ""

    if root.xpath("//*[contains(@class, 'tgme_widget_message_error')]"):
        raise ToolError("Telegram 消息不存在，或频道不公开")
    owner = first_text("//*[contains(@class, 'tgme_widget_message_owner_name')]")
    lines = [f"频道：{owner}"] if owner else []
    if stamps := root.xpath("//time[@datetime]/@datetime"):
        lines.append(f"发布时间：{datetime.fromisoformat(stamps[0]).astimezone(_CN_TZ):%Y-%m-%d %H:%M}")
    if views := first_text("//*[contains(@class, 'tgme_widget_message_views')]"):
        lines.append(f"浏览：{views}")
    found = False
    if texts := root.xpath("//div[contains(@class, 'js-message_text')]"):
        for emoji in texts[0].xpath(".//i[contains(@class, 'emoji')]"):
            for inner in emoji.findall("b"):
                inner.drop_tag()
            emoji.drop_tag()
        lines += ["", _fragment_text(str(lxml_html.tostring(texts[0], encoding="unicode")))]
        found = True
    media = []
    if photos := root.xpath("//*[contains(@class, 'tgme_widget_message_photo_wrap')]"):
        media.append(f"图片 {len(photos)} 张")
    if root.xpath("//*[contains(@class, 'tgme_widget_message_video_player')]"):
        media.append("视频")
    if document := first_text("//*[contains(@class, 'tgme_widget_message_document_title')]"):
        media.append(f"文件「{document}」")
    if media:
        lines += ["", "附件：" + "、".join(media)]
        found = True
    preview_title = first_text("//*[contains(@class, 'link_preview_title')]")
    preview_desc = first_text("//*[contains(@class, 'link_preview_description')]")
    if preview_title or preview_desc:
        site = first_text("//*[contains(@class, 'link_preview_site_name')]")
        lines += ["", f"链接预览（{site}）：" if site else "链接预览：", *filter(None, (preview_title, preview_desc))]
        found = True
    if not found:
        return None
    return Extracted(final_url, f"{owner} 的 Telegram 消息" if owner else "Telegram 消息", "\n".join(lines), "telegram")


_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_ID_PATHS = frozenset({"shorts", "live", "embed"})
_YT_INITIAL_DATA = "var ytInitialData = "
_YT_MAX_CHAPTERS = 100
# Caption URLs from the web player need a proof-of-origin token; the Android app's player response carries URLs that
# work without one (no login, no API key). Datacenter IPs may still get a "confirm you're not a bot" answer.
_YT_PLAYER = "https://www.youtube.com/youtubei/v1/player"
_YT_ANDROID_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30, "hl": "zh-CN"}
_YT_ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"


@site_adapter("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be")
async def _youtube_video(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    vid = _youtube_video_id(url)
    if vid is None:
        return None
    resp = await client.get(f"https://www.youtube.com/watch?v={vid}&hl=zh-CN")
    if resp.status_code != 200:
        return None
    page = parse_youtube_watch(resp.text, f"https://www.youtube.com/watch?v={vid}")
    if page is not None:
        page.text += f"\n\n{await _youtube_transcript(client, vid)}"
    return page


async def _youtube_transcript(client: httpx.AsyncClient, vid: str) -> str:
    """The captions as a transcript, or a note saying why there are none."""
    player = await _optional_json(
        client.post(
            _YT_PLAYER,
            params={"prettyPrint": "false"},
            json={"context": {"client": _YT_ANDROID_CLIENT}, "videoId": vid},
            headers={"User-Agent": _YT_ANDROID_UA},
        ),
        "youtube player",
    )
    if not isinstance(player, dict):
        return _no_transcript("字幕读取失败")
    status = player.get("playabilityStatus") or {}
    if status.get("status") != "OK":
        return _no_transcript(f"YouTube 不给字幕（{status.get('reason') or status.get('status')}）")
    renderer = (player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    tracks = [t for t in renderer.get("captionTracks") or () if isinstance(t, dict) and t.get("baseUrl")]
    if not tracks:
        return _no_transcript("这个视频没有字幕")
    track = min(tracks, key=_youtube_track_rank)
    src = httpx.URL(str(track["baseUrl"])).copy_set_param("fmt", "json3")
    captions = await _optional_json(client.get(src), "youtube captions")
    events = captions.get("events") if isinstance(captions, dict) else None
    text = _transcript(
        (int(e.get("tStartMs") or 0) / 1000, "".join(str(s.get("utf8", "")) for s in e.get("segs") or ()))
        for e in events or ()
        if isinstance(e, dict)
    )
    if not text:
        return _no_transcript("字幕读取失败")
    return f"字幕（{_yt_text(track.get('name')) or track.get('languageCode')}）：\n\n{text}"


def _youtube_track_rank(track: dict) -> tuple[bool, bool, bool]:
    """Uploaded captions before auto-generated ones, then Chinese, then English."""
    lang = str(track.get("languageCode", ""))
    return track.get("kind") == "asr", not lang.startswith("zh"), not lang.startswith("en")


def _youtube_video_id(url: httpx.URL) -> str | None:
    segments = [s for s in url.path.split("/") if s]
    if url.host.lower() == "youtu.be":
        vid = segments[0] if segments else ""
    elif url.path == "/watch":
        vid = url.params.get("v", "")
    else:
        vid = segments[1] if len(segments) >= 2 and segments[0] in _YT_ID_PATHS else ""
    return vid if _YT_ID_RE.match(vid) else None


def parse_youtube_watch(html: str, final_url: str) -> Extracted | None:
    """Metadata, description and chapters from a watch page's ``ytInitialData``, or ``None`` without them.

    Datacenter IPs get the page but not the player data (a "confirm you're not a bot" check);
    ``ytInitialData`` still carries everything shown below the video.
    """
    start = html.find(_YT_INITIAL_DATA)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start + len(_YT_INITIAL_DATA))
    except ValueError:
        return None
    primary = next(_find_key(data, "videoPrimaryInfoRenderer"), None) or {}
    title = _yt_text(primary.get("title"))
    if not title:
        return None  # unavailable video, or a layout we do not know: the generic path takes over
    secondary = next(_find_key(data, "videoSecondaryInfoRenderer"), None) or {}
    owner = (secondary.get("owner") or {}).get("videoOwnerRenderer") or {}
    lines = []
    if channel := _yt_text(owner.get("title")):
        subscribers = _yt_text(owner.get("subscriberCountText"))
        lines.append(f"频道：{channel}（{subscribers}）" if subscribers else f"频道：{channel}")
    if date := _yt_text(primary.get("dateText")):
        lines.append(f"发布时间：{date}")
    if views := _yt_text(((primary.get("viewCount") or {}).get("videoViewCountRenderer") or {}).get("viewCount")):
        lines.append(f"播放：{views}")
    if description := _yt_text(secondary.get("attributedDescription")).strip():
        lines += ["", "简介：", description]
    # The same markers appear in several panels; keep the first occurrence of each.
    chapters = list(
        dict.fromkeys(
            f"{_yt_text(m.get('timeDescription'))} {_yt_text(m.get('title'))}".strip()
            for m in _find_key(data, "macroMarkersListItemRenderer")
            if isinstance(m, dict)
        )
    )
    if chapters:
        lines += ["", f"章节（共 {len(chapters)} 个）：", *chapters[:_YT_MAX_CHAPTERS]]
    return Extracted(final_url, title, "\n".join(lines), "youtube")


def _find_key(node: object, key: str) -> Iterator[Any]:
    """Every value stored under ``key`` anywhere in a JSON tree, depth first."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)


def _yt_text(node: object) -> str:
    """Text of a YouTube text object: ``simpleText``, ``runs`` or ``content`` (attributed text)."""
    if not isinstance(node, dict):
        return ""
    if isinstance(text := node.get("simpleText") or node.get("content"), str):
        return text
    return "".join(str(r.get("text", "")) for r in node.get("runs") or () if isinstance(r, dict))


# Atlassian Statuspage incidents, served on each status page's own domain (status.example.com/incidents/<id>).
_STATUSPAGE_INCIDENT_RE = re.compile(r"^/incidents/([a-z0-9]{12})/?$")
_STATUSPAGE_LABELS = {
    "investigating": "调查中",
    "identified": "已找到原因",
    "monitoring": "观察中",
    "resolved": "已解决",
    "postmortem": "事后分析",
    "scheduled": "已排期",
    "in_progress": "进行中",
    "verifying": "验证中",
    "completed": "已完成",
    "none": "无",
    "minor": "轻微",
    "major": "严重",
    "critical": "重大",
    "maintenance": "维护",
    "operational": "正常",
    "degraded_performance": "性能下降",
    "partial_outage": "部分中断",
    "major_outage": "大面积中断",
    "under_maintenance": "维护中",
}


@path_adapter(_STATUSPAGE_INCIDENT_RE)
async def _statuspage_incident(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """An incident from the status page's public API: without scripts the page shows only its subscribe dialog.
    Other sites with such paths answer the API with an error and take the generic path."""
    match = _STATUSPAGE_INCIDENT_RE.match(url.path)
    if match is None:
        return None
    incident_id = match.group(1)
    api = url.copy_with(path=f"/api/v2/incidents/{incident_id}.json", query=None)
    data = await _statuspage_json(client, api)
    incident = data.get("incident") if isinstance(data, dict) else None
    if not isinstance(incident, dict) or not incident.get("name"):
        return None
    postmortem = ""
    if incident.get("status") in ("resolved", "postmortem"):
        # Only the page's own JSON, not the v2 API, carries the postmortem.
        own = url.copy_with(path=f"/incidents/{incident_id}.json", query=None)
        full = await _statuspage_json(client, own)
        if isinstance(full, dict) and full.get("postmortem_published_at"):
            postmortem = str(full.get("postmortem_body") or "").strip()
    page = data.get("page")
    return Extracted(
        final_url=str(url.copy_with(path=f"/incidents/{incident_id}", query=None)),
        title=str(incident["name"]),
        text=format_statuspage_incident(page if isinstance(page, dict) else {}, incident, postmortem),
        via="statuspage",
    )


# Incident JSON is a few KB. This adapter runs on any host, so it reads no more than this.
_STATUSPAGE_MAX_BYTES = 2 * MB


async def _statuspage_json(client: httpx.AsyncClient, url: httpx.URL) -> Any:
    """``url`` decoded as JSON, reading at most :data:`_STATUSPAGE_MAX_BYTES`; ``None`` unless that is a whole
    JSON answer with status 200."""
    try:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            if resp.status_code != 200:
                return None
            data, capped = await read_capped(resp.aiter_bytes(), _STATUSPAGE_MAX_BYTES)
    except (httpx.HTTPError, ToolError) as exc:
        LOGGER.warning("statuspage request failed: {!r}", exc)
        return None
    if capped:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):  # not JSON, or nested too deep to decode
        return None


def format_statuspage_incident(page: dict, incident: dict, postmortem: str) -> str:
    """Status, impact, components, then the updates oldest first; times in UTC and labelled so."""
    lines = [f"状态页：{page['name']}"] if page.get("name") else []
    lines.append(f"状态：{_sp_label(incident.get('status'))} · 影响：{_sp_label(incident.get('impact'))}")
    span = [f"开始 {_utc(started)}"] if (started := incident.get("started_at") or incident.get("created_at")) else []
    if resolved := incident.get("resolved_at"):
        span.append(f"解决 {_utc(resolved)}")
    if span:
        lines.append("时间：" + " · ".join(span))
    if scheduled := incident.get("scheduled_for"):
        until = incident.get("scheduled_until")
        lines.append(f"计划维护：{_utc(scheduled)}" + (f" 至 {_utc(until)}" if until else ""))
    components = [str(c["name"]) for c in incident.get("components") or [] if isinstance(c, dict) and c.get("name")]
    lines.append("受影响组件：" + ("、".join(components) if components else "未标注"))
    updates = [u for u in incident.get("incident_updates") or [] if isinstance(u, dict)]
    updates.sort(key=lambda u: str(u.get("display_at") or u.get("created_at") or ""))
    if updates:
        lines += ["", f"更新记录（共 {len(updates)} 条，按时间先后）："]
    for update in updates:
        head = f"{_utc(update.get('display_at') or update.get('created_at'))} · {_sp_label(update.get('status'))}"
        changes = [
            f"{c.get('name')} {_sp_label(c.get('old_status'))}→{_sp_label(c.get('new_status'))}"
            for c in update.get("affected_components") or []
            if isinstance(c, dict) and c.get("old_status") != c.get("new_status")
        ]
        if changes:
            head += "（" + "；".join(changes) + "）"
        lines += ["", head, str(update.get("body") or "").strip()]
    if postmortem:
        lines += ["", "事后分析（Postmortem）：", "", postmortem]
    return "\n".join(lines)


def _sp_label(value: object) -> str:
    return _STATUSPAGE_LABELS.get(str(value), str(value))


def _utc(stamp: object) -> str:
    """An ISO 8601 timestamp as ``YYYY-MM-DD HH:MM UTC``."""
    try:
        return f"{datetime.fromisoformat(str(stamp)).astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    except ValueError:
        return str(stamp)


_UBUNTU_SECURITY = "https://ubuntu.com/security"
_UBUNTU_CVE_RE = re.compile(r"^/security/(?:cves/)?(cve-\d{4}-\d{4,})(?:\.json)?/?$", re.IGNORECASE)
# A package's status in a release, in the tracker's terms; "DNE" (not in that release) is left out.
_UBUNTU_STATUSES = {
    "released": "已修复",
    "pending": "修复中",
    "needed": "受影响，尚无修复",
    "deferred": "受影响，修复推迟",
    "needs-triage": "待评估",
    "ignored": "不修复",
    "not-affected": "不受影响",
}


@site_adapter("ubuntu.com", "www.ubuntu.com")
async def _ubuntu_cve(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A CVE of the Ubuntu security tracker from its JSON API: extraction loses the values of the page's priority
    and CVSS boxes, and the page's status table runs to many pages."""
    match = _UBUNTU_CVE_RE.match(url.path)
    if match is None:
        return None
    cve_id = match.group(1).upper()
    cve, releases = await asyncio.gather(
        _optional_json(client.get(f"{_UBUNTU_SECURITY}/cves/{cve_id}.json"), "ubuntu cve api"),
        _optional_json(client.get(f"{_UBUNTU_SECURITY}/releases.json"), "ubuntu releases api"),
    )
    if not isinstance(cve, dict) or not cve.get("id"):
        return None
    known = releases.get("releases") if isinstance(releases, dict) else None
    text = format_ubuntu_cve(cve, known if isinstance(known, list) else [])
    return Extracted(f"{_UBUNTU_SECURITY}/{cve_id}", f"{cve_id} | Ubuntu", text, "ubuntu")


def format_ubuntu_cve(cve: dict, releases: list) -> str:
    """Priority and CVSS scores, descriptions and notes, then each package's status per release (``releases``, the
    tracker's release list, gives version numbers and the newest-first order)."""
    stamps = (("发布", "published"), ("更新", "updated_at"))
    dates = [f"{label} {str(cve[key])[:10]}" for label, key in stamps if cve.get(key)]
    lines = [" · ".join(dates)] if dates else []
    lines.append(f"Ubuntu 优先级：{str(cve.get('priority') or '').capitalize() or '未评级'}")
    notes: list[str] = []
    for note in cve.get("notes") or []:
        body = str(note.get("note") or "").strip() if isinstance(note, dict) else ""
        if body.lower().startswith("priority reason:"):
            lines.append("优先级理由：" + " ".join(body.split(":", 1)[1].split()))
        elif body:
            notes.append(f"{note['author']}：{body}" if note.get("author") else body)
    impact = raw if isinstance(raw := cve.get("impact"), dict) else {}
    scores = [
        _cvss_line(metric.get(key))
        for metric, key in ((impact.get("baseMetricV3"), "cvssV3"), (impact.get("baseMetricV4"), "cvssV4"))
        if isinstance(metric, dict)
    ]
    lines += [s for s in scores if s] or ([f"CVSS 3 评分：{cve['cvss3']}"] if cve.get("cvss3") is not None else [])
    for label, key in (("描述", "description"), ("Ubuntu 补充说明", "ubuntu_description"), ("缓解措施", "mitigation")):
        if body := str(cve.get(key) or "").strip():
            lines += ["", f"{label}：", body]
    if notes:
        lines += ["", "备注：", *notes]
    if notices := [str(n) for n in cve.get("notices_ids") or [] if n]:
        lines += ["", "Ubuntu 安全通告：" + "、".join(notices)]
    order = {str(r.get("codename")): i for i, r in enumerate(releases) if isinstance(r, dict)}
    names = {str(r.get("codename")): _ubuntu_release(r) for r in releases if isinstance(r, dict)}
    rows = []
    for package in cve.get("packages") or []:
        statuses = [
            s for s in package.get("statuses") or [] if isinstance(s, dict) and s.get("status") not in (None, "DNE")
        ]
        statuses.sort(key=lambda s: order.get(str(s.get("release_codename")), len(order)))
        cells = []
        for status in statuses:
            codename = str(status.get("release_codename"))
            cell = f"{names.get(codename, codename)} {_UBUNTU_STATUSES.get(status['status'], status['status'])}"
            detail = str(status.get("description") or "").strip()
            cells.append(f"{cell}（{detail}）" if detail else cell)
        if cells:
            rows.append(f"{package.get('name')}：" + "；".join(cells))
    if rows:
        lines += ["", f"各版本状态（{len(rows)} 个软件包，新版本在前）：", *rows]
    if references := [str(r) for r in cve.get("references") or [] if r]:
        lines += ["", "参考链接：", *references]
    return "\n".join(lines)


def _cvss_line(cvss: object) -> str | None:
    if not isinstance(cvss, dict) or cvss.get("baseScore") is None:
        return None
    line = f"CVSS {cvss.get('version') or ''} 评分：{cvss['baseScore']}"
    if severity := str(cvss.get("baseSeverity") or "").capitalize():
        line += f" {severity}"
    return f"{line}（{cvss['vectorString']}）" if cvss.get("vectorString") else line


def _ubuntu_release(release: dict) -> str:
    """``24.04 LTS noble``; just the codename for ``upstream``."""
    codename = str(release.get("codename"))
    if not (version := release.get("version")):
        return codename
    return f"{version} LTS {codename}" if release.get("lts") else f"{version} {codename}"


_HUGGING_FACE = "https://huggingface.co"
# First path segments of huggingface.co pages that are not a user or an organization.
_HF_RESERVED = frozenset(
    "api blog chat collections docs enterprise join learn login models new organizations papers posts pricing "
    "settings spaces tasks".split()
)
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HF_MAX_FILES = 40


@site_adapter("huggingface.co", "www.huggingface.co", "hf.co")
async def _hugging_face(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A model or dataset: facts from the API, then its card (the README). The page opens with the "use this
    model" widgets or the dataset viewer, which extraction takes for the content."""
    segments = [s for s in url.path.split("/") if s]
    kind = "models"
    if segments[:1] == ["datasets"]:
        kind, segments = "datasets", segments[1:]
    if len(segments) != 2 or segments[0] in _HF_RESERVED:
        return None
    repo = "/".join(segments)
    page = f"{_HUGGING_FACE}/{repo}" if kind == "models" else f"{_HUGGING_FACE}/datasets/{repo}"
    info, readme = await asyncio.gather(
        _optional_json(client.get(f"{_HUGGING_FACE}/api/{kind}/{repo}"), "hugging face api"),
        _optional_get(client.get(f"{page}/raw/main/README.md"), "hugging face readme"),
    )
    if not isinstance(info, dict):
        return None  # private, missing, or the API is down: the generic path decides
    card = _FRONT_MATTER_RE.sub("", readme.text).strip() if readme is not None else ""
    if not card:  # gated repositories show their card only on the page
        html = await _optional_get(client.get(page), "hugging face page")
        card = _hf_page_card(html.text) if html is not None else ""
    title = f"{info.get('id') or repo} · Hugging Face"
    return Extracted(page, title, format_hugging_face(kind, info, card), "huggingface")


def format_hugging_face(kind: str, info: dict, card: str) -> str:
    noun = "数据集" if kind == "datasets" else "模型"
    card_data = raw if isinstance(raw := info.get("cardData"), dict) else {}
    lines = [f"{noun}：{info.get('id')}"]
    facts = [f"任务 {info['pipeline_tag']}"] if info.get("pipeline_tag") else []
    if library := info.get("library_name"):
        facts.append(f"库 {library}")
    if isinstance(safetensors := info.get("safetensors"), dict) and safetensors.get("total"):
        facts.append(f"参数量 {_hf_params(int(safetensors['total']))}")
    gguf = raw if isinstance(raw := info.get("gguf"), dict) else {}
    if gguf.get("architecture"):
        facts.append(f"GGUF 架构 {gguf['architecture']}")
    if gguf.get("context_length"):
        facts.append(f"上下文长度 {gguf['context_length']}")
    if facts:
        lines.append(" · ".join(facts))
    if license_ := card_data.get("license"):
        name = card_data.get("license_name")
        lines.append(f"许可证：{license_}（{name}）" if name else f"许可证：{license_}")
    if base := card_data.get("base_model"):
        bases = [base] if isinstance(base, str) else [str(b) for b in base]
        relation = card_data.get("base_model_relation")
        lines.append("基础模型：" + "、".join(bases) + (f"（{relation}）" if relation else ""))
    stats = [f"近 30 天下载 {_cn_count(int(info['downloads']))}"] if info.get("downloads") is not None else []
    if info.get("likes") is not None:
        stats.append(f"点赞 {info['likes']}")
    if modified := info.get("lastModified"):
        stats.append(f"更新于 {str(modified)[:10]}")
    if stats:
        lines.append(" · ".join(stats))
    if info.get("gated"):
        lines.append("（受限仓库：要登录并同意作者的条款才能下载文件）")
    files = [str(s["rfilename"]) for s in info.get("siblings") or [] if isinstance(s, dict) and s.get("rfilename")]
    if files and kind == "datasets":
        lines.append(f"文件：{len(files)} 个")  # data shards: the names say nothing
    elif files:
        more = "……" if len(files) > _HF_MAX_FILES else ""
        lines.append(f"文件（{len(files)} 个）：" + "、".join(files[:_HF_MAX_FILES]) + more)
    lines += ["", f"{noun}卡（README）：", "", card] if card else ["", f"（没有{noun}卡）"]
    return "\n".join(lines)


def _hf_params(total: int) -> str:
    for unit, size in (("T", 10**12), ("B", 10**9), ("M", 10**6)):
        if total >= size:
            return f"{total / size:.1f}{unit}"
    return str(total)


def _hf_page_card(html: str) -> str:
    """The model or dataset card as rendered on the repository page, in Markdown."""
    from lxml import html as lxml_html
    from lxml.etree import ParserError

    try:
        root = lxml_html.fromstring(html)
    except (ParserError, ValueError):
        return ""
    cards = root.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' model-card-content ')"
        " or contains(concat(' ', normalize-space(@class), ' '), ' hf-sanitized ')]"
    )
    if not cards:
        return ""
    card = max(cards, key=lambda el: len(el.text_content()))
    return _fragment_text(str(lxml_html.tostring(card, encoding="unicode")))


_MSN_CONTENT_RE = re.compile(r"/(?:ar|vi|gm|ss)-([A-Za-z0-9]{6,})/?$")
_MSN_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.IGNORECASE)


@site_adapter("www.msn.com", "msn.com")
async def _msn_article(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """An MSN article (or video / slideshow summary) from the content API its own pages load: the page is a script
    shell that shows a cookie-consent wall first."""
    match = _MSN_CONTENT_RE.search(url.path)
    if match is None:
        return None
    first = url.path.split("/")[1]
    locale = first.lower() if _MSN_LOCALE_RE.match(first) else "en-us"
    api = f"https://assets.msn.com/content/view/v2/Detail/{locale}/{match.group(1)}"
    resp = await client.get(api, headers={"Accept": "application/json"})
    if resp.status_code in (404, 410):
        raise ToolError("MSN 上的这篇内容不存在或已下架")
    try:
        data = resp.json() if resp.status_code == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("title") or not (text := format_msn(data)):
        return None  # the generic path gets the consent wall and says so
    return Extracted(str(url.copy_with(query=None)), str(data["title"]), text, "msn")


def format_msn(data: dict) -> str:
    """Source, time and the article text (the abstract when there is no body); empty when there is neither."""
    body = _fragment_text(str(data.get("body") or ""))
    abstract = str(data.get("abstract") or "").strip()
    if not body and not abstract:
        return ""
    provider = raw if isinstance(raw := data.get("provider"), dict) else {}
    authors = [str(a["name"]) for a in data.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    source = [f"来源：{provider['name']}"] if provider.get("name") else []
    if authors:
        source.append("作者：" + "、".join(authors))
    lines = [" · ".join(source)] if source else []
    if published := data.get("publishedDateTime"):
        lines.append(f"发布时间：{_gh_time(published)}")
    if original := data.get("sourceHref"):
        lines.append(f"原文：{original}")
    lines += ["", body] if body else ["", f"摘要：{abstract}"]
    return "\n".join(lines)

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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# Official JSON APIs (Bilibili in particular) answer 412 to browser user agents without a full
# cookie fingerprint, but serve honest non-browser clients normally.
API_USER_AGENT = "yggdrasil-bot/0.1"
_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": _ACCEPT_LANGUAGE,
}
_MB = 1024 * 1024
MAX_HTML_BYTES = 5 * _MB
MAX_DOC_BYTES = 20 * _MB
# Extracted text shorter than this is treated as a JS shell worth rendering in the browser.
_MIN_TEXT_CHARS = 200
# Walls are short pages; keyword hits in long articles are ignored.
_WALL_MAX_CHARS = 2000
_WALL_KEYWORDS = (
    "验证码",
    "安全验证",
    "人机验证",
    "滑动验证",
    "拖动滑块",
    "完成验证",
    "请登录",
    "登录后查看",
    "登录后继续",
    "环境异常",
    "访问异常",
    "请求存在异常",
    "限制本次访问",
    "异常流量",
    "访问过于频繁",
    "captcha",
    "verify you are human",
    "are you a robot",
    "you're not a robot",
    "prove your humanity",
    "unusual traffic",
    "just a moment...",
    "attention required",
    "checking your browser",
    "enable javascript and cookies",
    # Google's EU cookie-consent interstitial (YouTube channel pages etc.); the browser shares our IP and gets it too.
    "before you continue to",
    "继续使用 youtube 前须知",
)
# Anti-bot block and error pages that answer with a normal-looking page (often HTTP 200). Same short-page rule as
# the walls; unlike a wall, a real browser may still get through, so these escalate to the browser tier.
_BLOCK_KEYWORDS = (
    "blocked by network security",  # reddit
    "you've been blocked",
    "you have been blocked",
    "access denied",
    "request blocked",
    "pardon our interruption",  # Akamai
    "something went wrong",  # generic SPA error shells (e.g. behind Cloudflare)
    "请求已被拦截",  # CN WAFs (e.g. coolapk's HTTP 567 page)
    "安全策略拦截",
)
# Walls that are JavaScript challenges rather than captchas or logins: a better browser may pass them, so they
# escalate like block pages. Captcha / login walls never do (no captcha fighting; CN walls are IP reputation).
_JS_CHALLENGES = frozenset({"just a moment...", "checking your browser", "enable javascript and cookies"})
_BROWSER_RETRY_STATUSES = frozenset({401, 403, 429, 503})
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
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# Lines that are only stray punctuation (left over from stripped inline widgets).
_JUNK_LINE_RE = re.compile(r"^[ \t]*[，。、：；！？,.:;!?·…]{1,3}[ \t]*(?:\n|$)", re.MULTILINE)
_CN_TZ = timezone(timedelta(hours=8))


@dataclass(slots=True)
class PageResult:
    url: str
    final_url: str
    title: str
    text: str
    page: int
    pages: int
    truncated: bool
    """More content exists beyond this page (later pages, or the source hit a size cap)."""
    via: str
    """``http`` / ``browser`` / ``document:<kind>`` / adapter name (``bilibili``, ``github``, ``hackernews``, ...)."""
    images: list[str] = field(default_factory=list)
    """Image URLs from the source (adapters only); the caller shows them with page 1."""


@dataclass(slots=True)
class Extracted:
    """Whole-document result of one fetch, before pagination."""

    final_url: str
    title: str
    text: str
    via: str
    capped: bool = False
    images: list[str] = field(default_factory=list)
    """Image URLs that belong to the content (third-party: download them through the SSRF guard)."""


class Unreadable(ToolError):
    """The page could not be read from here (wall, block page, HTTP error, network failure, nothing rendered);
    unlike other tool errors (not a web page, file too large, gone per an official API), a remote fetcher may
    still get it."""


type RemoteFetch = Callable[[str], Awaitable[Extracted | None]]
"""Fetch a URL from elsewhere (``yggdrasil.tools.remote_fetch``); ``None`` when it has nothing either."""


type AdapterResult = Extracted | str | None
"""An adapter returns the content, a rewritten URL to restart with, or ``None`` to use the generic path."""
type AdapterFn = Callable[[httpx.AsyncClient, httpx.URL], Awaitable[AdapterResult]]

_ADAPTERS: dict[str, AdapterFn] = {}
type _CacheKey = tuple[str, bool, bool]
"""(url, browser tier on, remote tier on)."""
_cache: OrderedDict[_CacheKey, tuple[float, Extracted]] = OrderedDict()


def site_adapter(*hosts: str) -> Callable[[AdapterFn], AdapterFn]:
    """Register an adapter for exact host names (lower-case)."""

    def register(fn: AdapterFn) -> AdapterFn:
        for host in hosts:
            _ADAPTERS[host] = fn
        return fn

    return register


async def read_url(
    url: str,
    *,
    max_chars: int = 16000,
    page: int = 1,
    browser_url: str | None = None,
    remote: RemoteFetch | None = None,
    timeout: float = 20,  # noqa: ASYNC109 - per-request network timeout, not a cancel scope
) -> PageResult:
    """Read ``url`` and return page ``page`` of its text split into ``max_chars`` pages."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if page < 1:
        raise ToolError("页码从 1 开始")
    target = await check_url(url)
    key = (target, browser_url is not None, remote is not None)
    extracted = _cache_get(key)
    with TRACER.start_as_current_span("tool.read_url") as span:
        if extracted is None:
            extracted = await _read(target, browser_url, remote, timeout)
            _cache_put(key, extracted)
        span.set_attribute("ygg.read_url.via", extracted.via)
    pages = paginate(extracted.text, max_chars)
    if page > len(pages):
        raise ToolError(f"页码超出范围，共 {len(pages)} 页")
    return PageResult(
        url=target,
        final_url=extracted.final_url,
        title=extracted.title,
        text=pages[page - 1],
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
    if remote is not None and generic and len(local.text) < _MIN_TEXT_CHARS:
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
        or _is_challenge(fetched.title, fetched.text)
    ):
        return None  # the remote copy is a wall / block page too
    return fetched


def paginate(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into pages of at most ``max_chars``, on paragraph, then line, then hard boundaries."""
    blocks: list[str] = []
    for para in _PARA_SPLIT_RE.split(text.strip()):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            blocks.append(para)
        else:
            blocks += _pack(_hard_lines(para, max_chars), "\n", max_chars)
    return _pack(blocks, "\n\n", max_chars) or [""]


def _hard_lines(para: str, limit: int) -> Iterable[str]:
    for line in para.split("\n"):
        for start in range(0, len(line), limit):
            yield line[start : start + limit]


def _pack(parts: Iterable[str], sep: str, limit: int) -> list[str]:
    out: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{sep}{part}" if current else part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                out.append(current)
            current = part
    if current:
        out.append(current)
    return out


def detect_wall(title: str, text: str) -> str | None:
    """Return the matched keyword if the page looks like a captcha / verification / login wall."""
    return _match_short_page(_WALL_KEYWORDS, title, text)


def detect_block(title: str, text: str) -> str | None:
    """Return the matched keyword if the page looks like an anti-bot block or error page."""
    return _match_short_page(_BLOCK_KEYWORDS, title, text)


def _match_short_page(keywords: tuple[str, ...], title: str, text: str) -> str | None:
    lowered_title = title.lower()
    for keyword in keywords:
        if keyword in lowered_title:
            return keyword
    if len(text) <= _WALL_MAX_CHARS:
        lowered = text.lower()
        for keyword in keywords:
            if keyword in lowered:
                return keyword
    return None


def _raise_if_wall(extracted: Extracted) -> None:
    if keyword := detect_wall(extracted.title, extracted.text):
        raise Unreadable(f"该页面需要人机验证或登录（检测到「{keyword}」），无法读取")


def _raise_if_blocked(extracted: Extracted) -> None:
    if keyword := detect_block(extracted.title, extracted.text):
        raise Unreadable(f"网站拒绝了自动访问（检测到「{keyword}」），无法读取")


def extract_html(html: str, url: str) -> tuple[str, str]:
    """Return ``(title, markdown-ish text)`` of the main content of an HTML document."""
    import trafilatura

    text = (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_links=False,
            include_images=False,
            include_comments=False,
            favor_recall=True,
            deduplicate=True,
        )
        or ""
    ).strip()
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title or "") if meta is not None else ""
    if len(text) < _MIN_TEXT_CHARS:
        fallback_title, fallback = _readability(html)
        if len(fallback) > len(text):
            text = fallback
        title = title or fallback_title
    return title.strip(), _JUNK_LINE_RE.sub("", text).strip()


def _readability(html: str) -> tuple[str, str]:
    from markdownify import markdownify
    from readability import Document

    try:
        doc = Document(html)
        summary = doc.summary(html_partial=True)
        title = doc.short_title()
    except Exception:
        return "", ""
    text = markdownify(summary, heading_style="ATX", strip=["a", "img"])
    return title or "", re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- fetch pipeline


async def _fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
    try:
        async with guarded_client(timeout=timeout_s, headers=_HEADERS) as client:
            for _ in range(3):
                adapter = _ADAPTERS.get(httpx.URL(url).host.lower())
                if adapter is None:
                    break
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
    except (httpx.TimeoutException, CurlTimeout) as exc:
        raise Unreadable("网页加载超时") from exc
    except (httpx.HTTPError, CurlRequestError) as exc:
        raise Unreadable("网页无法访问") from exc


@dataclass(slots=True)
class _Body:
    filename: str
    is_doc: bool
    limit: int
    data: bytes
    capped: bool
    charset: str | None


async def _fetch_generic(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
    # curl_cffi owns the browser header set (UA, client hints) so it matches the impersonated TLS fingerprint.
    async with impersonated_stream(url, timeout=timeout_s, headers={"Accept-Language": _ACCEPT_LANGUAGE}) as resp:
        final_url, status = resp.url, resp.status_code
        body = await _download(resp) if status < 400 else None
    if body is None:
        if status in _BROWSER_RETRY_STATUSES and browser_url:
            return await _render_or_raise(final_url, browser_url, timeout_s, None)
        raise Unreadable(f"网页返回错误（HTTP {status}）")

    if body.is_doc or body.data[:1024].lstrip().startswith(b"%PDF-"):
        if body.capped:
            raise ToolError(f"文件超过 {body.limit // _MB} MB，无法读取")
        doc = await asyncio.to_thread(parse_document, body.data, body.filename)
        if not doc.text.strip():
            if doc.scanned_pages:
                raise ToolError("这是扫描版 PDF，没有文字层，无法直接读取")
            raise ToolError("文档中没有可读取的文字")
        return Extracted(final_url, body.filename, doc.text, f"document:{doc.kind}", capped=doc.truncated)

    title, text = await asyncio.to_thread(extract_html, decode_html(body.data, body.charset), final_url)
    extracted = Extracted(final_url, title, text, "http", capped=body.capped)
    challenged = _is_challenge(title, text)
    thin = len(text) < _MIN_TEXT_CHARS and detect_wall(title, text) is None
    if browser_url and (challenged or thin):
        return await _render_or_raise(final_url, browser_url, timeout_s, None if challenged else extracted)
    _raise_if_wall(extracted)
    _raise_if_blocked(extracted)
    if not text:
        raise Unreadable("页面没有可读取的正文")
    return extracted


def _is_challenge(title: str, text: str) -> bool:
    """A block page or JS challenge: worth another (better) browser, unlike captcha and login walls."""
    return detect_wall(title, text) in _JS_CHALLENGES or detect_block(title, text) is not None


async def _download(resp: GuardedResponse) -> _Body:
    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    filename = _filename(resp.headers, resp.url)
    if ctype.startswith("image/"):
        raise ToolError("这是图片链接，不是网页，请用看图工具查看")
    if ctype.startswith(("audio/", "video/")):
        raise ToolError("这是音视频文件链接，无法读取内容")
    is_doc = ctype.startswith(_DOC_TYPES) or (ctype in _HTML_TYPES and _ext(filename) in _DOC_EXTS)
    if not is_doc and ctype not in _HTML_TYPES:
        raise ToolError(f"不支持的内容类型：{ctype}")
    limit = MAX_DOC_BYTES if is_doc else MAX_HTML_BYTES
    declared = resp.headers.get("content-length", "")
    if is_doc and declared.isdigit() and int(declared) > limit:
        raise ToolError(f"文件超过 {limit // _MB} MB，无法读取")
    data, capped = await _read_capped(resp.chunks, limit)
    return _Body(filename, is_doc, limit, data, capped, resp.charset)


async def _read_capped(chunks: AsyncIterator[bytes], limit: int) -> tuple[bytes, bool]:
    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            return bytes(buf[:limit]), True
    return bytes(buf), False


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
        rendered = Extracted(final_url, title, text, via)
        if text and not _is_challenge(title, text):
            break
        if detect_wall(title, text) not in (None, *_JS_CHALLENGES):
            break  # captcha / login wall: another engine will not help
    if rendered is not None:
        _raise_if_wall(rendered)
        _raise_if_blocked(rendered)
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


def _cache_get(key: _CacheKey) -> Extracted | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, extracted = entry
    if time.monotonic() - stored_at > _CACHE_TTL:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return extracted


def _cache_put(key: _CacheKey, extracted: Extracted) -> None:
    _cache[key] = (time.monotonic(), extracted)
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
    return Extracted(
        final_url=f"https://www.bilibili.com/video/{bvid}",
        title=str(data.get("title", "")),
        text=format_bilibili_video(data),
        via="bilibili",
    )


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


@site_adapter("github.com", "www.github.com")
async def _github(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    segments = [s for s in url.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() in _GITHUB_RESERVED:
        return None
    owner, repo = segments[0], segments[1].removesuffix(".git")
    rest = segments[2:]
    if len(rest) >= 3 and rest[0] == "blob":
        return await _github_blob(client, owner, repo, rest[1], "/".join(rest[2:]))
    if rest and not (rest[0] == "tree" and len(rest) == 2):
        return None  # issues, PRs, sub-trees: generic HTML extraction handles them
    ref = rest[1] if rest else None
    return await _github_repo(client, owner, repo, ref)


async def _github_blob(client: httpx.AsyncClient, owner: str, repo: str, ref: str, path: str) -> AdapterResult:
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    resp = await client.get(raw)
    if resp.status_code != 200 or len(resp.content) > MAX_HTML_BYTES:
        return None
    doc = await asyncio.to_thread(parse_document, resp.content, path.rsplit("/", 1)[-1])
    return Extracted(raw, f"{owner}/{repo}: {path}", doc.text, "github", capped=doc.truncated)


async def _github_repo(client: httpx.AsyncClient, owner: str, repo: str, ref: str | None) -> AdapterResult:
    api = f"https://api.github.com/repos/{owner}/{repo}"
    info_resp = await client.get(api, headers={"Accept": "application/vnd.github+json"})
    if info_resp.status_code == 404:
        raise ToolError("GitHub 仓库不存在或是私有仓库")
    info = info_resp.json() if info_resp.status_code == 200 else {}
    readme = ""
    raw = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/README.md")
    if raw.status_code == 200:
        readme = raw.text
    else:
        params = {"ref": ref} if ref else None
        api_readme = await client.get(f"{api}/readme", params=params, headers={"Accept": "application/vnd.github.raw"})
        if api_readme.status_code == 200:
            readme = api_readme.text
    if not info and not readme:
        return None
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
    if readme:
        lines += ["", "README：", "", readme.strip()]
    return Extracted(f"https://github.com/{full_name}", full_name, "\n".join(lines), "github")


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


@site_adapter("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be")
async def _youtube_video(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    vid = _youtube_video_id(url)
    if vid is None:
        return None
    resp = await client.get(f"https://www.youtube.com/watch?v={vid}&hl=zh-CN")
    if resp.status_code != 200:
        return None
    return parse_youtube_watch(resp.text, f"https://www.youtube.com/watch?v={vid}")


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

    Datacenter IPs get the page but not the player data (a "confirm you're not a bot" check), so duration and
    captions are out of reach; ``ytInitialData`` still carries everything shown below the video.
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
    lines += ["", "（只有页面信息，没有字幕，无法得知视频里具体说了什么）"]
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

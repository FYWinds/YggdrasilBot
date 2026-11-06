"""The ``read_url`` flow: its tiers in order (site adapter, plain HTTP, browser service, remote fetcher), the
deadline of each read, and the page cache."""

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
from curl_cffi.requests.exceptions import RequestException as CurlRequestError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from yggdrasil.core.log import LOGGER
from yggdrasil.core.telemetry import TRACER
from yggdrasil.tools.docs import decode_html, parse_document
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import GuardedResponse, check_url, guarded_client, impersonated_stream
from yggdrasil.tools.web.adapters import adapter_for
from yggdrasil.tools.web.blocks import JS_CHALLENGES, detect_wall, is_challenge, raise_if_blocked, raise_if_wall
from yggdrasil.tools.web.extract import MIN_TEXT_CHARS, extract_html, json_text
from yggdrasil.tools.web.model import (
    MAX_HTML_BYTES,
    MB,
    Extracted,
    ImageLink,
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
_GONE_STATUSES = frozenset({404, 410})
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


type _CacheKey = tuple[str, str, bool, bool]
"""(url, fragment, browser tier on, remote tier on)."""
_cache: OrderedDict[_CacheKey, tuple[float, Extracted, Anchor | None]] = OrderedDict()


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
                adapter = adapter_for(httpx.URL(url))
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
        if status in _GONE_STATUSES:  # the server says so itself: no other tier would find the page
            raise ToolError(f"网页不存在或已删除（HTTP {status}）")
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
        raise ImageLink(str(resp.url))
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

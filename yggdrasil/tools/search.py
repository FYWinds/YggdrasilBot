"""Web search: one function per backend kind, plus the provider pool behind the ``web_search`` tool.

Providers are admin-configured (dashboard), so their URLs are trusted and get no SSRF guard. The ``x`` kind holds
the X API token for ``search_x`` / ``read_x`` (``yggdrasil.tools.x``) and is not part of the ``web_search`` pool.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeIs

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import SEARCH_REQUESTS
from yggdrasil.core.telemetry import TRACER
from yggdrasil.kv import redis_client
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.errors import ToolError

WEB_KINDS = ("searxng", "exa", "deepseek")
KINDS = (*WEB_KINDS, "x")
DEFAULT_BASE_URLS = {"exa": "https://api.exa.ai", "deepseek": "https://api.deepseek.com", "x": "https://api.x.com"}
TIME_RANGES = frozenset({"day", "week", "month", "year"})
_TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
_SNIPPET_CHARS = 300
# Exa highlights and DeepSeek summaries are written for models: worth more room than SearXNG's engine snippets.
_LONG_SNIPPET_CHARS = 600
_DEEPSEEK_MAX_QUERY = 128
_TIMEOUT = 15.0


@dataclass(slots=True, frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    engine: str


class SearchBackendError(Exception):
    """One provider failed (network, HTTP status, bad payload); the pool moves on to the next."""


type Reserve = Callable[[SearchProviderInfo], Awaitable[bool]]


async def web_search(
    query: str,
    providers: Sequence[SearchProviderInfo],
    *,
    n: int = 6,
    time_range: str | None = None,
    client: httpx.AsyncClient | None = None,
    reserve: Reserve | None = None,
) -> list[SearchHit]:
    """Try enabled providers in priority order; the first one that returns hits wins.

    A provider is skipped when its monthly limit is used up, and passed over when it fails or
    returns nothing. No hits anywhere is an empty list; every provider failing is a ToolError.
    """
    query = query.strip()
    if not query:
        raise ToolError("搜索关键词不能为空")
    if time_range is not None and time_range not in TIME_RANGES:
        raise ToolError("time_range 只能是 day、week、month、year 之一")
    candidates = [p for p in providers if p.enabled and p.kind in WEB_KINDS]
    if not candidates:
        raise ToolError("没有可用的搜索源")
    reserve = reserve or reserve_quota
    any_answered = False
    with TRACER.start_as_current_span("tool.web_search") as span:
        for p in candidates:
            attrs = {"provider": p.name, "kind": p.kind}
            if not await reserve(p):
                SEARCH_REQUESTS.add(1, {**attrs, "outcome": "quota"})
                continue
            try:
                hits = await search_with(p, query, n=n, time_range=time_range, client=client)
            except SearchBackendError as e:
                LOGGER.warning("search provider {} failed: {}", p.name, e)
                SEARCH_REQUESTS.add(1, {**attrs, "outcome": "error"})
                continue
            any_answered = True
            SEARCH_REQUESTS.add(1, {**attrs, "outcome": "ok" if hits else "empty"})
            if hits:
                span.set_attribute("ygg.search.provider", p.name)
                return hits
    if any_answered:
        return []
    raise ToolError("搜索服务暂时不可用，请稍后再试")


async def search_with(
    p: SearchProviderInfo,
    query: str,
    *,
    n: int,
    time_range: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[SearchHit]:
    """Query a single provider (no quota bookkeeping). An ``x`` provider answers with X News stories."""
    if p.kind == "x":
        return await _x_news_hits(p, query, n, client)
    if p.kind == "searxng":
        request = _searxng_request(p, query, time_range)
        parse = parse_searxng
    elif p.kind == "exa":
        request = _exa_request(p, query, n, time_range)
        parse = parse_exa
    elif p.kind == "deepseek":
        request = _deepseek_request(p, query, n, time_range)
        parse = parse_deepseek
    else:
        raise SearchBackendError(f"unknown kind {p.kind!r}")
    request.extensions["timeout"] = httpx.Timeout(_TIMEOUT).as_dict()
    try:
        if client is None:
            async with httpx.AsyncClient() as own:
                response = await own.send(request)
        else:
            response = await client.send(request)
    except httpx.HTTPError as e:
        raise SearchBackendError(f"{type(e).__name__}: {e}") from e
    if response.status_code != 200:
        raise SearchBackendError(f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as e:
        raise SearchBackendError("response is not JSON") from e
    return parse(payload, n)


async def _x_news_hits(p: SearchProviderInfo, query: str, n: int, client: httpx.AsyncClient | None) -> list[SearchHit]:
    """X News stories of the last day as hits (the console's provider test)."""
    from yggdrasil.tools import x

    try:
        stories = await x.search_news(p, query, hours=24, n=n, client=client)
    except ToolError as e:
        raise SearchBackendError(str(e)) from e
    return [
        SearchHit(
            s.name,
            f"https://x.com/i/status/{s.post_ids[0]}" if s.post_ids else "",
            _clip(s.summary, _LONG_SNIPPET_CHARS),
            "x",
        )
        for s in stories
    ]


# --- quota -------------------------------------------------------------------


def _usage_key(provider_id: int, now: datetime | None = None) -> str:
    return f"ygg:search:usage:{provider_id}:{(now or datetime.now(UTC)):%Y-%m}"


async def reserve_quota(p: SearchProviderInfo) -> bool:
    """Count one request against this month's limit; False (and nothing counted) when it is used up.

    Free tiers reset on the first of the month, so the counter is per UTC calendar month.
    Failed requests still count: a slight overcount keeps us inside the free tier.
    """
    key = _usage_key(p.id)
    r = redis_client()
    if p.monthly_limit is not None and int(await r.get(key) or 0) >= p.monthly_limit:
        return False
    if await r.incr(key) == 1:
        await r.expire(key, int(timedelta(days=40).total_seconds()))
    return True


async def monthly_usage(provider_id: int) -> int:
    return int(await redis_client().get(_usage_key(provider_id)) or 0)


# --- SearXNG -----------------------------------------------------------------


def _searxng_request(p: SearchProviderInfo, query: str, time_range: str | None) -> httpx.Request:
    params = {"q": query, "format": "json", "language": str(p.params.get("language") or "zh-CN"), "pageno": "1"}
    if time_range:
        params["time_range"] = time_range
    return httpx.Request("GET", p.base_url + "/search", params=params)


def parse_searxng(payload: object, n: int) -> list[SearchHit]:
    """At most ``n`` unique hits (order preserved) from a SearXNG JSON payload."""
    results = payload.get("results") if isinstance(payload, dict) else None
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results if isinstance(results, list) else ():
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not _is_web_url(url) or (key := _dedupe_key(url)) in seen:
            continue
        seen.add(key)
        engines = item.get("engines")
        engine = item.get("engine") or (engines[0] if isinstance(engines, list) and engines else "")
        hits.append(
            SearchHit(
                title=_clean(item.get("title")) or url,
                url=url,
                snippet=_clip(_clean(item.get("content")), _SNIPPET_CHARS),
                engine=str(engine),
            )
        )
        if len(hits) >= n:
            break
    return hits


# --- Exa ---------------------------------------------------------------------


def _exa_request(p: SearchProviderInfo, query: str, n: int, time_range: str | None) -> httpx.Request:
    body: dict[str, object] = {
        "query": query,
        "type": str(p.params.get("type") or "auto"),
        "numResults": n,  # up to 10 cost the same
        "contents": {"highlights": True},
    }
    if time_range:
        start = datetime.now(UTC) - timedelta(days=_TIME_RANGE_DAYS[time_range])
        body["startPublishedDate"] = start.isoformat(timespec="seconds").replace("+00:00", "Z")
    headers = {"x-api-key": p.api_key or ""}
    return httpx.Request("POST", p.base_url + "/search", json=body, headers=headers)


def parse_exa(payload: object, n: int) -> list[SearchHit]:
    results = payload.get("results") if isinstance(payload, dict) else None
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results if isinstance(results, list) else ():
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not _is_web_url(url) or (key := _dedupe_key(url)) in seen:
            continue
        seen.add(key)
        highlights = item.get("highlights")
        text = " … ".join(_clean(h) for h in highlights if isinstance(h, str)) if isinstance(highlights, list) else ""
        published = item.get("publishedDate")
        if isinstance(published, str) and len(published) >= 10:
            text = f"[{published[:10]}] {text}".strip()
        hits.append(
            SearchHit(
                title=_clean(item.get("title")) or url,
                url=url,
                snippet=_clip(text, _LONG_SNIPPET_CHARS),
                engine="exa",
            )
        )
        if len(hits) >= n:
            break
    return hits


# --- DeepSeek ----------------------------------------------------------------


def _deepseek_request(p: SearchProviderInfo, query: str, n: int, time_range: str | None) -> httpx.Request:
    suffix = ""
    if time_range:
        start = datetime.now(UTC) - timedelta(days=_TIME_RANGE_DAYS[time_range])
        suffix = f" after:{start:%Y-%m-%d}"  # its query syntax; there is no date field
    body = {"query": query[: _DEEPSEEK_MAX_QUERY - len(suffix)] + suffix, "count": n}
    headers = {"Authorization": f"Bearer {p.api_key or ''}"}
    return httpx.Request("POST", p.base_url + "/v1/tools/search", json=body, headers=headers)


def parse_deepseek(payload: object, n: int) -> list[SearchHit]:
    results = payload.get("results") if isinstance(payload, dict) else None
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results if isinstance(results, list) else ():
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not _is_web_url(url) or (key := _dedupe_key(url)) in seen:
            continue
        seen.add(key)
        text = _clean(item.get("summary")) or _clean(item.get("snippet"))
        date = item.get("date")
        if isinstance(date, str) and len(date) >= 10:
            text = f"[{date[:10]}] {text}".strip()
        hits.append(
            SearchHit(
                title=_clean(item.get("title")) or url,
                url=url,
                snippet=_clip(text, _LONG_SNIPPET_CHARS),
                engine="deepseek",
            )
        )
        if len(hits) >= n:
            break
    return hits


# --- helpers -----------------------------------------------------------------


def _is_web_url(url: object) -> TypeIs[str]:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def _dedupe_key(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return url
    host = parsed.host.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    query = parsed.query.decode("ascii", errors="replace")
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"

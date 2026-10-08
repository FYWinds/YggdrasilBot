"""Web search through the in-cluster SearXNG JSON API."""

from dataclasses import dataclass

import httpx

from yggdrasil.tools.errors import ToolError

TIME_RANGES = frozenset({"day", "week", "month", "year"})
_SNIPPET_CHARS = 300


@dataclass(slots=True, frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    engine: str


async def web_search(
    query: str,
    *,
    searxng_url: str,
    n: int = 6,
    language: str = "zh-CN",
    time_range: str | None = None,
    timeout: float = 15,  # noqa: ASYNC109 - per-request HTTP timeout, not a cancel scope
    client: httpx.AsyncClient | None = None,
) -> list[SearchHit]:
    """Return up to ``n`` hits, deduplicated by URL. SearXNG is trusted (cluster-internal), so no SSRF guard."""
    query = query.strip()
    if not query:
        raise ToolError("搜索关键词不能为空")
    if time_range is not None and time_range not in TIME_RANGES:
        raise ToolError("time_range 只能是 day、week、month、year 之一")
    params = {"q": query, "format": "json", "language": language, "pageno": "1"}
    if time_range:
        params["time_range"] = time_range
    endpoint = searxng_url.rstrip("/") + "/search"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as own:
                response = await own.get(endpoint, params=params)
        else:
            response = await client.get(endpoint, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError("搜索服务暂时不可用，请稍后再试") from exc
    return parse_results(payload, n)


def parse_results(payload: object, n: int) -> list[SearchHit]:
    """Turn a SearXNG JSON payload into at most ``n`` unique hits (order preserved)."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        key = _dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        engines = item.get("engines")
        engine = item.get("engine") or (engines[0] if isinstance(engines, list) and engines else "")
        hits.append(
            SearchHit(
                title=_clean(item.get("title")) or url,
                url=url,
                snippet=_clip(_clean(item.get("content"))),
                engine=str(engine),
            )
        )
        if len(hits) >= n:
            break
    return hits


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


def _clip(text: str) -> str:
    return text if len(text) <= _SNIPPET_CHARS else text[: _SNIPPET_CHARS - 1] + "…"

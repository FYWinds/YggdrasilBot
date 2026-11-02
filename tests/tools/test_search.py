import json
from typing import Any

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.search import SearchHit, web_search

SEARXNG_PAYLOAD = {
    "query": "原神",
    "results": [
        {
            "url": "https://ys.mihoyo.com/",
            "title": " 原神 官网 ",
            "content": "开放世界\n冒险游戏",
            "engine": "bing",
            "engines": ["bing", "baidu"],
        },
        # Same page modulo scheme / www / trailing slash: deduplicated.
        {"url": "http://www.ys.mihoyo.com", "title": "dup", "content": "", "engines": ["sogou"]},
        {"url": "javascript:alert(1)", "title": "bad", "content": ""},
        {
            "url": "https://zh.wikipedia.org/wiki/原神",
            "title": "原神 - 维基百科",
            "content": "x" * 500,
            "engines": ["360search"],
        },
        {"url": "https://example.com/a?id=1", "title": "A1", "content": "", "engine": "quark"},
        {"url": "https://example.com/a?id=2", "title": "A2", "content": "", "engine": "quark"},
    ],
}

EXA_PAYLOAD = {
    "results": [
        {
            "title": "Python 3.15 有什么新变化",
            "url": "https://docs.python.org/zh-cn/3.15/whatsnew/3.15.html",
            "publishedDate": "2026-10-09T00:00:00.000Z",
            "highlights": ["本文介绍了 Python 3.15\n的新增特性。", "lazy import ..."],
        },
        {"title": "", "url": "https://github.com/python/cpython", "highlights": ["y" * 1000]},
        {"title": "no url"},
    ],
    "costDollars": {"total": 0.007},
}


def _provider(
    kind: str, pid: int = 1, *, enabled: bool = True, limit: int | None = None, **params: Any
) -> SearchProviderInfo:
    base = {"searxng": "http://searxng:8080", "exa": "https://api.exa.ai", "deepseek": "https://api.deepseek.com"}
    return SearchProviderInfo(
        pid, f"{kind}{pid}", kind, base[kind], None if kind == "searxng" else "k", enabled, pid, limit, params
    )


def _hosts(routes: Routes) -> list[str]:
    return [r.url.host for r in routes.seen]


async def _always(_: SearchProviderInfo) -> bool:
    return True


async def test_searxng_parses_and_dedupes() -> None:
    routes = Routes({"searxng": httpx.Response(200, json=SEARXNG_PAYLOAD)})
    async with routes.client() as client:
        hits = await web_search("原神", [_provider("searxng")], time_range="week", client=client, reserve=_always)

    params = routes.seen[0].url.params
    assert routes.seen[0].url.path == "/search"
    assert (params["format"], params["language"], params["time_range"]) == ("json", "zh-CN", "week")
    assert [h.url for h in hits] == [
        "https://ys.mihoyo.com/",
        "https://zh.wikipedia.org/wiki/原神",
        "https://example.com/a?id=1",
        "https://example.com/a?id=2",
    ]
    assert hits[0] == SearchHit(
        title="原神 官网", url="https://ys.mihoyo.com/", snippet="开放世界 冒险游戏", engine="bing"
    )
    assert hits[1].engine == "360search"
    assert len(hits[1].snippet) == 300
    assert hits[1].snippet.endswith("…")


async def test_exa_request_and_parse() -> None:
    routes = Routes({"api.exa.ai": httpx.Response(200, json=EXA_PAYLOAD)})
    async with routes.client() as client:
        hits = await web_search(
            "Python 3.15", [_provider("exa")], n=4, time_range="day", client=client, reserve=_always
        )

    request = routes.seen[0]
    body = json.loads(request.content)
    assert (request.method, request.url.path, request.headers["x-api-key"]) == ("POST", "/search", "k")
    assert (body["type"], body["numResults"], body["contents"]) == ("auto", 4, {"highlights": True})
    assert body["startPublishedDate"].endswith("Z")
    assert [h.url for h in hits] == [
        "https://docs.python.org/zh-cn/3.15/whatsnew/3.15.html",
        "https://github.com/python/cpython",
    ]
    assert hits[0].snippet == "[2026-10-09] 本文介绍了 Python 3.15 的新增特性。 … lazy import ..."
    assert hits[1].title == "https://github.com/python/cpython"  # falls back to the URL
    assert len(hits[1].snippet) == 600


async def test_deepseek_request_and_parse() -> None:
    payload = {
        "results": [
            {
                "title": "DeepSeek V4 发布",
                "url": "https://example.com/v4",
                "snippet": "short",
                "summary": "DeepSeek 发布了 V4\n模型。",
                "date": "2026-06-01T00:00:00+00:00",
            },
            {"title": "dup", "url": "http://www.example.com/v4/", "summary": "same page"},
            {"title": "", "url": "https://example.org/x", "snippet": "only a snippet", "date": None},
        ]
    }
    routes = Routes({"api.deepseek.com": httpx.Response(200, json=payload)})
    providers = [_provider("deepseek")]
    async with routes.client() as client:
        hits = await web_search("q" * 200, providers, n=5, time_range="week", client=client, reserve=_always)

    request = routes.seen[0]
    body = json.loads(request.content)
    assert (request.url.path, request.headers["authorization"], body["count"]) == ("/v1/tools/search", "Bearer k", 5)
    # The date filter is query syntax, and the whole query stays within the API's 128 characters.
    query, _, date = body["query"].partition(" after:")
    assert (query, len(body["query"])) == ("q" * (128 - len(" after:") - len(date)), 128)
    assert date.startswith("20")
    assert [(h.title, h.snippet) for h in hits] == [
        ("DeepSeek V4 发布", "[2026-06-01] DeepSeek 发布了 V4 模型。"),
        ("https://example.org/x", "only a snippet"),
    ]


async def test_pool_falls_through_failure_and_empty_in_priority_order() -> None:
    routes = Routes(
        {
            "api.exa.ai": httpx.Response(429, text="rate limited"),
            "empty": httpx.Response(200, json={"results": []}),
            "searxng": httpx.Response(200, json=SEARXNG_PAYLOAD),
        }
    )
    empty = SearchProviderInfo(2, "empty", "searxng", "http://empty", None, True, 2, None, {})
    async with routes.client() as client:
        hits = await web_search(
            "q", [_provider("exa", 1), empty, _provider("searxng", 3)], n=1, client=client, reserve=_always
        )
    assert _hosts(routes) == ["api.exa.ai", "empty", "searxng"]
    assert hits[0].url == "https://ys.mihoyo.com/"


async def test_pool_skips_disabled_and_exhausted_providers() -> None:
    routes = Routes({"searxng": httpx.Response(200, json=SEARXNG_PAYLOAD)})

    async def reserve(p: SearchProviderInfo) -> bool:
        return p.monthly_limit is None

    providers = [_provider("exa", 1, limit=1300), _provider("exa", 2, enabled=False), _provider("searxng", 3)]
    async with routes.client() as client:
        await web_search("q", providers, client=client, reserve=reserve)
    assert _hosts(routes) == ["searxng"]


async def test_no_results_anywhere_is_empty_but_all_failing_is_an_error() -> None:
    async with Routes({"searxng": httpx.Response(200, json={"results": None})}).client() as client:
        assert await web_search("q", [_provider("searxng")], client=client, reserve=_always) == []

    failing = Routes({"searxng": httpx.Response(200, text="<html>"), "api.exa.ai": httpx.Response(200, text="<html>")})
    async with failing.client() as client:
        with pytest.raises(ToolError):
            await web_search("q", [_provider("searxng"), _provider("exa", 2)], client=client, reserve=_always)


@pytest.mark.parametrize(
    ("query", "time_range", "providers"),
    [("  ", None, [_provider("searxng")]), ("q", "decade", [_provider("searxng")]), ("q", None, [])],
)
async def test_invalid_arguments_are_rejected_before_request(
    query: str, time_range: str | None, providers: list[SearchProviderInfo]
) -> None:
    routes = Routes()
    async with routes.client() as client:
        with pytest.raises(ToolError):
            await web_search(query, providers, time_range=time_range, client=client, reserve=_always)
    assert routes.seen == []

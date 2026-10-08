from collections.abc import Callable

import httpx
import pytest

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.search import SearchHit, web_search

PAYLOAD = {
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


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_parses_and_dedupes_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PAYLOAD)

    async with _client(handler) as client:
        hits = await web_search("原神", searxng_url="http://searxng:8080/", time_range="week", client=client)

    params = seen[0].url.params
    assert seen[0].url.path == "/search"
    assert params["format"] == "json"
    assert params["language"] == "zh-CN"
    assert params["time_range"] == "week"
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


async def test_respects_n() -> None:
    async with _client(lambda r: httpx.Response(200, json=PAYLOAD)) as client:
        hits = await web_search("原神", searxng_url="http://searxng:8080", n=2, client=client)
    assert len(hits) == 2


async def test_missing_results_is_empty() -> None:
    async with _client(lambda r: httpx.Response(200, json={"results": None})) as client:
        assert await web_search("q", searxng_url="http://searxng:8080", client=client) == []


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, text="down"), httpx.Response(200, text="<html>not json</html>")],
)
async def test_backend_failure_is_tool_error(response: httpx.Response) -> None:
    async with _client(lambda r: response) as client:
        with pytest.raises(ToolError):
            await web_search("q", searxng_url="http://searxng:8080", client=client)


@pytest.mark.parametrize(("query", "time_range"), [("  ", None), ("q", "decade")])
async def test_invalid_arguments_are_rejected_before_request(query: str, time_range: str | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    async with _client(handler) as client:
        with pytest.raises(ToolError):
            await web_search(query, searxng_url="http://searxng:8080", time_range=time_range, client=client)

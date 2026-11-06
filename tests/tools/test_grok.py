import dataclasses
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.support.agent import run_ctx
from tests.support.routes import Routes
from yggdrasil import runtime
from yggdrasil.agent.tools import web as web_tools
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools import grok, x
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.search import web_search

GROK = SearchProviderInfo(7, "grok", "grok", "https://api.x.ai", "xai-key", True, 1, 300, {"model": "grok-test"})
XAPI = SearchProviderInfo(9, "x", "x", "https://api.x.com", "tok", True, 2, 300, {})
POST_A = "https://x.com/alice/status/1"
POST_B = "https://x.com/bob/status/2"


def _response(text: str, urls: list[str], *, ticks: int = 650_000_000, posts: int = 11) -> dict[str, Any]:
    annotations = [{"type": "url_citation", "url": u, "start_index": 0, "end_index": 1, "title": "1"} for u in urls]
    return {
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "custom_tool_call", "name": "x_keyword_search", "input": "{}"},
            {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": annotations}]},
        ],
        "usage": {"cost_in_usd_ticks": ticks, "server_side_tool_usage_details": {"x_posts_fetched": posts}},
    }


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    grok._cache.clear()
    x._cache.clear()


async def test_x_search_asks_grok_with_x_search_tool_and_parses_citations() -> None:
    text = f"@alice 说发布了 {POST_A}"
    routes = Routes({"api.x.ai": httpx.Response(200, json=_response(text, [POST_A, POST_B, POST_A]))})
    async with routes.client() as client:
        answer = await grok.x_search(GROK, "LongCat-2.5 发布了吗", hours=48, client=client)
        again = await grok.x_search(GROK, "LongCat-2.5 发布了吗", hours=48, client=client)

    assert again is answer  # cached
    assert len(routes.seen) == 1
    request = routes.seen[0]
    sent = json.loads(request.content)
    assert (request.url.path, request.headers["authorization"]) == ("/v1/responses", "Bearer xai-key")
    assert (sent["model"], sent["reasoning"]) == ("grok-test", {"effort": "low"})
    assert [m["role"] for m in sent["input"]] == ["system", "user"]
    assert sent["input"][1]["content"] == "LongCat-2.5 发布了吗"
    assert sent["tools"][0]["type"] == "x_search"
    assert len(sent["tools"][0]["from_date"]) == 10  # YYYY-MM-DD
    assert (answer.text, answer.citations) == (text, [POST_A, POST_B])
    assert (answer.cost_usd, answer.posts_fetched) == (0.065, 11)
    # Posts already linked in the text are not listed again.
    formatted = grok.format_answer(answer)
    assert formatted.count(POST_A) == 1
    assert f"其他搜到的帖子：\n1. {POST_B}" in formatted


async def test_x_search_reasoning_effort_comes_from_provider_params() -> None:
    high = dataclasses.replace(GROK, params={"model": "grok-test", "reasoning_effort": "high"})
    routes = Routes({"api.x.ai": httpx.Response(200, json=_response("t", []))})
    async with routes.client() as client:
        await grok.x_search(GROK, "q", hours=24, client=client)
        await grok.x_search(high, "q", hours=24, client=client)  # not served from the "low" answer's cache entry

    assert [json.loads(r.content)["reasoning"] for r in routes.seen] == [{"effort": "low"}, {"effort": "high"}]


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, {"error": {"message": "Incorrect API key"}}, "HTTP 401：Incorrect API key"),
        (429, {"error": "rate limited"}, "HTTP 429：rate limited"),
        (200, {"output": []}, "无法识别"),
    ],
)
async def test_x_search_failures_are_tool_errors(status: int, body: dict[str, Any], message: str) -> None:
    routes = Routes({"api.x.ai": httpx.Response(status, json=body)})
    async with routes.client() as client:
        with pytest.raises(ToolError, match=message):
            await grok.x_search(GROK, "q", hours=24, client=client)


NEWS = {"data": [{"id": "1", "name": "API 兜底的新闻", "summary": "摘要", "cluster_posts_results": [{"post_id": "3"}]}]}


async def _search_x(
    routes: Routes, monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot], *providers: Any
) -> str:
    routes.install(monkeypatch)
    snapshot(search_providers=list(providers))
    result = await web_tools.search_x(run_ctx(), "某个问题")
    assert isinstance(result, str)
    return result


async def test_search_x_uses_grok_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Callable[..., runtime.Snapshot],
) -> None:
    routes = Routes({"api.x.ai": httpx.Response(200, json=_response(f"总结 {POST_A}", [POST_A]))})
    result = await _search_x(routes, monkeypatch, snapshot, GROK, XAPI)
    assert "Grok 在 X 上搜索后写的总结" in result
    assert POST_A in result
    assert [r.url.host for r in routes.seen] == ["api.x.ai"]


async def test_search_x_falls_back_to_x_api_when_grok_fails(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Callable[..., runtime.Snapshot],
) -> None:
    routes = Routes({"api.x.ai": httpx.Response(500, json={}), "api.x.com": httpx.Response(200, json=NEWS)})
    result = await _search_x(routes, monkeypatch, snapshot, GROK, XAPI)
    assert "API 兜底的新闻" in result
    assert [r.url.host for r in routes.seen] == ["api.x.ai", "api.x.com"]


async def test_search_x_reports_grok_error_without_x_api(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Callable[..., runtime.Snapshot],
) -> None:
    routes = Routes({"api.x.ai": httpx.Response(500, json={})})
    result = await _search_x(routes, monkeypatch, snapshot, GROK)
    assert result.startswith("[工具错误] Grok 出错（HTTP 500")


async def test_web_search_skips_grok_provider() -> None:
    searxng = SearchProviderInfo(2, "s", "searxng", "http://searxng:8080", None, True, 2, None, {})
    routes = Routes({"searxng": httpx.Response(200, json={"results": [{"url": "https://example.com/", "title": "t"}]})})

    async def always(_: SearchProviderInfo) -> bool:
        return True

    async with routes.client() as client:
        hits = await web_search("q", [GROK, searxng], client=client, reserve=always)
    assert [r.url.host for r in routes.seen] == ["searxng"]
    assert [h.url for h in hits] == ["https://example.com/"]

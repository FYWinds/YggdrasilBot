import json

import httpx

from tests.support.routes import Routes
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.remote_fetch import deepseek_fetch, remote_fetcher


def _provider(pid: int, kind: str = "deepseek", *, enabled: bool = True, key: str | None = "k") -> SearchProviderInfo:
    return SearchProviderInfo(pid, f"p{pid}", kind, f"https://api{pid}.example.com", key, enabled, pid, None, {})


async def test_fetch_unescapes_markdown_and_keeps_the_requested_url() -> None:
    content = "\\# 标题\n\n时间 2026\\-10\\-08 18\\:26，价格 5\\.0\\! 路径 C\\:\\\\tmp，代码 a\\_b"
    routes = Routes({"api1.example.com": httpx.Response(200, json={"title": "标题", "content": content})})
    async with routes.client() as client:
        page = await deepseek_fetch("https://example.com/a", _provider(1), client=client)

    [request] = routes.seen
    assert (request.url.path, request.headers["authorization"]) == ("/v1/tools/fetch", "Bearer k")
    assert json.loads(request.content) == {"url": "https://example.com/a"}
    assert page is not None
    assert (page.final_url, page.title, page.via) == ("https://example.com/a", "标题", "remote:deepseek")
    assert page.text == "# 标题\n\n时间 2026-10-08 18:26，价格 5.0! 路径 C:\\tmp，代码 a_b"


async def test_fetch_without_usable_content_is_none() -> None:
    answers = iter(
        [
            httpx.Response(404, json={"error": {"message": "failed to fetch URL"}}),
            httpx.Response(200, json={"content": " "}),
        ]
    )
    async with Routes({"api1.example.com": lambda _r: next(answers)}).client() as client:
        assert await deepseek_fetch("https://example.com/a", _provider(1), client=client) is None
        assert await deepseek_fetch("https://example.com/a", _provider(1), client=client) is None


def test_fetcher_needs_an_enabled_deepseek_provider_with_a_key() -> None:
    assert remote_fetcher([]) is None
    assert remote_fetcher([_provider(1, "exa"), _provider(2, enabled=False), _provider(3, key=None)]) is None
    assert remote_fetcher([_provider(1, "exa"), _provider(2)]) is not None

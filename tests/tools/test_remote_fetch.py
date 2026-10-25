import json

import httpx

from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.remote_fetch import deepseek_fetch, remote_fetcher


def _provider(pid: int, kind: str = "deepseek", *, enabled: bool = True, key: str | None = "k") -> SearchProviderInfo:
    return SearchProviderInfo(pid, f"p{pid}", kind, f"https://api{pid}.example.com", key, enabled, pid, None, {})


async def test_fetch_unescapes_markdown_and_keeps_the_requested_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = "\\# 标题\n\n时间 2026\\-10\\-08 18\\:26，价格 5\\.0\\! 路径 C\\:\\\\tmp，代码 a\\_b"
        return httpx.Response(200, json={"title": "标题", "content": content})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await deepseek_fetch("https://example.com/a", _provider(1), client=client)

    assert (seen[0].url.path, seen[0].headers["authorization"]) == ("/v1/tools/fetch", "Bearer k")
    assert json.loads(seen[0].content) == {"url": "https://example.com/a"}
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
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: next(answers))) as client:
        assert await deepseek_fetch("https://example.com/a", _provider(1), client=client) is None
        assert await deepseek_fetch("https://example.com/a", _provider(1), client=client) is None


def test_fetcher_needs_an_enabled_deepseek_provider_with_a_key() -> None:
    assert remote_fetcher([]) is None
    assert remote_fetcher([_provider(1, "exa"), _provider(2, enabled=False), _provider(3, key=None)]) is None
    assert remote_fetcher([_provider(1, "exa"), _provider(2)]) is not None

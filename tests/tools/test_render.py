"""The render_* tools as the model calls them: the render service turns Mermaid, Markdown or HTML into pages that ride
in the reply's forward, within the reply's image slots."""

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.routes import Routes
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.images import MAX_IMAGES, render_html, render_markdown, render_mermaid
from yggdrasil.config import APP_CONFIG
from yggdrasil.tools import render as renderer

BASE = "http://render.example.com:8080"
RENDER = f"{BASE}/render"
DONE = "会附在合并转发里，正文不用再重复图里的内容。"


def _ok(pages: int, warnings: list[str] | None = None) -> httpx.Response:
    images = [{"png": base64.b64encode(f"page{i}".encode()).decode(), "width": 10, "height": 10} for i in range(pages)]
    return httpx.Response(200, json={"images": images, "warnings": warnings or []})


def _ctx(images: int = 0) -> RunContext[Deps]:
    """A chat run whose reply already carries ``images`` pictures."""
    ctx = run_ctx()
    ctx.deps.images.extend((b"x", "") for _ in range(images))
    return ctx


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Routes:
    """The configured render service; ``answers[RENDER]`` is its answer (one page unless a test says otherwise)."""
    monkeypatch.setattr(APP_CONFIG, "render_url", BASE + "/")
    routes = Routes({RENDER: _ok(1)})
    routes.install(monkeypatch)
    return routes


def _asked(service: Routes) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in service.seen]


async def test_pages_arrive_in_order_in_the_remaining_slots_with_captions(service: Routes) -> None:
    service.answers[RENDER] = _ok(3)
    ctx = _ctx(images=1)
    result = await render_markdown(ctx, "# long", " 对比 ")
    assert _asked(service) == [{"kind": "markdown", "source": "# long", "max_images": MAX_IMAGES - 1}]
    assert ctx.deps.images[1:] == [(b"page0", "对比"), (b"page1", "对比（2/3）"), (b"page2", "对比（3/3）")]
    assert result == f"已生成图 2–4（共 3 页）（对比），{DONE}"


async def test_a_single_page_without_caption(service: Routes) -> None:
    ctx = _ctx()
    result = await render_mermaid(ctx, "graph TD\nA-->B")
    assert _asked(service) == [{"kind": "mermaid", "source": "graph TD\nA-->B", "max_images": 1}]
    assert ctx.deps.images == [(b"page0", "")]
    assert result == f"已生成图 1（无标题），{DONE}"


async def test_pages_without_caption_stay_empty(service: Routes) -> None:
    service.answers[RENDER] = _ok(2)
    ctx = _ctx()
    await render_markdown(ctx, "# long")
    assert [caption for _, caption in ctx.deps.images] == ["", ""]


async def test_service_warnings_reach_the_model(service: Routes) -> None:
    service.answers[RENDER] = _ok(1, ["第 3 个 mermaid 代码块语法错误，已按代码显示"])
    result = str(await render_markdown(_ctx(), "# x"))
    assert result.endswith("改好内容后重新生成）：\n- 第 3 个 mermaid 代码块语法错误，已按代码显示")


async def test_an_unreachable_service_is_unavailable(service: Routes) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    service.answers[RENDER] = refuse
    assert "渲染服务暂时不可用" in str(await render_html(_ctx(), "<b>x</b>"))


@pytest.mark.parametrize("source", ["  \n", "x" * (renderer.MAX_SOURCE_CHARS + 1)])
async def test_sources_are_checked_before_the_service(service: Routes, source: str) -> None:
    assert str(await render_markdown(_ctx(), source)).startswith("[工具错误]")
    assert service.seen == []


async def test_a_full_reply_refuses_without_asking_the_service(service: Routes) -> None:
    result = await render_html(_ctx(images=MAX_IMAGES), "<b>x</b>")
    assert result == f"[工具错误] 这次回复已经有 {MAX_IMAGES} 张图了"
    assert service.seen == []


async def test_without_a_render_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(APP_CONFIG, "render_url", None)
    assert await render_mermaid(_ctx(), "graph TD\nA-->B") == "[工具错误] 没有配置渲染服务"


async def test_concurrent_renders_never_overfill_the_reply(service: Routes) -> None:
    both = asyncio.Event()

    async def together(request: httpx.Request) -> httpx.Response:
        if len(service.seen) == 2:
            both.set()
        async with asyncio.timeout(5):
            await both.wait()  # two renders in flight at once
        return _ok(json.loads(request.content)["max_images"])

    service.answers[RENDER] = together
    ctx = _ctx(images=2)
    results = await asyncio.gather(
        render_mermaid(ctx, "graph TD\nA-->B", "a"),
        render_markdown(ctx, "# long", "b"),
        render_html(ctx, "<b>x</b>", "c"),
    )
    assert len(ctx.deps.images) == MAX_IMAGES
    assert [body["max_images"] for body in _asked(service)] == [1, 1]
    assert "同时进行" in str(results[2])
    assert ctx.deps.images_reserved == 0


async def test_a_failed_render_releases_its_slots(service: Routes) -> None:
    service.answers[RENDER] = httpx.Response(422, json={"error": "太长"})
    ctx = _ctx()
    assert await render_markdown(ctx, "# x") == "[工具错误] 太长"
    assert ctx.deps.images_reserved == 0
    service.answers[RENDER] = _ok(1)
    await render_markdown(ctx, "# x")
    assert _asked(service)[-1]["max_images"] == MAX_IMAGES  # every slot is free again

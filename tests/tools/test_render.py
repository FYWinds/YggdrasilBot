import asyncio
import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.agent import tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.tools import MAX_IMAGES, _attach_render
from yggdrasil.settings import Settings
from yggdrasil.tools import render as renderer
from yggdrasil.tools.errors import ToolError

BASE = "http://render.test:8080"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _ok(pages: int, warnings: list[str] | None = None) -> httpx.Response:
    images = [{"png": base64.b64encode(f"page{i}".encode()).decode(), "width": 10, "height": 10} for i in range(pages)]
    return httpx.Response(200, json={"images": images, "warnings": warnings or []})


def _client(handler: Any, seen: list[dict[str, Any]] | None = None) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def _ctx(images: int = 0) -> RunContext[Deps]:
    deps = Deps(cast(Any, None), 1, 2, Settings(), Handles(), "chat")
    deps.images.extend((b"x", "") for _ in range(images))
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


@dataclass
class _Service:
    handler: Callable[[httpx.Request], httpx.Response] = lambda _r: _ok(1)
    seen: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch):
    state = _Service()
    monkeypatch.setattr(tools.APP_CONFIG, "render_url", BASE)
    real = renderer.render

    async def fake(*args: Any, **kwargs: Any) -> tuple[list[bytes], list[str]]:
        await asyncio.sleep(0.01)  # stay in flight so concurrent tool calls overlap
        async with _client(lambda r: state.handler(r), state.seen) as client:
            return await real(*args, client=client, **kwargs)

    monkeypatch.setattr(renderer, "render", fake)
    return state


async def test_render_decodes_pages_in_order():
    seen: list[dict[str, Any]] = []
    async with _client(lambda _r: _ok(3, ["外部图片已移除"]), seen) as client:
        pngs, warnings = await renderer.render("markdown", "# hi", base_url=BASE + "/", max_images=3, client=client)
    assert pngs == [b"page0", b"page1", b"page2"]
    assert warnings == ["外部图片已移除"]
    assert seen == [{"kind": "markdown", "source": "# hi", "max_images": 3}]


@pytest.mark.parametrize("status", [422, 503])
async def test_render_passes_service_errors_through(status: int):
    async with _client(lambda _r: httpx.Response(status, json={"error": "第 2 行语法错误"})) as client:
        with pytest.raises(ToolError, match="第 2 行语法错误"):
            await renderer.render("mermaid", "graph TD\nA-->", base_url=BASE, max_images=1, client=client)


async def test_render_transport_error_is_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(boom) as client:
        with pytest.raises(ToolError, match="渲染服务暂时不可用"):
            await renderer.render("html", "<b>x</b>", base_url=BASE, max_images=1, client=client)


@pytest.mark.parametrize("source", ["  \n", "x" * (renderer.MAX_SOURCE_CHARS + 1)])
async def test_render_validates_locally(source: str):
    async with _client(lambda _r: pytest.fail("should not call the service")) as client:
        with pytest.raises(ToolError):
            await renderer.render("markdown", source, base_url=BASE, max_images=1, client=client)


async def test_attach_uses_remaining_slots_and_pages_captions(service: Any):
    service.handler = lambda _r: _ok(3)
    ctx = _ctx(images=1)
    result = await _attach_render(ctx, "markdown", "# long", " 对比 ")
    assert service.seen[-1]["max_images"] == MAX_IMAGES - 1
    assert [c for _, c in ctx.deps.images[1:]] == ["对比", "对比（2/3）", "对比（3/3）"]
    assert result == "已生成图 2–4（共 3 页）（对比），会附在合并转发里，正文不用再重复图里的内容。"


async def test_attach_single_image_without_caption(service: Any):
    ctx = _ctx()
    result = await _attach_render(ctx, "mermaid", "graph TD\nA-->B", "")
    assert ctx.deps.images == [(b"page0", "")]
    assert result == "已生成图 1（无标题），会附在合并转发里，正文不用再重复图里的内容。"


async def test_attach_pages_without_caption_stay_empty(service: Any):
    service.handler = lambda _r: _ok(2)
    ctx = _ctx()
    await _attach_render(ctx, "markdown", "# long", "")
    assert [c for _, c in ctx.deps.images] == ["", ""]


async def test_attach_refuses_when_slots_are_full(service: Any):
    with pytest.raises(ToolError, match=f"已经有 {MAX_IMAGES} 张图"):
        await _attach_render(_ctx(images=MAX_IMAGES), "html", "<b>x</b>", "")
    assert service.seen == []


async def test_attach_without_service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tools.APP_CONFIG, "render_url", None)
    with pytest.raises(ToolError, match="没有配置渲染服务"):
        await _attach_render(_ctx(), "mermaid", "graph TD\nA-->B", "")


async def test_attach_appends_service_warnings(service: Any):
    service.handler = lambda _r: _ok(1, ["第 3 个 mermaid 代码块语法错误，已按代码显示"])
    result = await _attach_render(_ctx(), "markdown", "# x", "")
    assert result.endswith("改好内容后重新生成）：\n- 第 3 个 mermaid 代码块语法错误，已按代码显示")


def _pages_as_requested(request: httpx.Request) -> httpx.Response:
    return _ok(json.loads(request.content)["max_images"])


async def test_concurrent_renders_never_overfill_the_reply(service: Any):
    service.handler = _pages_as_requested
    ctx = _ctx(images=2)
    results = await asyncio.gather(
        _attach_render(ctx, "mermaid", "graph TD\nA-->B", "a"),
        _attach_render(ctx, "markdown", "# long", "b"),
        _attach_render(ctx, "html", "<b>x</b>", "c"),
        return_exceptions=True,
    )
    assert len(ctx.deps.images) == MAX_IMAGES
    assert [body["max_images"] for body in service.seen] == [1, 1]
    assert isinstance(results[2], ToolError)
    assert "同时进行" in str(results[2])
    assert ctx.deps.images_reserved == 0


async def test_failed_render_releases_its_slots(service: Any):
    service.handler = lambda _r: httpx.Response(422, json={"error": "太长"})
    ctx = _ctx()
    with pytest.raises(ToolError, match="太长"):
        await _attach_render(ctx, "markdown", "# x", "")
    assert ctx.deps.images_reserved == 0
    service.handler = lambda _r: _ok(1)
    await _attach_render(ctx, "mermaid", "graph TD\nA-->B", "")
    assert service.seen[-1]["max_images"] == 1


LIVE_URL = os.environ.get("YGG_TEST_RENDER_URL")
SAMPLES = {
    "mermaid": "sequenceDiagram\n  用户->>机器人: 你好\n  机器人-->>用户: 你好呀",
    "markdown": "# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('hi')\n```\n\n$e^{i\\pi}+1=0$\n\n"
    "> [!NOTE]\n> 提示\n\n```mermaid\ngraph LR\n  A-->B\n```\n",
    "html": '<div style="padding:12px;background:#ddf4ff;border-radius:6px"><b>卡片</b> 内容</div>',
}


@pytest.mark.skipif(not LIVE_URL, reason="YGG_TEST_RENDER_URL not set")
@pytest.mark.parametrize("kind", list(SAMPLES))
async def test_live_render(kind: renderer.Kind):
    pngs, warnings = await renderer.render(kind, SAMPLES[kind], base_url=cast(str, LIVE_URL), max_images=4)
    assert len(pngs) == 1
    assert pngs[0].startswith(PNG_MAGIC)
    assert warnings == []


@pytest.mark.skipif(not LIVE_URL, reason="YGG_TEST_RENDER_URL not set")
async def test_live_mermaid_syntax_error():
    with pytest.raises(ToolError) as e:
        await renderer.render("mermaid", "graph TD\n  A-->\n  B--x[", base_url=cast(str, LIVE_URL), max_images=1)
    assert str(e.value)

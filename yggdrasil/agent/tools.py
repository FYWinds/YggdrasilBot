"""Agent tools; failures are returned as text so the model can adapt.

Everything in ``ALL_TOOLS`` is read-only. Action tools (``action_tools``) change things and are only
handed to a run when the code has already decided the speaker may do that action.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import wraps
from typing import Any

from pydantic_ai import Agent, BinaryContent, RunContext, Tool, ToolReturn

from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.media import load_image, media_bytes, media_type
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.ingest.normalize import normalize
from yggdrasil.llm.registry import TaskNotConfigured, chat_model, sees_images
from yggdrasil.llm.streaming import drain_events
from yggdrasil.prompts import render
from yggdrasil.tools.errors import ToolError

type ToolResult = str | ToolReturn


def _tool[**P](name: str) -> Callable[[Callable[P, Awaitable[ToolResult]]], Callable[P, Awaitable[ToolResult]]]:
    def deco(fn: Callable[P, Awaitable[ToolResult]]) -> Callable[P, Awaitable[ToolResult]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
            ctx = args[0]
            assert isinstance(ctx, RunContext)
            ctx.deps.tool_counts[name] += 1
            if ctx.deps.on_slow_tool is not None and name in ctx.deps.settings.tools.slow_tools:
                await ctx.deps.on_slow_tool()
            started = time.perf_counter()
            outcome = "ok"
            try:
                return await fn(*args, **kwargs)
            except ToolError as e:
                outcome = "error"
                return f"[工具错误] {e}"
            except TaskNotConfigured as e:
                outcome = "error"
                return f"[工具不可用] {e}"
            except Exception as e:
                outcome = "error"
                LOGGER.opt(exception=e).warning("tool {} failed", name)
                return f"[工具错误] {type(e).__name__}: {str(e)[:200]}"
            finally:
                TOOL_CALLS.add(1, {"tool": name, "outcome": outcome})
                TOOL_DURATION.record(time.perf_counter() - started, {"tool": name})

        return wrapper

    return deco


def _handle(ctx: RunContext[Deps], key: str, *kinds: str) -> Handle:
    h = ctx.deps.handles.get(key)
    if h is None or (kinds and h.kind not in kinds):
        known = ", ".join(k for k, v in ctx.deps.handles.items.items() if not kinds or v.kind in kinds) or "无"
        raise ToolError(f"找不到句柄 {key}，可用的有：{known}")
    return h


async def _vision(ctx: RunContext[Deps], images: list[bytes], question: str) -> str:
    resolved = chat_model("vision")
    agent = Agent(resolved.model, instructions=render("vision_system"), name="vision")
    content: list[Any] = [question or "描述这张图片，并完整转写其中的文字。"]
    content += [BinaryContent(data=img, media_type=media_type(img)) for img in images]
    result = await agent.run(content, model_settings=resolved.settings, event_stream_handler=drain_events)
    usage = result.usage
    ctx.deps.vision_tokens.append((resolved.label, usage.input_tokens or 0, usage.output_tokens or 0))
    return result.output


@_tool("read_url")
async def read_url(ctx: RunContext[Deps], url_or_handle: str, page: int = 1) -> str:
    """读取网页正文（支持分页）。参数可以是 URL 或 url#N / card#N 句柄。"""
    from yggdrasil.tools.web import read_url as _read

    url = url_or_handle
    if "#" in url_or_handle and not url_or_handle.startswith("http"):
        h = _handle(ctx, url_or_handle, "url", "card")
        url = h.data.get("url") or ""
        if not url:
            raise ToolError("这个卡片没有链接")
    cfg = ctx.deps.settings.tools
    res = await _read(
        url,
        max_chars=cfg.read_url_max_chars,
        page=page,
        browser_ws_url=APP_CONFIG.browser_ws_url if cfg.browser_fallback else None,
    )
    more = f"（第 {res.page}/{res.pages} 页，可用 page 参数翻页）" if res.pages > 1 else ""
    return f"标题：{res.title}\n地址：{res.final_url}{more}\n\n{res.text}"


@_tool("web_search")
async def web_search(ctx: RunContext[Deps], query: str) -> str:
    """用搜索引擎查资料，返回标题、链接和摘要。需要细节时再用 read_url 读原文。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools.search import web_search as _search

    hits = await _search(query, snapshot().search_providers, n=ctx.deps.settings.tools.search_results)
    if not hits:
        return "没有搜索结果"
    return "\n\n".join(f"{i + 1}. {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits))


@_tool("read_file")
async def read_file(ctx: RunContext[Deps], handle: str) -> str:
    """读取文件内容（PDF/Office/文本/代码等），参数是 file#N 句柄。"""
    from yggdrasil.tools.docs import parse_document

    h = _handle(ctx, handle, "file")
    cfg = ctx.deps.settings.tools
    blob, name = await media_bytes(ctx.deps.api, ctx.deps.group_id, h, cfg.file_max_mb * 1024 * 1024)
    doc = await asyncio.to_thread(parse_document, blob, name, max_pages=cfg.file_max_pages)
    text = doc.text
    if doc.scanned_pages:
        text = await _vision(ctx, doc.scanned_pages, "这是扫描版文档的页面，请按顺序完整转写文字，保留结构。")
    note = "（内容已截断）" if doc.truncated else ""
    return f"文件：{name}（{doc.kind}，{doc.pages} 页）{note}\n\n{text}"


@_tool("view_image")
async def view_image(ctx: RunContext[Deps], handle: str, question: str = "") -> ToolResult:
    """查看图片，参数是 img#N 句柄，可附带具体问题。能看图的模型会直接收到原图，否则返回文字描述和转写。"""
    h = _handle(ctx, handle, "image")
    blob = await load_image(ctx.deps.api, ctx.deps.group_id, h)
    if sees_images(ctx.deps.task):
        return ToolReturn(
            return_value=f"{handle} 的原图附在下一条消息里，直接看图。",
            content=[f"{handle}：", BinaryContent(data=blob, media_type=media_type(blob))],
        )
    try:
        return "（以下是另一个模型写的图片描述）\n" + await _vision(ctx, [blob], question)
    except TaskNotConfigured:
        result = await ctx.deps.api.ocr_image(image=str(h.data.get("url") or h.data.get("file")))
        return "（无视觉模型，以下为 OCR 结果）\n" + "\n".join(t.text for t in result.texts)


@_tool("expand_forward")
async def expand_forward(ctx: RunContext[Deps], handle: str) -> str:
    """展开合并转发（聊天记录），参数是 fwd#N 句柄。嵌套的转发也会展开。"""
    h = _handle(ctx, handle, "forward")
    lines: list[str] = []

    async def walk(forward_id: str, depth: int, content: list | None) -> None:
        if depth > 3 or len(lines) > 400:
            return
        records = content
        if records is None:
            records = [m.model_dump() for m in (await ctx.deps.api.get_forward_msg(id=forward_id)).messages]
        for rec in records:
            if rec.get("type") == "node":  # OneBot node format (our own stored forwards)
                rec = rec.get("data") or {}
            sender = rec.get("sender") or {}
            segs = rec.get("message") or rec.get("content") or []
            if isinstance(segs, str):
                segs = [{"type": "text", "data": {"text": segs}}]
            norm = normalize(segs)
            ts = datetime.fromtimestamp(rec["time"]).strftime("%m-%d %H:%M") if rec.get("time") else ""
            name = sender.get("card") or sender.get("nickname") or rec.get("nickname") or "?"
            lines.append(f"{'  ' * depth}[{ts}] {name}: {norm.text}")
            for att in norm.attachments:
                key = ctx.deps.handles.add(att.kind, att.data)
                if key:
                    lines[-1] += f" ({key})"
                if att.kind == "forward":
                    inner = att.data.get("content")
                    await walk(str(att.data.get("id", "")), depth + 1, inner if isinstance(inner, list) else None)

    inner = h.data.get("content")
    await walk(str(h.data.get("id", "")), 0, inner if isinstance(inner, list) else None)
    return "\n".join(lines) or "（空的合并转发）"


@_tool("search_history")
async def search_history(ctx: RunContext[Deps], query: str, days: int | None = None, user_id: int | None = None) -> str:
    """检索本群历史聊天记录和话题摘要（关键词+语义）。days 限定最近几天，user_id 限定发言人。"""
    from yggdrasil.memory.search import search_history as _search

    if ctx.deps.group_id is None:
        raise ToolError("私聊里不能检索群历史")
    since = None
    if days:
        since = datetime.now().astimezone() - timedelta(days=days)
    hits = await _search(
        ctx.deps.group_id, query, since=since, user_id=user_id, top_k=ctx.deps.settings.tools.history_top_k
    )
    if not hits:
        return "没有找到相关记录"
    out = []
    for h in hits:
        when = h.when.astimezone().strftime("%Y-%m-%d %H:%M") if h.when else ""
        ref = f"#{h.msg_id}" if h.msg_id else f"{h.kind}:{h.ref_id}"
        who = f"{h.sender_name}: " if h.sender_name else ""
        out.append(f"[{when}] ({ref}) {who}{h.text}")
    return "\n".join(out)


@_tool("get_messages_around")
async def get_messages_around(ctx: RunContext[Deps], message_id: int, before: int = 10, after: int = 10) -> str:
    """取某条历史消息（#id）前后的上下文。"""
    from yggdrasil.memory.search import messages_around

    if ctx.deps.group_id is None:
        raise ToolError("私聊里不能检索群历史")
    msgs = await messages_around(ctx.deps.group_id, message_id, min(before, 40), min(after, 40))
    return "\n".join(f"[{m.sent_at.astimezone():%m-%d %H:%M}] #{m.id} {m.sender_name}: {m.text}" for m in msgs) or "无"


@_tool("recent_chat")
async def recent_chat(ctx: RunContext[Deps], minutes: int = 30) -> str:
    """取本群最近的聊天记录（最近 minutes 分钟，至少包含最近若干条）。只在目标需要群里的上下文才能讲清时用，
    比如短词、黑话、截图、引用的一句聊天；链接、文件、合并转发直接读原文，不需要它。"""
    from yggdrasil.memory.search import recent_messages

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群聊记录")
    cfg = ctx.deps.settings.context
    msgs = await recent_messages(ctx.deps.group_id, limit=cfg.recent_messages, minutes=min(max(minutes, 1), 24 * 60))
    if not msgs:
        return "这段时间没有聊天记录"
    me = f"{ctx.deps.settings.persona.name}(你)"
    text = "\n".join(
        f"[{m.sent_at.astimezone():%m-%d %H:%M}] #{m.id} {me if m.is_self else m.sender_name}: {m.text}" for m in msgs
    )
    if len(text) > cfg.max_context_chars:
        text = text[-cfg.max_context_chars :]
        text = text[text.find("\n") + 1 :]
    return text


@_tool("lookup_glossary")
async def lookup_glossary(ctx: RunContext[Deps], term: str) -> str:
    """查本群黑话/梗词典。"""
    from yggdrasil.memory.search import lookup_glossary as _lookup

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群词典")
    terms = await _lookup(ctx.deps.group_id, term)
    if not terms:
        return f"词典里没有「{term}」"
    return "\n".join(f"{t.term}（别名：{'、'.join(t.aliases) or '无'}）：{t.definition}" for t in terms)


@_tool("get_profile")
async def get_profile(ctx: RunContext[Deps], user_id: int) -> str:
    """查群友画像（昵称、兴趣、常聊话题）。"""
    from yggdrasil.memory.search import get_profile as _get

    if ctx.deps.group_id is None:
        raise ToolError("私聊里没有群友画像")
    p = await _get(ctx.deps.group_id, user_id)
    if p is None:
        return "没有这个人的画像（可能选择了不被记录）"
    return f"{p.display_name}：{p.summary}\n兴趣：{'、'.join(p.interests)}\n特点：{'、'.join(p.traits)}"


_MAX_IMAGES = 4


async def _attach_render(ctx: RunContext[Deps], kind: str, source: str, caption: str) -> str:
    from yggdrasil.tools import render as renderer

    if not APP_CONFIG.browser_ws_url:
        raise ToolError("没有可用的渲染浏览器")
    if len(ctx.deps.images) >= _MAX_IMAGES:
        raise ToolError(f"这次回复已经有 {_MAX_IMAGES} 张图了")
    fn = {"mermaid": renderer.render_mermaid, "markdown": renderer.render_markdown, "html": renderer.render_html}[kind]
    png = await fn(source, browser_ws_url=APP_CONFIG.browser_ws_url)
    ctx.deps.images.append((png, caption.strip()))
    return f"已生成图 {len(ctx.deps.images)}（{caption or '无标题'}），会附在合并转发里，正文不用再重复图里的内容。"


@_tool("render_mermaid")
async def render_mermaid(ctx: RunContext[Deps], code: str, caption: str = "") -> str:
    """把 Mermaid 代码（flowchart、sequenceDiagram、classDiagram 等）渲染成图片，附在合并转发里发出。
    节点文字用中文即可，含特殊字符的文字用双引号包起来。语法错误会返回报错，改好后重试。"""
    return await _attach_render(ctx, "mermaid", code, caption)


@_tool("render_markdown")
async def render_markdown(ctx: RunContext[Deps], markdown: str, caption: str = "") -> str:
    """把 Markdown（表格、代码块、标题、列表都支持）排版渲染成一张图片，附在合并转发里发出。
    适合对比表、代码、带格式的长内容这类纯文本消息里不好看的东西。"""
    return await _attach_render(ctx, "markdown", markdown, caption)


@_tool("render_html")
async def render_html(ctx: RunContext[Deps], html: str, caption: str = "") -> str:
    """把一段 HTML（可以用内联 style，不能用脚本和外部资源）渲染成图片，附在合并转发里发出。
    Markdown 表达不了的排版（卡片、色块、复杂表格）再用它。"""
    return await _attach_render(ctx, "html", html, caption)


ALL_TOOLS: dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "read_url": read_url,
    "web_search": web_search,
    "read_file": read_file,
    "view_image": view_image,
    "expand_forward": expand_forward,
    "search_history": search_history,
    "get_messages_around": get_messages_around,
    "recent_chat": recent_chat,
    "lookup_glossary": lookup_glossary,
    "get_profile": get_profile,
    "render_mermaid": render_mermaid,
    "render_markdown": render_markdown,
    "render_html": render_html,
}


# chat gets recent chat injected into the prompt; tldr summarizes the target itself, so it fetches chat
# context only on demand and gets no profiles (they slant the summary toward the asker).
_HANDLER_EXCLUDED: dict[str, frozenset[str]] = {
    "chat": frozenset({"recent_chat"}),
    "tldr": frozenset({"get_profile"}),
}


def tool_names(enabled: list[str], handler: str) -> list[str]:
    excluded = _HANDLER_EXCLUDED.get(handler, frozenset())
    return [name for name in enabled if name in ALL_TOOLS and name not in excluded]


def tools_for(names: list[str]) -> list[Tool[Deps]]:
    return [Tool(ALL_TOOLS[name], takes_ctx=True) for name in names]


@_tool("recall_message")
async def recall_message(ctx: RunContext[Deps]) -> str:
    """撤回被回复的那条消息（你自己发的）。成功后选 mode=done，不要再发消息；失败就照实说明。"""
    target = ctx.deps.recall_target
    if target is None:
        raise ToolError("这次没有可以撤回的消息")
    await ctx.deps.api.delete_msg(message_id=target)
    ctx.deps.actions.append(f"recall {target}")
    return "已撤回"


def action_tools(deps: Deps) -> list[Tool[Deps]]:
    """Action tools this run's speaker is allowed to use (decided in code before the run)."""
    return [Tool(recall_message, takes_ctx=True)] if deps.recall_target is not None else []

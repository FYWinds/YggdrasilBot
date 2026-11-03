"""Web and X tools: ``read_url``, ``read_x``, ``search_x``, ``web_search``."""

from typing import Literal

from pydantic_ai import RunContext

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool, resolve_ref
from yggdrasil.config import APP_CONFIG
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.schema import Hit, HitsDisplay, PageDisplay


@chat_tool("read_url")
async def read_url(ctx: RunContext[Deps], url_or_handle: str, page: int = 1) -> str:
    """读取网页正文（支持分页）。参数可以是 URL、url#N / card#N 句柄，或聊天记录里的消息编号 #N（那条消息里的链接）。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools.remote_fetch import remote_fetcher
    from yggdrasil.tools.web import read_url as _read

    url = url_or_handle
    if "#" in url_or_handle and not url_or_handle.startswith("http"):
        h = await resolve_ref(ctx, url_or_handle, "url", "card")
        url = h.data.get("url") or ""
        if not url:
            raise ToolError("这个卡片没有链接")
    cfg = ctx.deps.settings.tools
    res = await _read(
        url,
        max_chars=cfg.read_url_max_chars,
        page=page,
        browser_url=APP_CONFIG.browser_url if cfg.browser_fallback else None,
        remote=remote_fetcher(snapshot().search_providers) if cfg.remote_fetch else None,
    )
    page_info = PageDisplay(
        url=url, final_url=res.final_url, title=res.title, via=res.via, page=res.page, pages=res.pages
    )
    ctx.deps.notes.show(ctx.tool_call_id, page_info)
    more = f"（第 {res.page}/{res.pages} 页，可用 page 参数翻页）" if res.pages > 1 else ""
    return f"标题：{res.title}\n地址：{res.final_url}{more}\n\n{res.text}{_image_handles(ctx, res.images, 'web')}"


def _image_handles(ctx: RunContext[Deps], urls: list[str], source: str) -> str:
    """Register third-party image URLs (numbered 图 1, 图 2, ... in the text) as img#N handles; the line to append."""
    keys = [ctx.deps.handles.add("image", {"url": url, "source": source}) for url in urls]
    shown = "、".join(f"图 {i} = {key}" for i, key in enumerate(keys, 1) if key)
    return f"\n\n图片句柄（用 view_image 查看）：{shown}" if shown else ""


@chat_tool("read_x")
async def read_x(ctx: RunContext[Deps], url_or_id: str, page: int = 1) -> str:
    """读 X（推特）上的一条帖子：作者、时间、互动数、全文（含长推和文章）、引用的帖子、投票、社区笔记，以及同一对话里的
    其他帖子；图片注册成 img#N，用 view_image 看。参数是帖子链接（x.com、twitter.com 等）、帖子 id、
    url#N / card#N 句柄，或聊天记录里的消息编号 #N（那条消息里的链接）。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools import x
    from yggdrasil.tools.web import paginate

    ref = url_or_id.strip()
    if "#" in ref and not ref.startswith("http"):
        ref = str((await resolve_ref(ctx, ref, "url", "card")).data.get("url") or "")
    pid = x.post_id(ref)
    if pid is None:
        raise ToolError("这不是 X 帖子的链接或 id")
    post = await x.read_post(pid, x.provider(snapshot().search_providers))
    pages = paginate(x.format_post(post), ctx.deps.settings.tools.read_url_max_chars)
    if not 1 <= page <= len(pages):
        raise ToolError(f"页码超出范围，共 {len(pages)} 页")
    title = f"{x.who(post)} 在 X 上的帖子"
    ctx.deps.notes.show(
        ctx.tool_call_id,
        PageDisplay(url=ref, final_url=post.url, title=title, via=post.via, page=page, pages=len(pages)),
    )
    more = f"（第 {page}/{len(pages)} 页，可用 page 参数翻页）" if len(pages) > 1 else ""
    images = _image_handles(ctx, [m.url for m in post.images()], "x") if page == 1 else ""
    return f"标题：{title}\n地址：{post.url}{more}\n\n{pages[page - 1]}{images}"


@chat_tool("search_x")
async def search_x(ctx: RunContext[Deps], query: str, kind: Literal["news", "posts"] = "news", hours: int = 24) -> str:
    """搜 X（推特）上的最新动态和讨论：Grok 在 X 上搜索后用中文总结，附上引用的帖子链接。很慢（常要 30–60 秒），
    只在确实需要 X 上的内容时用，普通资料用 web_search。query 直接写想问的问题
    （“某某最近说了什么”“XX 发布了吗，大家怎么评价”）。hours 是往前看几小时（最多 720）。Grok 不可用时改用 X API：
    kind="news"（默认）是 X 整理的新闻摘要，kind="posts" 是最近 7 天按相关度排的原帖（最多 10 条）。
    要看帖子全文、图片或评论上下文，再用 read_x。"""
    from yggdrasil.core.log import LOGGER
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools import grok, x
    from yggdrasil.tools.search import reserve_quota

    query = query.strip()
    if not query:
        raise ToolError("搜索关键词不能为空")
    providers = snapshot().search_providers
    api = x.provider(providers)
    if (g := grok.provider(providers)) is not None:
        try:
            if not await reserve_quota(g):
                raise ToolError("Grok 本月额度已用完")
            answer = await grok.x_search(g, query, hours=hours)
        except ToolError as e:
            if api is None:
                raise
            LOGGER.warning("grok x_search failed, falling back to the X API: {}", e)
        else:
            hits = [Hit(title="Grok 总结", snippet=answer.text)] + [Hit(title=u, url=u) for u in answer.citations]
            ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
            return grok.format_answer(answer)
    if api is None:
        raise ToolError("没有配置 Grok 或 X API（控制台 → 搜索源 → 添加 Grok 或 X API 类型）")
    if kind == "news":
        n = ctx.deps.settings.tools.search_results
        stories = await x.search_news(api, query, hours=hours, n=n, reserve=reserve_quota)
        if not stories:
            return "X 上没有相关的新闻"
        hits = [
            Hit(title=s.name, url=f"https://x.com/i/status/{s.post_ids[0]}" if s.post_ids else "", snippet=s.summary)
            for s in stories
        ]
        ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
        return x.format_news(stories)
    posts = await x.search_posts(api, query, hours=hours, reserve=reserve_quota)
    if not posts:
        return "X 上没有相关的帖子"
    hits = [Hit(title=x.who(p), url=p.url, snippet=p.text) for p in posts]
    ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
    return x.format_posts(posts)


@chat_tool("web_search")
async def web_search(ctx: RunContext[Deps], query: str) -> str:
    """用搜索引擎查资料，返回标题、链接和摘要。需要细节时再用 read_url 读原文。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools.search import web_search as _search

    hits = await _search(query, snapshot().search_providers, n=ctx.deps.settings.tools.search_results)
    if not hits:
        return "没有搜索结果"
    shown = [Hit(title=h.title, url=h.url, snippet=h.snippet) for h in hits]
    ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=shown))
    return "\n\n".join(f"{i + 1}. {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits))

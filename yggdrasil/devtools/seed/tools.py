"""Fake tools with the real tools' names and arguments; they record into ``notes`` the way the real ones do."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic_ai import BinaryContent, ModelRetry, RunContext, ToolReturn

from yggdrasil.db.models import Message
from yggdrasil.devtools.seed.chat import Chat, format_lines
from yggdrasil.devtools.seed.data import MEDIA, TECH, Media
from yggdrasil.ingest.normalize import normalize
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import ForwardNode, Hit, HitsDisplay, PageDisplay, QQForwardDisplay


@dataclass
class SeedDeps:
    notes: TraceNotes
    chat: Chat
    before: datetime
    target: Message | None = None


def _image_of(m: Message) -> tuple[dict[str, Any], Media]:
    seg = next(s for s in m.segments if s["type"] == "image")
    name = next(k for k, v in MEDIA.items() if seg["data"]["file"].startswith(v.md5))
    return seg["data"], MEDIA[name]


async def recent_chat(ctx: RunContext[SeedDeps], minutes: int = 30) -> str:
    rows = [m for m in ctx.deps.chat.rows if m.sent_at < ctx.deps.before][-15:]
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in rows])
    return format_lines(rows)


async def get_messages_around(ctx: RunContext[SeedDeps], message_id: int, before: int = 10, after: int = 10) -> str:
    rows = ctx.deps.chat.rows
    i = rows.index(ctx.deps.chat.by_id(message_id))
    window = rows[max(0, i - before) : i + after + 1]
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in window])
    return format_lines(window)


async def search_history(
    ctx: RunContext[SeedDeps], query: str, days: int | None = None, user_id: int | None = None
) -> str:
    if days is not None and days <= 0:
        raise ModelRetry("days 要大于 0；不限时间就不要传")
    hits = [m for m in ctx.deps.chat.rows if query in m.plain_text and (user_id is None or m.user_id == user_id)][-5:]
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in hits])
    return format_lines(hits) or "没有找到相关记录。"


async def view_image(ctx: RunContext[SeedDeps], handle: str, question: str = "") -> ToolReturn:
    data, media = _image_of(ctx.deps.chat.by_id(int(handle.lstrip("#"))))
    ctx.deps.notes.image(media.data, media.mime, segment=data)
    return ToolReturn(
        return_value=f"{handle} 的原图附在下一条消息里，直接看图。",
        content=[f"{handle}：", BinaryContent(media.data, media_type=media.mime)],
    )


async def expand_forward(ctx: RunContext[SeedDeps], handle: str) -> str:
    assert ctx.deps.target is not None
    shown: list[ForwardNode] = []
    lines: list[str] = []

    def walk(content: list[dict[str, Any]], depth: int) -> None:
        for rec in content:
            sender = rec["sender"]
            sent = datetime.fromtimestamp(rec["time"]).astimezone()
            shown.append(
                ForwardNode(
                    depth=depth,
                    user_id=sender["user_id"],
                    sender_name=sender["nickname"],
                    sent_at=sent,
                    segments=rec["message"],
                )
            )
            text = normalize(rec["message"]).text
            lines.append(
                f"{'  ' * depth}[{sent.strftime('%m-%d %H:%M')}] {sender['nickname']}({sender['user_id']}): {text}"
            )
            for seg in rec["message"]:
                if seg["type"] == "forward":
                    walk(seg["data"]["content"], depth + 1)

    # A tldr on a remark about the forward quotes the remark; the forward is one step up its reply chain.
    target = ctx.deps.target.segments
    forward = next((s for s in target if s["type"] == "forward"), None) or next(
        s for s in ctx.deps.chat.keys["fwd"].segments if s["type"] == "forward"
    )
    walk(forward["data"]["content"], 0)
    ctx.deps.notes.show(ctx.tool_call_id, QQForwardDisplay(nodes=shown))
    return "\n".join(lines)


async def web_search(ctx: RunContext[SeedDeps], query: str) -> str:
    hits = [
        Hit(
            title="Python 3.14 free-threading 指南",
            url="https://example.com/free-threading-howto",
            snippet="单线程开销约 5-10%，多线程 CPU 密集任务可以线性扩展……",
        ),
        Hit(title="PEP 779 – Criteria for supported status", url="https://example.com/pep-0779", snippet="……"),
        Hit(title="What's new in Python 3.14", url="https://example.com/whatsnew-3-14", snippet="free-threaded 构建……"),
    ]
    ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
    return "\n\n".join(f"{i}. {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits, 1))


async def read_url(ctx: RunContext[SeedDeps], url_or_handle: str, page: int = 1) -> str:
    url = url_or_handle
    if url.startswith("card#"):
        url = "https://example.com/python-3-14"
    if "missing" in url:
        ctx.deps.notes.failed(ctx.tool_call_id)
        return f"读取失败：{url} 返回 HTTP 404"
    title = "What's new in Python 3.14" if "3-14" in url else "Python 3.14 free-threading 指南"
    ctx.deps.notes.show(
        ctx.tool_call_id, PageDisplay(url=url, final_url=url, title=title, via="direct", page=page, pages=2)
    )
    return f"# {title}\n\n（第 {page}/2 页）free-threaded 构建在 3.14 进入受支持阶段；单线程性能损失降到 5-10%……"


async def render_markdown(ctx: RunContext[SeedDeps], markdown: str, caption: str = "") -> str:
    ctx.deps.notes.shown(ctx.tool_call_id, [(MEDIA["render"].data, caption)])
    return "已渲染 1 页，会附在合并转发里。"


async def recall_message(ctx: RunContext[SeedDeps]) -> str:
    return "已撤回。"


async def generate_image(
    ctx: RunContext[SeedDeps],
    prompt: str,
    references: list[str] | None = None,
    aspect_ratio: str = "auto",
    text: str = "",
) -> str:
    ctx.deps.notes.shown(ctx.tool_call_id, [(MEDIA["drawing"].data, text)])
    return "图已经发到群里了（花费 $0.040）。没有别的要说就选 mode=done。"


async def lookup_glossary(ctx: RunContext[SeedDeps], term: str) -> str:
    return f"没有收录「{term}」。相近的词条：新赛季（每季度第一天晚上八点开服）"


async def read_file(ctx: RunContext[SeedDeps], handle: str) -> str:
    await asyncio.sleep(60)  # cancelled by the seed while it "reads"
    return ""


async def list_reviews(ctx: RunContext[SeedDeps], status: str = "pending") -> str:
    return json.dumps([{"group_id": TECH, "request": "帮我查一下某个同事的手机号"}], ensure_ascii=False)


async def get_settings(ctx: RunContext[SeedDeps], group_id: int | None = None, section: str | None = None) -> str:
    return json.dumps({"stored": {}, "effective": {"ratelimit": {"user_per_minute": 4}}}, ensure_ascii=False)


async def update_settings(ctx: RunContext[SeedDeps], patch: dict[str, Any], group_id: int | None = None) -> str:
    return "已修改，配置已重新加载。"


CHAT_TOOLS = [
    recent_chat,
    get_messages_around,
    search_history,
    view_image,
    expand_forward,
    web_search,
    read_url,
    render_markdown,
    recall_message,
    generate_image,
    lookup_glossary,
    read_file,
]
OPS_TOOLS = [list_reviews, get_settings, update_settings]

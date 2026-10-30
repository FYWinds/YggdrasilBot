"""Synthetic, public-safe data for a local console::

    uv run python -m yggdrasil.devtools.seed [--reset]

Two example groups (10001, 10002) get members, chat covering every QQ segment type, stickers, memory (topics, a
digest, glossary, profiles), bot events, review items, the bot's invocations of every handler and outcome, and
background jobs (archived ones and one running in Redis). Transcripts come from the real recorder: each invocation
is a scripted ``FunctionModel`` run whose prompt is built by the real ``build_context`` and whose tools record what
they showed the way the real ones do; job transcripts go through the real omp message conversion.

Everything written is scoped (the two groups, members 20001-20009, ``seed-`` job ids, the seed provider, stickers
and images), so running it again replaces the previous seed and ``--reset`` only removes it. Uses the usual
``POSTGRES_*`` / ``REDIS_URL`` settings and refuses to run with ``YGG_ENV=prod``.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import random
import secrets
import sys
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
from pydantic_ai import Agent, BinaryContent, ModelRetry, PromptedOutput, RunContext, ToolReturn
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.agent.context import build_context
from yggdrasil.agent.schema import BotReply, OpsReply, Section, SourceRef
from yggdrasil.config import APP_CONFIG
from yggdrasil.db.models import (
    BotEvent,
    DailyDigest,
    GlossaryCandidate,
    GlossaryTerm,
    Group,
    Invocation,
    Message,
    MessageChunk,
    ModerationResult,
    Provider,
    ReviewItem,
    SandboxJob,
    Sticker,
    StickerBlob,
    StickerUsage,
    TaskRoute,
    Topic,
    TopicMessage,
    Transcript,
    TranscriptBlob,
    UserAlias,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kv import redis_client
from yggdrasil.memory import search
from yggdrasil.memory.search import MessageView
from yggdrasil.people import self_label
from yggdrasil.prompts import render
from yggdrasil.sandbox import archive
from yggdrasil.sandbox.jobs import (
    ACTIVE_KEY,
    Job,
    JobEvent,
    events_key,
    group_key,
    job_key,
    render_rules,
    running_key,
)
from yggdrasil.settings import Settings
from yggdrasil.transcript import store
from yggdrasil.transcript.capture import TranscriptRecorder, text_part
from yggdrasil.transcript.notes import Blob, TraceNotes, blob_id
from yggdrasil.transcript.omp import message_entries
from yggdrasil.transcript.schema import (
    Entry,
    ForwardNode,
    Hit,
    HitsDisplay,
    PageDisplay,
    QQContextDisplay,
    QQForwardDisplay,
    QQMessage,
)
from yggdrasil.triggers.index import Resolved

TECH, GAME = 10001, 10002
GROUPS = {TECH: "示例技术群", GAME: "示例游戏群"}
SELF = 10000  # the bot's QQ number in seed data
MEMBERS = {20001: "阿明", 20002: "小红", 20003: "老王", 20004: "Neko", 20005: "路人甲", 20006: "新来的"}
SUPERUSER = 20001
JOB_PREFIX = "seed-"
PROVIDER = "seed-example"
MODEL, FAST = "example/chat-large", "example/chat-fast"
ADMIN = "admin@example.com"
TRACE_PREFIX = "5eed"  # seeded invocations' trace ids start with it, so reset finds them (private ones too)
FLAG_PREFIX = "seed-"  # bot event request flags
PRIVATE_TEXT = "把示例游戏群每人每天的次数调到 120"
PERSONA = Settings().persona.name
NOW = datetime.now(UTC)


# --- media ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest().upper()  # QQ's file name scheme, not security

    @property
    def url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _png(
    label: str, color: tuple[int, int, int], size: tuple[int, int] = (640, 400), bars: list[int] | None = None
) -> Media:
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle((10, 10, w - 10, h - 10), outline=(255, 255, 255), width=4)
    draw.text((32, 28), label, fill=(255, 255, 255), font=_font(34))
    for i, value in enumerate(bars or []):
        x = 60 + i * 110
        draw.rectangle((x, h - 40 - value, x + 70, h - 40), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Media(buf.getvalue(), "image/png")


def _gif(label: str, colors: list[tuple[int, int, int]]) -> Media:
    frames = []
    for i, color in enumerate(colors):
        img = Image.new("RGB", (240, 240), color)
        draw = ImageDraw.Draw(img)
        draw.ellipse((40 + i * 10, 40, 200 - i * 10, 200), fill=(255, 255, 255))
        draw.text((70, 105), label, fill=color, font=_font(30))
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return Media(buf.getvalue(), "image/gif")


def _media() -> dict[str, Media]:
    return {
        "bench": _png("free-threading benchmark", (45, 85, 140), bars=[200, 190, 260, 300]),
        "map": _png("season 7 map", (40, 120, 70)),
        "render": _png("rendered markdown", (90, 60, 130), size=(800, 500)),
        "screenshot": _png("pelican site", (180, 90, 40), size=(960, 600)),
        "drawing": _png("pixel poster", (60, 150, 200), size=(640, 640)),
        "doge": _gif("doge", [(200, 160, 60), (220, 180, 80), (240, 200, 100)]),
        "cat": _png("cat", (120, 120, 120), size=(240, 240)),
        "happy": _png("happy", (230, 120, 150), size=(240, 240)),
    }


MEDIA = _media()


# --- segments -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """A reply segment to the line with this key (its QQ id is known once the lines are numbered)."""

    key: str


type Seg = dict[str, Any] | Ref


def _t(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def _at(qq: int | str) -> dict[str, Any]:
    return {"type": "at", "data": {"qq": str(qq)}}


def _face(face_id: int, label: str) -> dict[str, Any]:
    return {"type": "face", "data": {"id": str(face_id), "raw": {"faceText": label}}}


def _img(name: str) -> dict[str, Any]:
    m = MEDIA[name]
    ext = m.mime.split("/")[1]
    return {
        "type": "image",
        "data": {"file": f"{m.md5}.{ext}", "sub_type": 0, "summary": "", "url": m.url, "file_size": str(len(m.data))},
    }


def _sticker(name: str) -> dict[str, Any]:
    seg = _img(name)
    seg["data"].update(sub_type=1, summary="[动画表情]")
    return seg


def _mface() -> dict[str, Any]:
    return {
        "type": "mface",
        "data": {
            "emoji_package_id": "230001",
            "emoji_id": "5f3c2e1a9b8d7c6e5f4a3b2c1d0e9f8a",
            "key": "seedkey0001",
            "summary": "[开心]",
            "url": MEDIA["happy"].url,
        },
    }


def _card(title: str, url: str) -> dict[str, Any]:
    doc = {
        "app": "com.tencent.structmsg",
        "prompt": f"[分享]{title}",
        "meta": {"news": {"title": title, "jumpUrl": url}},
    }
    return {"type": "json", "data": {"data": json.dumps(doc, ensure_ascii=False)}}


def _node(user_id: int, name: str, minutes: float, segments: list[dict[str, Any]]) -> dict[str, Any]:
    sent = NOW - timedelta(days=7, minutes=minutes)
    return {"sender": {"user_id": user_id, "nickname": name}, "time": int(sent.timestamp()), "message": segments}


def _forward() -> dict[str, Any]:
    inner = [
        _node(20003, "老王", 30, [_t("JIT 在 3.14 里还是实验性的，要 --enable-experimental-jit 编译")]),
        _node(20002, "小红", 29, [_t("那默认构建里就没有？")]),
    ]
    nodes = [
        _node(20001, "阿明", 40, [_t("CPython 的 JIT 现在到什么程度了？")]),
        _node(20003, "老王", 38, [_t("copy-and-patch，先把字节码模板拼起来，收益还不大")]),
        _node(20004, "Neko", 35, [_img("bench"), _t("这是官方的数字")]),
        _node(20002, "小红", 31, [{"type": "forward", "data": {"id": "seed-inner-1", "content": inner}}]),
    ]
    return {"type": "forward", "data": {"id": "seed-forward-1", "content": nodes}}


def _bot_forward(forward_id: str, sections: list[str]) -> dict[str, Any]:
    nodes = [
        {"type": "node", "data": {"user_id": str(SELF), "nickname": PERSONA, "content": [_t(s)]}} for s in sections
    ]
    return {"type": "forward", "data": {"id": forward_id, "content": nodes}}


SEGMENT_SAMPLES: list[dict[str, Any]] = [
    {"type": "record", "data": {"file": "voice-0001.amr", "file_size": "10240"}},
    {"type": "video", "data": {"file": "clip-0001.mp4", "file_size": "2097152"}},
    {"type": "file", "data": {"file": "benchmark.xlsx", "file_id": "seed-file-1", "file_size": "18432"}},
    {"type": "poke", "data": {"type": "1", "id": "1"}},
    {"type": "dice", "data": {"result": "4"}},
    {"type": "rps", "data": {"result": "2"}},
    {"type": "markdown", "data": {"content": "**今日小结**\n- free-threading 能用了\n- JIT 默认还是关的"}},
]


# --- chat -------------------------------------------------------------------------------------------------------


@dataclass
class Line:
    key: str
    minutes: float  # before now
    user: int
    segments: list[Seg]
    recalled: bool = False


TECH_CHAT = [
    Line("hello", 170, 20001, [_t("早上好，有人试过 Python 3.14 的 free-threading 构建吗？")]),
    Line("ft", 168, 20002, [_t("试了，"), _face(14, "/微笑"), _t(" 装的 3.14t，C 扩展还得等适配")]),
    Line("bench", 165, 20003, [_img("bench")]),
    Line("bench_note", 164, 20003, [_t("我跑的 benchmark：单线程慢 5% 左右，8 线程快了 5 倍")]),
    Line("ask_gil", 160, 20004, [Ref("bench"), _t("多线程那栏是关了 GIL 跑的吗？")]),
    Line("gil_ans", 158, 20003, [_at(20004), _t(" 对，PYTHON_GIL=0")]),
    Line("doge", 155, 20005, [_sticker("doge")]),
    Line("happy", 150, 20001, [_mface()]),
    Line("card", 140, 20002, [_card("Python 3.14 有哪些新特性", "https://example.com/python-3-14")]),
    Line("fwd", 130, 20004, [_forward()]),
    Line("fwd_note", 129, 20004, [_t("上周讨论 JIT 的记录，补课用")]),
    Line("voice", 120, 20005, [SEGMENT_SAMPLES[0]]),
    Line("clip", 115, 20001, [SEGMENT_SAMPLES[1]]),
    Line("file", 110, 20003, [SEGMENT_SAMPLES[2]]),
    Line("poke", 105, 20002, [SEGMENT_SAMPLES[3]]),
    Line("dice", 100, 20005, [SEGMENT_SAMPLES[4]]),
    Line("rps", 99, 20001, [SEGMENT_SAMPLES[5]]),
    Line("md", 90, 20004, [SEGMENT_SAMPLES[6]]),
    Line("oops", 80, 20002, [_t("发错群了")], recalled=True),
    Line("meetup", 70, 20001, [_at("all"), _t(" 周末聚会改到周日下午，地点不变")]),
    Line("meetup2", 68, 20003, [_t("收到，我带鹈鹕去")]),
    Line("pelican", 66, 20005, [_t("鹈鹕是谁？")]),
    Line("pelican2", 65, 20002, [_at(20005), _t(" 鹈鹕是老王给他那台旧服务器起的名字")]),
    Line(
        "bot_old", 50, SELF, [_bot_forward("seed-old", ["free-threading 构建去掉了 GIL，纯 Python 多线程能用满多核。"])]
    ),
    # Triggers of the invocations below.
    Line("ask_chart", 40, 20002, [_at(SELF), _t(" 老王那张图说明了什么？")]),
    Line("ask_fwd", 34, 20004, [Ref("fwd"), _at(SELF), _t(" 总结一下这段聊天记录")]),
    Line("fwd_remark", 33, 20002, [Ref("fwd"), _t("JIT 那段说得挺清楚")]),
    Line("ask_tldr", 28, 20001, [Ref("card"), _t("tldr")]),
    Line("ask_silent", 24, 20005, [_at(SELF), _t(" 哈哈哈")]),
    Line("ask_recall", 22, 20001, [Ref("bot_old"), _at(SELF), _t(" 撤回这条吧，发错地方了")]),
    Line("ask_review", 19, 20003, [_at(SELF), _t(" 帮我查一下某个同事的手机号")]),
    Line("ask_reject", 17, 20005, [_at(SELF), _t(" 写一段话阴阳一下老王")]),
    Line("ask_blocked", 15, 20005, [_at(SELF), _t(" （示例）一条会被输入审核拦下的请求")]),
    Line("ask_fail", 13, 20004, [_at(SELF), _t(" 查一下 PEP 779 现在是什么状态")]),
    Line("ask_draw", 12, 20003, [Ref("bench"), _at(SELF), _t(" 把这张图画成像素风海报")]),
    Line("ask_cancel", 11, 20002, [_at(SELF), _t(" 帮我看看这个表格"), SEGMENT_SAMPLES[2]]),
    Line("ask_rl", 9, 20005, [_at(SELF), _t(" 再来一个")]),
    Line("ask_chain", 8, 20003, [Ref("fwd_remark"), _t("tldr")]),
    Line("ops_reviews", 7, SUPERUSER, [_at(SELF), _t(" 现在有几条待审核？")]),
    Line("ops_rate", 5, SUPERUSER, [_at(SELF), _t(" 把本群每人每分钟的次数调到 6")]),
    Line("ops_handoff", 3, SUPERUSER, [_at(SELF), _t(" 3.14t 和 3.14 有什么区别")]),
    Line("ask_running", 0.3, 20004, [_at(SELF), _t(" 对比一下 3.13 和 3.14 的启动速度")]),
]

PATCH_NOTES = (
    "第七赛季更新说明：一、新地图「潮汐港」加入排位，地图中央的灯塔每三分钟开放一次；二、狙击类武器开镜速度降低 8%，"
    "冲锋枪腰射散布减小；三、新增双人模式，段位单独计算；四、修复了在楼梯上换弹会卡住的问题；五、赛季通行证新增 "
    "40 级奖励，老赛季的皮肤可以用代币兑换；六、服务器维护时间改为每周二凌晨四点到六点。"
)

GAME_CHAT = [
    Line("g_hi", 120, 20004, [_t("新赛季今晚开，谁一起？")]),
    Line("g_me", 118, 20005, [_t("我我我"), _face(178, "/斜眼笑")]),
    Line("g_shot", 110, 20004, [_img("map")]),
    Line("g_long", 100, 20001, [_t(PATCH_NOTES)]),
    Line("g_cat", 95, 20002, [_sticker("cat")]),
    Line("g_tldr", 30, 20005, [Ref("g_long"), _t("tldr")]),
    Line("g_meta", 20, 20004, [_at(SELF), _t(" 今晚几点开服？")]),
]


@dataclass
class Chat:
    group_id: int
    rows: list[Message] = field(default_factory=list)
    keys: dict[str, Message] = field(default_factory=dict)
    next_qq: int = 0

    def by_id(self, message_id: int) -> Message:
        return next(m for m in self.rows if m.id == message_id)

    def quoted(self, trigger: Message) -> Message:
        return next(m for m in self.rows if m.qq_message_id == trigger.reply_to_qq_id)


def _qq_ids(lines: list[Line], base: int) -> dict[str, int]:
    return {line.key: base + i for i, line in enumerate(lines)}


async def _insert_chat(group_id: int, lines: list[Line], base: int) -> Chat:
    qq = _qq_ids(lines, base)
    chat = Chat(group_id, next_qq=base + len(lines))
    async with get_session() as s:
        for line in lines:
            segments = [
                {"type": "reply", "data": {"id": str(qq[x.key])}} if isinstance(x, Ref) else x for x in line.segments
            ]
            reply_to = next((qq[x.key] for x in line.segments if isinstance(x, Ref)), None)
            msg = Message(
                sent_at=NOW - timedelta(minutes=line.minutes),
                group_id=group_id,
                user_id=line.user,
                qq_message_id=qq[line.key],
                reply_to_qq_id=reply_to,
                sender_name=PERSONA if line.user == SELF else MEMBERS[line.user],
                plain_text=normalize(segments).text,
                segments=segments,
                is_self=line.user == SELF,
                recalled=line.recalled,
                recalled_at=NOW - timedelta(minutes=line.minutes - 1) if line.recalled else None,
            )
            s.add(msg)
            chat.rows.append(msg)
            chat.keys[line.key] = msg
        await s.flush()
    return chat


async def _bot_message(chat: Chat, at: datetime, title: str, sections: list[str]) -> Message:
    chat.next_qq += 1
    segments = [_bot_forward(f"seed-reply-{chat.next_qq}", [title, *sections])]
    msg = Message(
        sent_at=at,
        group_id=chat.group_id,
        user_id=SELF,
        qq_message_id=chat.next_qq,
        sender_name=PERSONA,
        plain_text=normalize(segments).text,
        segments=segments,
        is_self=True,
    )
    async with get_session() as s:
        s.add(msg)
    chat.rows.append(msg)
    chat.rows.sort(key=lambda m: m.sent_at)
    return msg


def _who(m: Message) -> str:
    return f"{m.sender_name}({m.user_id})"


def _lines(rows: list[Message]) -> str:
    return "\n".join(
        f"[{m.sent_at.astimezone().strftime('%m-%d %H:%M')}] #{m.id} {_who(m)}: {normalize(m.segments).text}"
        for m in rows
    )


# --- tools ------------------------------------------------------------------------------------------------------
# Same names and arguments as the real tools; they record into ``notes`` the way the real ones do.


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
    return _lines(rows)


async def get_messages_around(ctx: RunContext[SeedDeps], message_id: int, before: int = 10, after: int = 10) -> str:
    rows = ctx.deps.chat.rows
    i = rows.index(ctx.deps.chat.by_id(message_id))
    window = rows[max(0, i - before) : i + after + 1]
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in window])
    return _lines(window)


async def search_history(
    ctx: RunContext[SeedDeps], query: str, days: int | None = None, user_id: int | None = None
) -> str:
    if days is not None and days <= 0:
        raise ModelRetry("days 要大于 0；不限时间就不要传")
    hits = [m for m in ctx.deps.chat.rows if query in m.plain_text and (user_id is None or m.user_id == user_id)][-5:]
    ctx.deps.notes.messages(ctx.tool_call_id, [m.id for m in hits])
    return _lines(hits) or "没有找到相关记录。"


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


# --- invocations ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MsgId:
    """A tool argument naming a seeded message: its ``messages.id`` (or ``#id`` with ``hash``)."""

    key: str
    hash: bool = False


@dataclass
class Turn:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    think: str = ""
    say: str = ""
    output: BaseModel | None = None
    fail: str = ""  # the model request fails with this


type Verdict = tuple[str, float, str, list[str]]  # verdict, score, action, categories
SAFE: Verdict = ("Safe", 0.02, "pass", [])

Status = Literal[
    "ok", "done", "silent", "review", "rejected", "blocked_input", "failed", "cancelled", "ratelimited", "running"
]
OpsStatus = Literal["done", "reply", "handoff"]


@dataclass
class Run:
    key: str  # the trigger line
    handler: Literal["chat", "tldr", "ops", "report"]
    status: Status | OpsStatus
    turns: list[Turn] = field(default_factory=list)
    group_id: int | None = TECH
    quoted: bool = False  # the trigger quotes the message it is about
    full: bool = True  # privacy.capture_llm_content
    output_verdict: Verdict | None = None
    error: str | None = None
    prompt: str = ""  # report runs: the job summary they are given
    user_id: int = 0  # private / report runs without a trigger line
    text_output: bool = False  # the model writes the output as text (prompted / native mode), not final_result


def _reply(title: str, *sections: str, sources: list[SourceRef] | None = None) -> BotReply:
    return BotReply(mode="forward", title=title, sections=[Section(content=s) for s in sections], sources=sources or [])


RUNS: list[Run] = [
    Run(
        "ask_chart",
        "chat",
        "ok",
        [
            Turn([("recent_chat", {"minutes": 60})], think="她问的是老王发的图，先看最近的聊天找到那张图。"),
            Turn([("view_image", {"handle": MsgId("bench", hash=True), "question": "图里比较的是什么"})]),
            Turn(
                [("web_search", {"query": "Python 3.14 free-threading 单线程性能损失"})],
                say="图里是单线程和多线程的耗时对比，我查一下官方给的数字核对一下。",
            ),
            Turn([("read_url", {"url_or_handle": "https://example.com/free-threading-howto"})]),
            Turn(
                output=_reply(
                    "老王的 benchmark 怎么看",
                    "图里四根柱子是同一个任务在普通构建和 3.14t（关掉 GIL）下，单线程和 8 线程的耗时。单线程慢了大约"
                    " 5%，这是去掉 GIL 后对象引用计数改成原子操作的代价，和官方说的 5-10% 一致。",
                    "8 线程快了 5 倍左右，说明他的任务是纯 Python 的 CPU 密集计算，"
                    "正好是 free-threading 受益最多的场景。如果用到了还没适配的 C 扩展，解释器会自动把 GIL 打开，"
                    "这时就看不到这个提升了。",
                    sources=[
                        SourceRef(label="#bench 老王的图"),
                        SourceRef(
                            label="Python 3.14 free-threading 指南", url="https://example.com/free-threading-howto"
                        ),
                    ],
                )
            ),
        ],
        output_verdict=("Safe", 0.03, "pass", []),
    ),
    Run(
        "ask_fwd",
        "chat",
        "ok",
        [
            Turn([("expand_forward", {"handle": "fwd#1"})]),
            Turn(
                [
                    (
                        "render_markdown",
                        {
                            "markdown": "| 方案 | 状态 |\n|---|---|\n"
                            "| free-threading | 3.14 受支持 |\n| JIT | 实验性 |",
                            "caption": "要点对比",
                        },
                    )
                ],
                think="记录不长，做一张对比表放在回复里更清楚。",
            ),
            Turn(
                output=_reply(
                    "JIT 讨论小结",
                    "大家在聊 CPython 的 JIT：3.14 里还是实验性的，要自己加 --enable-experimental-jit 编译，"
                    "默认构建里没有。它用 copy-and-patch 把字节码模板拼成机器码，目前收益还不大。",
                    "Neko 贴的是官方的 benchmark，小红转的那段补充了编译方式。要点对比见图。",
                )
            ),
        ],
        quoted=True,
        output_verdict=SAFE,
    ),
    Run(
        "ask_tldr",
        "tldr",
        "ok",
        [
            Turn([("read_url", {"url_or_handle": "card#1"})]),
            Turn([("search_history", {"query": "3.14", "days": 0})], think="看看群里之前有没有聊过。"),
            Turn([("search_history", {"query": "3.14", "days": 7})]),
            Turn([("get_messages_around", {"message_id": MsgId("card"), "before": 3, "after": 3})]),
            Turn(
                output=_reply(
                    "Python 3.14 新特性",
                    "free-threaded 构建转正（可以关掉 GIL），模板字符串 t-string，延迟求值的注解，"
                    "标准库加了 zstd 压缩。群里主要在讨论 free-threading 的性能。",
                    sources=[SourceRef(label="What's new in Python 3.14", url="https://example.com/python-3-14")],
                )
            ),
        ],
        quoted=True,
        output_verdict=SAFE,
    ),
    Run(
        "ask_chain",
        "tldr",
        "ok",
        [
            Turn(
                [("expand_forward", {"handle": "fwd#1"})],
                think="被引用的只是一句点评，它回复的是那段合并转发：总结转发本身。",
            ),
            Turn(
                output=_reply(
                    "JIT 讨论记录",
                    "这段转发在聊 CPython 的 JIT：3.14 里还是实验性的，要用 --enable-experimental-jit 自己编译，"
                    "默认构建没有。它用 copy-and-patch 拼机器码，目前收益不大。被引用的那句是在夸这段讲得清楚。",
                )
            ),
        ],
        quoted=True,
        output_verdict=SAFE,
    ),
    Run(
        "ask_silent",
        "chat",
        "silent",
        [Turn(output=BotReply(mode="silent", silent_reason="只是在笑，没有问题要回答"))],
    ),
    Run(
        "ask_recall",
        "chat",
        "done",
        [Turn([("recall_message", {})], think="他让我撤回我自己发的那条。"), Turn(output=BotReply(mode="done"))],
        quoted=True,
    ),
    Run(
        "ask_draw",
        "chat",
        "done",
        [
            Turn(
                [
                    (
                        "generate_image",
                        {
                            "prompt": "把这张 benchmark 柱状图画成 16-bit 像素风海报：保留四根柱子和数字，"
                            "深蓝背景，标题写 3.14t 多线程快 5 倍",
                            "references": [MsgId("bench", hash=True)],
                            "aspect_ratio": "1:1",
                        },
                    )
                ],
                think="他要按引用的那张图改成像素风，参考图传 benchmark 那条消息。",
            ),
            Turn(output=BotReply(mode="done")),
        ],
        quoted=True,
    ),
    Run(
        "ask_review",
        "chat",
        "review",
        [
            Turn(
                output=_reply(
                    "查别人手机号",
                    "我帮不了查同事的手机号：这是别人的个人信息，没有对方同意不该打听。需要联系的话可以问他本人，"
                    "或者通过公司通讯录、行政找。",
                )
            )
        ],
        output_verdict=("Controversial", 0.62, "review", ["隐私"]),
    ),
    Run(
        "ask_reject",
        "chat",
        "rejected",
        [Turn(output=_reply("阴阳老王", "（这段回复因为有人身攻击被输出审核拦下，不会发出）"))],
        output_verdict=("Unsafe", 0.91, "drop", ["辱骂"]),
    ),
    Run("ask_blocked", "chat", "blocked_input"),
    Run(
        "ask_fail",
        "chat",
        "failed",
        [
            Turn([("web_search", {"query": "PEP 779 status"})]),
            Turn([("read_url", {"url_or_handle": "https://example.com/missing/pep-0779"})]),
            Turn(fail="overloaded"),
        ],
        error="ModelHTTPError: status_code: 529, model_name: example/chat-large, body: overloaded",
    ),
    Run(
        "ask_cancel",
        "chat",
        "cancelled",
        [Turn([("read_file", {"handle": "file#1"})], say="我打开表格看看。")],
    ),
    Run("ask_rl", "chat", "ratelimited"),
    Run(
        "ops_reviews",
        "ops",
        "reply",
        [
            Turn([("list_reviews", {})]),
            Turn(output=OpsReply(mode="reply", text="现在有 1 条待审核：老王想查同事的手机号。")),
        ],
    ),
    Run(
        "ops_rate",
        "ops",
        "done",
        [
            Turn([("get_settings", {"group_id": TECH, "section": "ratelimit"})]),
            Turn([("update_settings", {"patch": {"ratelimit": {"user_per_minute": 6}}, "group_id": TECH})]),
            Turn(output=OpsReply(mode="done")),
        ],
    ),
    Run("ops_handoff", "ops", "handoff", [Turn(output=OpsReply(mode="handoff"))]),
    Run("ask_running", "chat", "running"),
    Run(
        "g_tldr",
        "tldr",
        "ok",
        [
            Turn(
                output=_reply(
                    "第七赛季更新",
                    "新地图潮汐港进排位；狙击开镜变慢、冲锋枪腰射更准；加了双人模式（段位单算）；"
                    "修了楼梯换弹卡住；通行证加到 40 级；维护改到周二凌晨 4-6 点。",
                )
            )
        ],
        group_id=GAME,
        quoted=True,
        output_verdict=SAFE,
        text_output=True,
    ),
    Run(
        "g_meta",
        "chat",
        "ok",
        [
            Turn([("lookup_glossary", {"term": "开服"})]),
            Turn(output=_reply("今晚开服时间", "新赛季一般是第一天晚上八点开服，以游戏内公告为准。")),
        ],
        group_id=GAME,
        full=False,
        output_verdict=SAFE,
    ),
]

PRIVATE_OPS = Run(
    "private",
    "ops",
    "done",
    [
        Turn([("update_settings", {"patch": {"ratelimit": {"user_per_day": 120}}, "group_id": GAME})]),
        Turn(output=OpsReply(mode="done")),
    ],
    group_id=None,
    user_id=SUPERUSER,
)


def _resolve_args(args: dict[str, Any], keys: dict[str, Message]) -> dict[str, Any]:
    def one(v: Any) -> Any:
        if isinstance(v, MsgId):
            return f"#{keys[v.key].id}" if v.hash else keys[v.key].id
        if isinstance(v, list):
            return [one(x) for x in v]
        return v

    return {k: one(v) for k, v in args.items()}


def _model(run: Run, turns: list[Turn], model: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        idx = sum(isinstance(m, ModelResponse) for m in messages)
        turn = turns[idx]
        if turn.fail:
            raise ModelHTTPError(529, model, turn.fail)
        parts: list[ModelResponsePart] = []
        if turn.think:
            parts.append(ThinkingPart(turn.think))
        if turn.say:
            parts.append(TextPart(turn.say))
        parts += [
            ToolCallPart(name, args, tool_call_id=f"call_{run.key}_{idx}_{j}")
            for j, (name, args) in enumerate(turn.calls)
        ]
        if turn.output is not None and run.text_output:
            parts.append(TextPart(turn.output.model_dump_json(exclude_defaults=True)))
        elif turn.output is not None:
            out = turn.output.model_dump(mode="json", exclude_defaults=True)
            parts.append(ToolCallPart(info.output_tools[0].name, out, tool_call_id=f"call_{run.key}_{idx}_out"))
        written = len(turn.think) + len(turn.say) + sum(len(json.dumps(a, ensure_ascii=False)) for _, a in turn.calls)
        if turn.output is not None:
            written += len(turn.output.model_dump_json())
        usage = RequestUsage(input_tokens=2600 + 900 * idx, output_tokens=40 + written, cache_read_tokens=2000 * idx)
        return ModelResponse(parts=parts, usage=usage)

    return FunctionModel(respond, model_name=model)


def _event(trigger: Message, quoted: Message | None) -> GroupMessageEvent:
    def message(segments: list[dict[str, Any]]) -> OneBotMessage:
        return OneBotMessage([MessageSegment(s["type"], s["data"]) for s in segments if s["type"] != "reply"])

    data: dict[str, Any] = {
        "time": int(trigger.sent_at.timestamp()),
        "self_id": SELF,
        "post_type": "message",
        "sub_type": "normal",
        "user_id": trigger.user_id,
        "message_type": "group",
        "message_id": trigger.qq_message_id,
        "message": message(trigger.segments),
        "original_message": message(trigger.segments),
        "raw_message": "",
        "font": 0,
        "sender": {"user_id": trigger.user_id, "card": trigger.sender_name},
        "to_me": True,
        "group_id": trigger.group_id,
    }
    if quoted is not None:
        data["reply"] = {
            "time": int(quoted.sent_at.timestamp()),
            "message_type": "group",
            "message_id": quoted.qq_message_id,
            "real_id": quoted.qq_message_id,
            "sender": {"user_id": quoted.user_id, "nickname": quoted.sender_name},
            "message": message(quoted.segments),
        }
    return GroupMessageEvent.model_validate(data)


def _request_text(m: Message) -> str:
    return "".join(s["data"]["text"] for s in m.segments if s["type"] == "text").strip()


def _qq(m: Message) -> QQMessage:
    return store.qq_from_row(m)


@contextmanager
def _as_of(when: datetime) -> Generator[None]:
    """``build_context`` reads "recent" chat relative to now; the seed replays past triggers, so answer as of then."""
    real = search.recent_messages

    async def recent(group_id: int, *, limit: int = 40, minutes: int = 0) -> list[MessageView]:
        earlier = [m for m in await real(group_id, limit=300, minutes=24 * 60) if m.sent_at <= when]
        cutoff = when - timedelta(minutes=minutes)
        in_window = next((i for i, m in enumerate(earlier) if m.sent_at >= cutoff), len(earlier))
        return earlier[min(max(0, len(earlier) - limit), in_window) :]

    search.recent_messages = recent
    try:
        yield
    finally:
        search.recent_messages = real


async def _prompt(run: Run, chat: Chat | None, trigger: Message | None, notes: TraceNotes) -> tuple[str, str]:
    """``(instructions, prompt)`` for the run, built like the real handlers build them."""
    now = NOW.astimezone().strftime("%Y-%m-%d %H:%M %A")
    nonce = secrets.token_hex(3)
    if run.handler == "ops":
        where = f"群「{GROUPS[run.group_id]}」（{run.group_id}）" if run.group_id is not None else "私聊"
        if trigger is not None:
            text, who, asker = _request_text(trigger), _who(trigger), _qq(trigger)
        else:
            text, who = PRIVATE_TEXT, f"{MEMBERS[run.user_id]}({run.user_id})"
            asker = QQMessage(user_id=run.user_id, sender_name=MEMBERS[run.user_id], segments=[_t(text)])
        notes.context = QQContextDisplay(trigger=asker, blocks=["请求"])
        instructions = render("ops_system", now=now, where=where, in_group=run.group_id is not None, nonce=nonce)
        return instructions, f'<请求-{nonce} 发送者="{who}">\n{text}\n</请求-{nonce}>'
    settings = Settings()
    tool_names = [t.__name__ for t in CHAT_TOOLS]
    if run.handler == "report":
        prompt = run.prompt
    else:
        assert chat is not None
        assert trigger is not None
        quoted = chat.quoted(trigger) if run.quoted else None
        target = "quoted_or_self" if run.quoted else "self"
        resolved = Resolved(run.handler, 0, "", (), target, 0, False)
        request_text = "" if run.handler == "tldr" else _request_text(trigger)
        with _as_of(trigger.sent_at):
            ctx = await build_context(
                _event(trigger, quoted), resolved, settings, self_id=SELF, bot_name=PERSONA, request_text=request_text
            )
        notes.context = ctx.display
        prompt, nonce = ctx.prompt, ctx.nonce
    instructions = render(
        f"{'chat' if run.handler == 'report' else run.handler}_system",
        persona=settings.persona,
        group_name=GROUPS.get(run.group_id or 0, "本群"),
        now=now,
        tools=tool_names,
        sandbox=None,
        sandbox_links=False,
        actions=["recall_message"],
        recall_denied=False,
        report=run.handler == "report",
        style=render("style_rules"),
        data_rules=render("data_rules", nonce=nonce, me=self_label(PERSONA), sandbox=False),
    )
    return instructions, prompt


def _verdict(stage: str, v: Verdict, model: str = "example/guard") -> dict[str, Any]:
    verdict, score, action, categories = v
    latency = random.Random(stage + verdict).randint(180, 900)
    return {
        "stage": stage,
        "verdict": verdict,
        "score": score,
        "categories": categories,
        "action": action,
        "model": model,
        "latency_ms": latency,
    }


def _retime(entries: list[dict[str, Any]], start: datetime, rng: random.Random) -> datetime:
    """Spread the recorded entries over a plausible timeline (the scripted run itself took milliseconds)."""
    t = start
    for e in entries:
        if e["kind"] == "assistant":
            written = sum(int(p.get("chars", 0)) for p in e.get("parts", []))
            ms = rng.randint(900, 2400) + 18 * min(written, 600)
            t += timedelta(milliseconds=ms)
            e["duration_ms"] = ms
        elif e["kind"] == "tool":
            ms = rng.randint(150, 2600) if e.get("outcome") != "cancelled" else rng.randint(4000, 9000)
            t += timedelta(milliseconds=ms)
            e["duration_ms"] = ms
        elif e["kind"] == "event":
            t += timedelta(milliseconds=rng.randint(200, 900))
        e["at"] = t.isoformat()
    return t


async def _invoke(run: Run, chats: dict[int, Chat], report_job: Job | None = None) -> UUID:
    chat = chats.get(run.group_id) if run.group_id is not None else None
    trigger = chat.keys[run.key] if chat is not None and run.key in chat.keys else None
    if trigger is not None:
        start = trigger.sent_at + timedelta(milliseconds=600)
    elif report_job is not None and report_job.ended_at is not None:
        start = datetime.fromtimestamp(report_job.ended_at, UTC) + timedelta(seconds=4)
    else:
        start = NOW - timedelta(minutes=45)
    user_id = trigger.user_id if trigger is not None else run.user_id
    rng = random.Random(run.key)
    notes = TraceNotes(full=run.full, bot=(SELF, PERSONA))
    recorder = TranscriptRecorder()
    output: BaseModel | None = None
    error = run.error
    model = FAST if run.handler == "ops" else MODEL
    ran = run.status not in ("blocked_input", "ratelimited", "running")
    if ran:
        instructions, prompt = await _prompt(run, chat, trigger, notes)
        keys = chat.keys if chat else {}
        turns = [replace(t, calls=[(n, _resolve_args(a, keys)) for n, a in t.calls]) for t in run.turns]
        output_type = OpsReply if run.handler == "ops" else BotReply
        agent: Agent[SeedDeps, Any] = Agent(
            _model(run, turns, model),
            output_type=PromptedOutput(output_type) if run.text_output else output_type,
            deps_type=SeedDeps,
            instructions=instructions,
            tools=OPS_TOOLS if run.handler == "ops" else CHAT_TOOLS,
            capabilities=[recorder],
            name=run.handler,
            retries=2,
        )
        target = chat.quoted(trigger) if chat is not None and trigger is not None and run.quoted else None
        deps = SeedDeps(notes, chat or Chat(0), before=start, target=target)
        task = asyncio.create_task(agent.run(prompt, deps=deps))
        if run.status == "cancelled":
            await asyncio.sleep(0.2)
            task.cancel()
        try:
            output = (await task).output
        except asyncio.CancelledError:
            pass
        except ModelHTTPError:
            assert run.status == "failed"

    responses = [m for m in recorder.history if isinstance(m, ModelResponse)]
    inv = Invocation(
        id=uuid4(),
        created_at=start,
        group_id=run.group_id,
        user_id=user_id,
        handler=run.handler,
        trigger_qq_message_id=trigger.qq_message_id if trigger else None,
        request_text=_request_text(trigger) if trigger else report_job.title if report_job else PRIVATE_TEXT,
        status=run.status,
        models=[model] if ran else [],
        tokens_in=sum(r.usage.input_tokens for r in responses),
        tokens_out=sum(r.usage.output_tokens for r in responses),
        tokens_cached=sum(r.usage.cache_read_tokens for r in responses),
        tool_calls=dict(Counter(t.name for t in recorder.tools.values())),
        ack_kind="react" if trigger is not None and run.status != "ratelimited" else None,
        ack_latency_ms=rng.randint(300, 900) if trigger is not None and run.status != "ratelimited" else None,
        input_verdict=_verdict("input", ("Unsafe", 0.97, "drop", ["违法"]) if run.status == "blocked_input" else SAFE)
        if run.handler in ("chat", "tldr") and run.status not in ("ratelimited", "running")
        else None,
        output_verdict=_verdict("output", run.output_verdict) if run.output_verdict else None,
        reply=output.model_dump(mode="json") if output is not None else None,
        trace_id=TRACE_PREFIX + secrets.token_hex(14),  # the prefix marks seeded rows for reset
        error=error,
    )
    async with get_session() as s:
        s.add(inv)
    end = start + timedelta(milliseconds=rng.randint(400, 1200))
    if ran:
        await store.save_invocation(inv.id, recorder, notes, run.group_id)
        row = await store.for_invocation(inv.id)
        assert row is not None
        entries = list(row.entries)
        end = _retime(entries, start + timedelta(milliseconds=200), rng)
        async with get_session() as s:
            await s.execute(
                update(Transcript)
                .where(Transcript.id == row.id)
                .values(entries=entries, created_at=start, updated_at=end)
            )
    duration = int((end - start).total_seconds() * 1000) + (900 if ran else 0)
    reply_qq: int | None = None
    if chat is not None and isinstance(output, BotReply) and run.status == "ok":
        sent = await _bot_message(
            chat, start + timedelta(milliseconds=duration), output.title or "", [s.content for s in output.sections]
        )
        reply_qq = sent.qq_message_id
    async with get_session() as s:
        await s.execute(
            update(Invocation)
            .where(Invocation.id == inv.id)
            .values(duration_ms=None if run.status == "running" else duration, reply_qq_message_id=reply_qq)
        )
        if inv.input_verdict:
            s.add(_moderation(inv.id, inv.input_verdict, start + timedelta(milliseconds=350)))
        if inv.output_verdict:
            s.add(_moderation(inv.id, inv.output_verdict, end + timedelta(milliseconds=300)))
    return inv.id


def _moderation(invocation_id: UUID, v: dict[str, Any], at: datetime) -> ModerationResult:
    return ModerationResult(
        created_at=at,
        invocation_id=invocation_id,
        stage=v["stage"],
        verdict=v["verdict"],
        score=v["score"],
        categories=v["categories"],
        action=v["action"],
        model=v["model"],
        latency_ms=v["latency_ms"],
    )


# --- other tables -----------------------------------------------------------------------------------------------


async def _base() -> None:
    async with get_session() as s:
        s.add_all(Group(group_id=g, name=name, enabled=True) for g, name in GROUPS.items())
        for g in GROUPS:
            s.add_all(
                UserAlias(group_id=g, user_id=u, name=name, first_seen=NOW - timedelta(days=60), last_seen=NOW)
                for u, name in MEMBERS.items()
            )
        s.add(
            UserAlias(
                group_id=TECH,
                user_id=20003,
                name="王工",
                first_seen=NOW - timedelta(days=90),
                last_seen=NOW - timedelta(days=30),
            )
        )
        provider = Provider(name=PROVIDER, base_url="https://api.example.com/v1", models=[MODEL, FAST])
        s.add(provider)
        await s.flush()
        for task, model in (("chat", MODEL), ("tldr", MODEL), ("ops", FAST), ("quick_ack", FAST), ("vision", MODEL)):
            await s.execute(
                pg_insert(TaskRoute)
                .values(task=task, provider_id=provider.id, model=model)
                .on_conflict_do_nothing(index_elements=["task"])
            )


async def _stickers() -> None:
    specs: list[dict[str, Any]] = [
        {
            "name": "doge",
            "status": "ready",
            "frames": 3,
            "description": "一只眯眼笑的狗头",
            "usage": "调侃、表示「懂的都懂」",
            "emotions": ["调侃", "得意"],
            "usage_by_group": {TECH: 12},
        },
        {"name": "cat", "status": "untagged", "frames": 1, "usage_by_group": {GAME: 3}},
    ]
    async with get_session() as s:
        for spec in specs:
            media = MEDIA[spec["name"]]
            img = Image.open(io.BytesIO(media.data))
            sticker = Sticker(
                kind="image",
                ref=media.md5,
                summary="动画表情",
                status=spec["status"],
                mime=media.mime,
                size_bytes=len(media.data),
                width=img.width,
                height=img.height,
                frames=spec["frames"],
                description=spec.get("description", ""),
                usage=spec.get("usage", ""),
                emotions=spec.get("emotions", []),
                tagged_at=NOW - timedelta(hours=2) if spec["status"] == "ready" else None,
                seen_count=sum(spec["usage_by_group"].values()),
            )
            s.add(sticker)
            await s.flush()
            s.add(StickerBlob(sticker_id=sticker.id, data=media.data))
            s.add_all(
                StickerUsage(sticker_id=sticker.id, group_id=g, count=n, last_seen=NOW)
                for g, n in spec["usage_by_group"].items()
            )
        broken = Sticker(kind="image", ref=_broken_ref(), summary="动画表情", status="failed", error="too_large")
        mface = _mface()["data"]
        happy = Sticker(
            kind="mface",
            ref=f"{mface['emoji_package_id']}:{mface['emoji_id']}",
            emoji_id=mface["emoji_id"],
            package_id=mface["emoji_package_id"],
            key=mface["key"],
            summary="开心",
            seen_count=5,
        )
        s.add_all([broken, happy])
        await s.flush()
        s.add(StickerUsage(sticker_id=happy.id, group_id=TECH, count=5, last_seen=NOW))


def _broken_ref() -> str:
    return hashlib.md5(b"seed-broken-sticker").hexdigest().upper()


async def _memory(chats: dict[int, Chat]) -> None:
    tech, game = chats[TECH].keys, chats[GAME].keys

    def ids(keys: dict[str, Message], *names: str) -> list[int]:
        return [keys[n].id for n in names]

    async with get_session() as s:
        closed = Topic(
            group_id=TECH,
            title="Python 3.14 free-threading 性能",
            summary="老王贴了 benchmark：单线程慢 5%，8 线程快 5 倍；Neko 确认是关掉 GIL 跑的。",
            keywords=["free-threading", "GIL", "benchmark"],
            participants=[20001, 20002, 20003, 20004],
            start_at=tech["hello"].sent_at,
            end_at=tech["gil_ans"].sent_at,
            first_msg_id=tech["hello"].id,
            last_msg_id=tech["gil_ans"].id,
            msg_count=6,
            status="closed",
            closed_at=tech["gil_ans"].sent_at + timedelta(hours=1),
        )
        meetup = Topic(
            group_id=TECH,
            title="周末线下聚会",
            summary="聚会改到周日下午，地点不变；老王会带「鹈鹕」（他的旧服务器）。",
            keywords=["聚会", "周日"],
            participants=[20001, 20002, 20003, 20005],
            start_at=tech["meetup"].sent_at,
            end_at=tech["pelican2"].sent_at,
            first_msg_id=tech["meetup"].id,
            last_msg_id=tech["pelican2"].id,
            msg_count=4,
        )
        season = Topic(
            group_id=GAME,
            title="第七赛季开荒",
            summary="Neko 招人今晚一起打新赛季，阿明贴了更新说明。",
            keywords=["新赛季", "更新"],
            participants=[20001, 20004, 20005],
            start_at=game["g_hi"].sent_at,
            end_at=game["g_long"].sent_at,
            first_msg_id=game["g_hi"].id,
            last_msg_id=game["g_long"].id,
            msg_count=4,
        )
        s.add_all([closed, meetup, season])
        await s.flush()
        members = {
            closed.id: ids(tech, "hello", "ft", "bench", "bench_note", "ask_gil", "gil_ans"),
            meetup.id: ids(tech, "meetup", "meetup2", "pelican", "pelican2"),
            season.id: ids(game, "g_hi", "g_me", "g_shot", "g_long"),
        }
        s.add_all(TopicMessage(topic_id=t, message_id=m) for t, ms in members.items() for m in ms)
        s.add(
            DailyDigest(
                group_id=TECH,
                day=(NOW - timedelta(days=1)).astimezone().date(),
                summary="昨天群里主要聊了 Python 3.14 的发布和 free-threading 的 benchmark，顺便约了周末聚会。",
                topic_ids=[closed.id],
            )
        )
        s.add_all(
            [
                GlossaryTerm(
                    group_id=TECH,
                    term="鹈鹕",
                    definition="老王那台旧服务器的名字",
                    examples=[{"msg_id": tech["pelican2"].id, "text": "鹈鹕是老王给他那台旧服务器起的名字"}],
                    first_seen=tech["meetup2"].sent_at,
                    last_seen=tech["pelican2"].sent_at,
                    hits=3,
                    confidence=0.8,
                ),
                GlossaryTerm(
                    group_id=TECH,
                    term="3.14t",
                    aliases=["free-threaded 构建"],
                    definition="Python 3.14 去掉 GIL 的构建",
                    hits=5,
                    confidence=1.0,
                    locked=True,
                    source="admin",
                ),
                GlossaryCandidate(
                    group_id=TECH,
                    term="开摆",
                    count=4,
                    first_seen=NOW - timedelta(days=3),
                    last_seen=NOW - timedelta(hours=5),
                    example_msg_ids=ids(tech, "ft"),
                ),
                GlossaryCandidate(
                    group_id=GAME,
                    term="潮汐港",
                    count=2,
                    first_seen=NOW - timedelta(hours=2),
                    last_seen=NOW - timedelta(hours=2),
                    status="rejected",
                ),
            ]
        )
        s.add_all(
            [
                UserProfile(
                    group_id=TECH,
                    user_id=20003,
                    display_name="老王",
                    summary="后端工程师，喜欢跑 benchmark，有台叫「鹈鹕」的旧服务器",
                    interests=["Python 性能", "自建服务器"],
                    traits=["较真", "爱分享数据"],
                    last_closed_at=closed.closed_at,
                    last_topic_id=closed.id,
                ),
                UserProfile(
                    group_id=TECH,
                    user_id=20001,
                    display_name="阿明",
                    summary="群主，负责组织线下聚会",
                    interests=["Python", "聚会"],
                    traits=["热心"],
                ),
                UserProfile(group_id=GAME, user_id=20004, display_name="Neko", opted_out=True),
                UserProfileFact(
                    group_id=TECH,
                    user_id=20003,
                    fact="在 3.14t 上跑过多线程 benchmark",
                    source_msg_ids=ids(tech, "bench_note"),
                ),
            ]
        )
        s.add(
            MessageChunk(
                group_id=TECH,
                first_msg_id=tech["hello"].id,
                last_msg_id=tech["gil_ans"].id,
                start_at=tech["hello"].sent_at,
                end_at=tech["gil_ans"].sent_at,
                msg_count=6,
                text=_lines([m for m in chats[TECH].rows if m.id in members[closed.id]]),
            )
        )


async def _events() -> None:
    def ev(kind: str, at: float, **kw: Any) -> BotEvent:
        return BotEvent(kind=kind, at=NOW - timedelta(minutes=at), **kw)

    async with get_session() as s:
        s.add_all(
            [
                ev(
                    "request.group.invite",
                    90,
                    group_id=10009,
                    user_id=20002,
                    data={"request_type": "group", "sub_type": "invite", "comment": "", "flag": "seed-flag-1"},
                    status="pending",
                ),
                ev(
                    "request.group.add",
                    60,
                    group_id=TECH,
                    user_id=20006,
                    data={"request_type": "group", "sub_type": "add", "comment": "朋友推荐来的", "flag": "seed-flag-2"},
                    status="approved",
                    handled_by=f"qq:{SUPERUSER}",
                    handled_at=NOW - timedelta(minutes=58),
                ),
                ev(
                    "request.friend",
                    300,
                    user_id=20005,
                    data={"request_type": "friend", "comment": "加个好友", "flag": "seed-flag-3"},
                    status="expired",
                ),
                ev(
                    "notice.group_ban.ban",
                    45,
                    group_id=TECH,
                    user_id=20005,
                    operator_id=20003,
                    data={"notice_type": "group_ban", "sub_type": "ban", "duration": 600},
                ),
                ev(
                    "notice.group_decrease.kick",
                    200,
                    group_id=GAME,
                    user_id=20006,
                    operator_id=20001,
                    data={"notice_type": "group_decrease", "sub_type": "kick"},
                ),
            ]
        )


async def _reviews(review_inv: UUID) -> None:
    def item(status: str, minutes: float, text: str, verdict: Verdict, **kw: Any) -> ReviewItem:
        return ReviewItem(
            created_at=NOW - timedelta(minutes=minutes),
            group_id=TECH,
            user_id=20003,
            request_text=text,
            reply=_reply("回复", "（示例回复）" + text).model_dump(mode="json"),
            verdict=_verdict("output", verdict),
            status=status,
            expires_at=NOW - timedelta(minutes=minutes) + timedelta(hours=24),
            **kw,
        )

    async with get_session() as s:
        pending = await s.scalar(select(Invocation).where(Invocation.id == review_inv))
        assert pending is not None
        s.add_all(
            [
                ReviewItem(
                    created_at=pending.created_at,
                    invocation_id=review_inv,
                    group_id=TECH,
                    user_id=pending.user_id,
                    trigger_qq_message_id=pending.trigger_qq_message_id,
                    request_text=pending.request_text,
                    reply=pending.reply or {},
                    verdict=pending.output_verdict or {},
                    expires_at=pending.created_at + timedelta(hours=24),
                ),
                item(
                    "approved",
                    600,
                    "公司内网怎么连",
                    ("Controversial", 0.55, "review", ["网络安全"]),
                    decided_by=ADMIN,
                    decided_at=NOW - timedelta(minutes=590),
                ),
                item(
                    "rejected",
                    900,
                    "帮我编一个请假理由",
                    ("Controversial", 0.58, "review", ["欺骗"]),
                    decided_by=f"qq:{SUPERUSER}",
                    decided_at=NOW - timedelta(minutes=880),
                ),
                ReviewItem(
                    created_at=NOW - timedelta(days=2),
                    group_id=GAME,
                    user_id=20005,
                    request_text="外挂哪里买",
                    reply=_reply("外挂", "（示例回复）外挂会封号").model_dump(mode="json"),
                    verdict=_verdict("output", ("Controversial", 0.66, "review", ["违规"])),
                    status="expired",
                    expires_at=NOW - timedelta(days=1),
                ),
            ]
        )


# --- jobs -------------------------------------------------------------------------------------------------------


def _job(
    suffix: str, kind: Literal["command", "agent"], title: str, spec: str, *, hours: float, minutes: float, **kw: Any
) -> Job:
    created = (NOW - timedelta(hours=hours)).timestamp()
    ended = created + minutes * 60 if kw.get("status", "succeeded") not in ("running", "starting") else None
    defaults: dict[str, Any] = {
        "group_id": TECH,
        "user_id": 20003,
        "trigger_message_id": None,
        "invocation_id": None,
        "request_text": title,
        "workspace": "train" if kind == "command" else "site",
        "claim": f"ygg-sbx-{TECH}-seed",
        "deadline": created + 3600,
        "ended_at": ended,
        "status": "succeeded",
    }
    return Job(id=f"{JOB_PREFIX}{suffix}", kind=kind, title=title, spec=spec, created_at=created, **(defaults | kw))


def _omp_messages(job: Job, *, finished: bool) -> list[dict[str, Any]]:
    """What omp reports for a small site-building session (``message_end`` payloads)."""
    t = int(job.created_at * 1000) + 3000
    shot = base64.b64encode(MEDIA["screenshot"].data).decode()
    msgs: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "先建一个静态站点目录，写首页，再起个服务器截图。"},
                {
                    "type": "toolCall",
                    "id": "toolu_1",
                    "name": "bash",
                    "arguments": {
                        "command": "mkdir -p site && cd site && cat > index.html <<'EOF'\n<h1>鹈鹕</h1>\nEOF"
                    },
                },
            ],
            "model": MODEL,
            "usage": {"input": 5200, "output": 180, "cacheRead": 0},
            "stopReason": "toolUse",
            "timestamp": t,
            "duration": 4200,
        },
        {
            "role": "toolResult",
            "toolCallId": "toolu_1",
            "toolName": "bash",
            "content": [{"type": "text", "text": ""}],
            "isError": False,
            "timestamp": t + 4800,
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "首页写好了，起个服务器截图看看效果。"},
                {
                    "type": "toolCall",
                    "id": "toolu_2",
                    "name": "browser",
                    "arguments": {"action": "screenshot", "url": "http://127.0.0.1:8000/"},
                },
            ],
            "model": MODEL,
            "usage": {"input": 5600, "output": 90, "cacheRead": 5000},
            "stopReason": "toolUse",
            "timestamp": t + 5000,
            "duration": 2600,
        },
        {
            "role": "toolResult",
            "toolCallId": "toolu_2",
            "toolName": "browser",
            "content": [{"type": "image", "data": shot, "mimeType": "image/png"}],
            "isError": False,
            "timestamp": t + 11000,
        },
    ]
    if finished:
        msgs += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "toolu_3",
                        "name": "show",
                        "arguments": {"path": "/workspace/site/shot.png", "caption": "首页截图"},
                    }
                ],
                "model": MODEL,
                "usage": {"input": 7400, "output": 60, "cacheRead": 5500},
                "stopReason": "toolUse",
                "timestamp": t + 11500,
                "duration": 1800,
            },
            {
                "role": "toolResult",
                "toolCallId": "toolu_3",
                "toolName": "show",
                "content": [{"type": "text", "text": "已记下这张图，会附在完成报告里"}],
                "isError": False,
                "timestamp": t + 13500,
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "完成了：/workspace/site 下是一个单页站点，首页截图已附上。"}],
                "model": MODEL,
                "usage": {"input": 7600, "output": 40, "cacheRead": 7300},
                "stopReason": "stop",
                "timestamp": t + 14000,
                "duration": 1500,
            },
        ]
    return msgs


async def _transcribe(job: Job, messages: list[dict[str, Any]]) -> None:
    """Write a job's transcript the way ``JobRunner`` does: the rules and the task, then omp's messages."""
    at = datetime.fromtimestamp(job.created_at, UTC)
    opening = [
        Entry(kind="system", at=at, parts=[text_part(render_rules(Settings(), job, links=False), True)]),
        Entry(kind="user", at=at, parts=[text_part(job.spec, True)]),
    ]
    await store.append_job(job.id, opening, {}, full=True)
    starts: dict[str, float] = {}
    for msg in messages:
        blobs: dict[str, Blob] = {}
        await store.append_job(
            job.id, message_entries(msg, full=True, blobs=blobs, tool_starts=starts), blobs, full=True
        )


async def _jobs(chats: dict[int, Chat]) -> None:
    train = _job(
        "train",
        "command",
        "训练小模型",
        "python train.py --epochs 3",
        hours=5,
        minutes=12,
        exit_code=0,
        result="epoch 1 loss 0.82\nepoch 2 loss 0.51\nepoch 3 loss 0.33\n模型已保存到 /workspace/runs/model.pt",
        report="done",
    )
    crawl = _job(
        "crawl",
        "command",
        "抓取更新日志",
        "python crawl.py https://example.com/changelog",
        hours=4,
        minutes=1,
        status="failed",
        exit_code=1,
        error="退出码 1",
        result='Traceback (most recent call last):\n  File "crawl.py", line 12\nConnectionError: example.com refused',
        report="done",
    )
    site = _job(
        "site",
        "agent",
        "鹈鹕网站",
        "搭一个介绍「鹈鹕」服务器的单页网站并截图",
        hours=3,
        minutes=6,
        model=MODEL,
        result="完成了：/workspace/site 下是一个单页站点，首页截图已附上。",
        images=[{"path": "/workspace/site/shot.png", "caption": "首页截图"}],
        usage={"requests": 4, "input_tokens": 25800, "cached_tokens": 17800, "output_tokens": 370},
        report="done",
    )
    scrape = _job(
        "scrape",
        "agent",
        "整理群聊表情包",
        "把最近一周的表情包整理成一个画廊页面",
        hours=2,
        minutes=3,
        status="cancelled",
        model=MODEL,
        cancel=f"admin:{ADMIN}",
        error="已取消",
        usage={"requests": 2, "input_tokens": 10800, "cached_tokens": 5000, "output_tokens": 270},
        report="done",
    )
    report = Run(
        "report",
        "report",
        "ok",
        [
            Turn(
                output=_reply(
                    "鹈鹕网站做好了",
                    "老王要的单页网站做好了，放在工作区 site 的 /workspace/site 下，首页截图附在后面。",
                )
            )
        ],
        user_id=site.user_id,
        prompt=f"后台任务「{site.title}」已完成。\n任务：{site.spec}\n结果：{site.result}\n请向发起人简短汇报。",
        output_verdict=SAFE,
    )
    site.report_invocation = str(await _invoke(report, chats, report_job=site))

    def events(job: Job, *texts: str) -> list[JobEvent]:
        end = job.ended_at or NOW.timestamp()
        step = (end - job.created_at) / max(1, len(texts))
        return [JobEvent(at=job.created_at + i * step, text=t) for i, t in enumerate(texts)]

    for job, log in (
        (train, events(train, "启动", "进度：epoch 1/3", "进度：epoch 2/3", "进度：epoch 3/3", "已完成")),
        (crawl, events(crawl, "启动", "失败：退出码 1")),
        (
            site,
            events(
                site,
                "开始工作",
                "bash: mkdir -p site",
                "browser: screenshot",
                "show /workspace/site/shot.png",
                "已完成",
            ),
        ),
        (scrape, events(scrape, "开始工作", "bash: ls stickers", "请求取消（admin）", "已取消")),
    ):
        await archive.archive_job(job, log)
    await _transcribe(site, _omp_messages(site, finished=True))
    await _transcribe(scrape, _omp_messages(scrape, finished=False)[:2])

    live = _job(
        "live",
        "agent",
        "给聚会做个报名页",
        "做一个周日聚会的报名页，能填名字和人数",
        hours=0.1,
        minutes=0,
        status="running",
        user_id=20001,
        model=MODEL,
        progress="首页写好了，正在截图",
        deadline=(NOW + timedelta(minutes=40)).timestamp(),
    )
    await _transcribe(live, _omp_messages(live, finished=False))
    try:
        r = redis_client()
        await r.hset(job_key(live.id), mapping=live.to_hash())  # pyright: ignore[reportArgumentType, reportGeneralTypeIssues]
        await r.hset(running_key(live.group_id), live.id, live.kind)  # pyright: ignore[reportGeneralTypeIssues]
        await r.zadd(group_key(live.group_id), {live.id: live.created_at})
        await r.sadd(ACTIVE_KEY, live.id)  # pyright: ignore[reportGeneralTypeIssues]
        for e in events(live, "开始工作", "bash: mkdir -p site", "进度：首页写好了，正在截图"):
            await r.lpush(events_key(live.id), f"{e.at!r}\t{e.text}")  # pyright: ignore[reportGeneralTypeIssues]
    except Exception as e:  # the rest of the seed is still useful without Redis
        sys.stderr.write(f"redis unavailable, skipped the running job: {e}\n")


# --- reset ------------------------------------------------------------------------------------------------------

JOB_IDS = [f"{JOB_PREFIX}{s}" for s in ("train", "crawl", "site", "scrape", "live")]


async def reset() -> None:
    groups = list(GROUPS)
    seeded_events = or_(BotEvent.group_id.in_(groups), BotEvent.data["flag"].astext.startswith(FLAG_PREFIX))
    async with get_session() as s:
        await s.execute(delete(Transcript).where(Transcript.job_id.in_(JOB_IDS)))
        await s.execute(
            delete(Invocation).where(or_(Invocation.group_id.in_(groups), Invocation.trace_id.startswith(TRACE_PREFIX)))
        )
        await s.execute(delete(ReviewItem).where(ReviewItem.group_id.in_(groups)))
        await s.execute(delete(BotEvent).where(seeded_events))
        await s.execute(delete(SandboxJob).where(SandboxJob.id.in_(JOB_IDS)))
        await s.execute(delete(Message).where(Message.group_id.in_(groups)))
        await s.execute(delete(UserAlias).where(UserAlias.group_id.in_(groups)))
        await s.execute(delete(MessageChunk).where(MessageChunk.group_id.in_(groups)))
        await s.execute(delete(Topic).where(Topic.group_id.in_(groups)))  # topic_messages cascade
        for model in (DailyDigest, GlossaryTerm, GlossaryCandidate, UserProfile, UserProfileFact):
            await s.execute(delete(model).where(model.group_id.in_(groups)))
        refs = [m.md5 for m in MEDIA.values()] + [_broken_ref(), f"230001:{_mface()['data']['emoji_id']}"]
        await s.execute(delete(Sticker).where(Sticker.ref.in_(refs)))  # blobs and usage cascade
        await s.execute(delete(StickerUsage).where(StickerUsage.group_id.in_(groups)))
        shas = [blob_id(m.data) for m in MEDIA.values()]
        await s.execute(delete(TranscriptBlob).where(TranscriptBlob.sha256.in_(shas)))
        provider = await s.scalar(select(Provider.id).where(Provider.name == PROVIDER))
        if provider is not None:
            await s.execute(delete(TaskRoute).where(TaskRoute.provider_id == provider))
            await s.execute(delete(Provider).where(Provider.id == provider))
        await s.execute(delete(Group).where(Group.group_id.in_(groups)))
    try:
        r = redis_client()
        for job_id in JOB_IDS:
            await r.delete(job_key(job_id), events_key(job_id))
            await r.srem(ACTIVE_KEY, job_id)  # pyright: ignore[reportGeneralTypeIssues]
        for g in groups:
            await r.zrem(group_key(g), *JOB_IDS)
            await r.hdel(running_key(g), *JOB_IDS)  # pyright: ignore[reportGeneralTypeIssues]
    except Exception as e:
        sys.stderr.write(f"redis unavailable, skipped clearing the running job: {e}\n")


async def seed() -> None:
    await reset()
    await _base()
    chats = {TECH: await _insert_chat(TECH, TECH_CHAT, 5_000_000), GAME: await _insert_chat(GAME, GAME_CHAT, 6_000_000)}
    await _stickers()
    await _memory(chats)
    await _events()
    ids: dict[str, UUID] = {}
    for run in RUNS:
        ids[run.key] = await _invoke(run, chats)
    await _invoke(PRIVATE_OPS, chats)
    await _reviews(ids["ask_review"])
    await _jobs(chats)


async def _main(only_reset: bool) -> None:
    if APP_CONFIG.environment == "prod":
        raise SystemExit("refusing to seed with YGG_ENV=prod")
    if only_reset:
        await reset()
        sys.stdout.write("removed the seed data\n")
    else:
        await seed()
        sys.stdout.write(f"seeded groups {', '.join(map(str, GROUPS))}: chat, stickers, memory, invocations, jobs\n")
    try:
        from yggdrasil import runtime

        await runtime.publish_change("seed")  # a running console reloads its group list
    except Exception:  # nothing is listening
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--reset", action="store_true", help="only remove the seed data")
    asyncio.run(_main(parser.parse_args().reset))


if __name__ == "__main__":
    main()

"""Scripted group chat covering every QQ segment type, and the writer that inserts it as ``messages`` rows."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.devtools.seed.data import MEDIA, MEMBERS, NOW, PERSONA, SELF, SUPERUSER, mface_segment
from yggdrasil.ingest.normalize import normalize


@dataclass(frozen=True)
class Ref:
    """A reply segment to the line with this key (its QQ id is known once the lines are numbered)."""

    key: str


type Seg = dict[str, Any] | Ref


def text_segment(text: str) -> dict[str, Any]:
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
        _node(20003, "老王", 30, [text_segment("JIT 在 3.14 里还是实验性的，要 --enable-experimental-jit 编译")]),
        _node(20002, "小红", 29, [text_segment("那默认构建里就没有？")]),
    ]
    nodes = [
        _node(20001, "阿明", 40, [text_segment("CPython 的 JIT 现在到什么程度了？")]),
        _node(20003, "老王", 38, [text_segment("copy-and-patch，先把字节码模板拼起来，收益还不大")]),
        _node(20004, "Neko", 35, [_img("bench"), text_segment("这是官方的数字")]),
        _node(20002, "小红", 31, [{"type": "forward", "data": {"id": "seed-inner-1", "content": inner}}]),
    ]
    return {"type": "forward", "data": {"id": "seed-forward-1", "content": nodes}}


def _bot_forward(forward_id: str, sections: list[str]) -> dict[str, Any]:
    nodes = [
        {"type": "node", "data": {"user_id": str(SELF), "nickname": PERSONA, "content": [text_segment(s)]}}
        for s in sections
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


@dataclass
class Line:
    key: str
    minutes: float  # before now
    user: int
    segments: list[Seg]
    recalled: bool = False


TECH_CHAT = [
    Line("hello", 170, 20001, [text_segment("早上好，有人试过 Python 3.14 的 free-threading 构建吗？")]),
    Line("ft", 168, 20002, [text_segment("试了，"), _face(14, "/微笑"), text_segment(" 装的 3.14t，C 扩展还得等适配")]),
    Line("bench", 165, 20003, [_img("bench")]),
    Line("bench_note", 164, 20003, [text_segment("我跑的 benchmark：单线程慢 5% 左右，8 线程快了 5 倍")]),
    Line("ask_gil", 160, 20004, [Ref("bench"), text_segment("多线程那栏是关了 GIL 跑的吗？")]),
    Line("gil_ans", 158, 20003, [_at(20004), text_segment(" 对，PYTHON_GIL=0")]),
    Line("doge", 155, 20005, [_sticker("doge")]),
    Line("happy", 150, 20001, [mface_segment()]),
    Line("card", 140, 20002, [_card("Python 3.14 有哪些新特性", "https://example.com/python-3-14")]),
    Line("fwd", 130, 20004, [_forward()]),
    Line("fwd_note", 129, 20004, [text_segment("上周讨论 JIT 的记录，补课用")]),
    Line("voice", 120, 20005, [SEGMENT_SAMPLES[0]]),
    Line("clip", 115, 20001, [SEGMENT_SAMPLES[1]]),
    Line("file", 110, 20003, [SEGMENT_SAMPLES[2]]),
    Line("poke", 105, 20002, [SEGMENT_SAMPLES[3]]),
    Line("dice", 100, 20005, [SEGMENT_SAMPLES[4]]),
    Line("rps", 99, 20001, [SEGMENT_SAMPLES[5]]),
    Line("md", 90, 20004, [SEGMENT_SAMPLES[6]]),
    Line("oops", 80, 20002, [text_segment("发错群了")], recalled=True),
    Line("meetup", 70, 20001, [_at("all"), text_segment(" 周末聚会改到周日下午，地点不变")]),
    Line("meetup2", 68, 20003, [text_segment("收到，我带鹈鹕去")]),
    Line("pelican", 66, 20005, [text_segment("鹈鹕是谁？")]),
    Line("pelican2", 65, 20002, [_at(20005), text_segment(" 鹈鹕是老王给他那台旧服务器起的名字")]),
    Line(
        "bot_old", 50, SELF, [_bot_forward("seed-old", ["free-threading 构建去掉了 GIL，纯 Python 多线程能用满多核。"])]
    ),
    # Triggers of the scripted invocations (scenarios.RUNS).
    Line("ask_chart", 40, 20002, [_at(SELF), text_segment(" 老王那张图说明了什么？")]),
    Line("ask_fwd", 34, 20004, [Ref("fwd"), _at(SELF), text_segment(" 总结一下这段聊天记录")]),
    Line("fwd_remark", 33, 20002, [Ref("fwd"), text_segment("JIT 那段说得挺清楚")]),
    Line("ask_tldr", 28, 20001, [Ref("card"), text_segment("tldr")]),
    Line("ask_silent", 24, 20005, [_at(SELF), text_segment(" 哈哈哈")]),
    Line("ask_recall", 22, 20001, [Ref("bot_old"), _at(SELF), text_segment(" 撤回这条吧，发错地方了")]),
    Line("ask_review", 19, 20003, [_at(SELF), text_segment(" 帮我查一下某个同事的手机号")]),
    Line("ask_reject", 17, 20005, [_at(SELF), text_segment(" 写一段话阴阳一下老王")]),
    Line("ask_blocked", 15, 20005, [_at(SELF), text_segment(" （示例）一条会被输入审核拦下的请求")]),
    Line("ask_fail", 13, 20004, [_at(SELF), text_segment(" 查一下 PEP 779 现在是什么状态")]),
    Line("ask_draw", 12, 20003, [Ref("bench"), _at(SELF), text_segment(" 把这张图画成像素风海报")]),
    Line("ask_cancel", 11, 20002, [_at(SELF), text_segment(" 帮我看看这个表格"), SEGMENT_SAMPLES[2]]),
    Line("ask_rl", 9, 20005, [_at(SELF), text_segment(" 再来一个")]),
    Line("ask_chain", 8, 20003, [Ref("fwd_remark"), text_segment("tldr")]),
    Line("ops_reviews", 7, SUPERUSER, [_at(SELF), text_segment(" 现在有几条待审核？")]),
    Line("ops_rate", 5, SUPERUSER, [_at(SELF), text_segment(" 把本群每人每分钟的次数调到 6")]),
    Line("ops_handoff", 3, SUPERUSER, [_at(SELF), text_segment(" 3.14t 和 3.14 有什么区别")]),
    Line("ask_running", 0.3, 20004, [_at(SELF), text_segment(" 对比一下 3.13 和 3.14 的启动速度")]),
]

PATCH_NOTES = (
    "第七赛季更新说明：一、新地图「潮汐港」加入排位，地图中央的灯塔每三分钟开放一次；二、狙击类武器开镜速度降低 8%，"
    "冲锋枪腰射散布减小；三、新增双人模式，段位单独计算；四、修复了在楼梯上换弹会卡住的问题；五、赛季通行证新增 "
    "40 级奖励，老赛季的皮肤可以用代币兑换；六、服务器维护时间改为每周二凌晨四点到六点。"
)

GAME_CHAT = [
    Line("g_hi", 120, 20004, [text_segment("新赛季今晚开，谁一起？")]),
    Line("g_me", 118, 20005, [text_segment("我我我"), _face(178, "/斜眼笑")]),
    Line("g_shot", 110, 20004, [_img("map")]),
    Line("g_long", 100, 20001, [text_segment(PATCH_NOTES)]),
    Line("g_cat", 95, 20002, [_sticker("cat")]),
    Line("g_tldr", 30, 20005, [Ref("g_long"), text_segment("tldr")]),
    Line("g_meta", 20, 20004, [_at(SELF), text_segment(" 今晚几点开服？")]),
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


async def insert_chat(group_id: int, lines: list[Line], base: int) -> Chat:
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


async def bot_message(chat: Chat, at: datetime, title: str, sections: list[str]) -> Message:
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


def sender_label(m: Message) -> str:
    return f"{m.sender_name}({m.user_id})"


def format_lines(rows: list[Message]) -> str:
    return "\n".join(
        f"[{m.sent_at.astimezone().strftime('%m-%d %H:%M')}] #{m.id} {sender_label(m)}: {normalize(m.segments).text}"
        for m in rows
    )

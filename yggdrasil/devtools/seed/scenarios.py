"""Scripted invocations covering every handler and outcome, as model turns for the replay."""

import random
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from yggdrasil.agent.schema import BotReply, OpsReply, Section, SourceRef
from yggdrasil.devtools.seed.data import GAME, SUPERUSER, TECH


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


def bot_reply(title: str, *sections: str, sources: list[SourceRef] | None = None) -> BotReply:
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
                output=bot_reply(
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
                output=bot_reply(
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
                output=bot_reply(
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
                output=bot_reply(
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
                output=bot_reply(
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
        [Turn(output=bot_reply("阴阳老王", "（这段回复因为有人身攻击被输出审核拦下，不会发出）"))],
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
                output=bot_reply(
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
            Turn(output=bot_reply("今晚开服时间", "新赛季一般是第一天晚上八点开服，以游戏内公告为准。")),
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


def verdict_record(stage: str, v: Verdict, model: str = "example/guard") -> dict[str, Any]:
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

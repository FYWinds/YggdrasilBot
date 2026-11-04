from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from tests.support.onebot import group_message
from yggdrasil.agent import context
from yggdrasil.agent.deps import Handles
from yggdrasil.ingest.normalize import normalize
from yggdrasil.memory import search
from yggdrasil.memory.search import MessageView
from yggdrasil.people import People, label, mentioned_ids, self_label
from yggdrasil.settings import ContextSettings, Settings
from yggdrasil.triggers.index import Resolved

SELF = 1000000001
FRIEND = 1000000002
PEOPLE = People("耶梦加得", {FRIEND: "阿明"}, frozenset({SELF}))


def test_label_edge_cases() -> None:
    assert label("阿明", FRIEND) == "阿明(1000000002)"
    assert label(None, 12345) == "群友(12345)"
    assert label("  ", 12345) == "群友(12345)"
    assert label('小(明)（二）<b>"x"\n换行 名', 12345) == "小明二bx换行 名(12345)"
    assert self_label("耶梦加得") == "耶梦加得(你)"


def test_who() -> None:
    assert PEOPLE.who(FRIEND, "旧名片") == "阿明(1000000002)"
    assert PEOPLE.who(10001, "新人") == "新人(10001)"
    assert PEOPLE.who(10001) == "群友(10001)"
    assert PEOPLE.who(SELF, "bot 昵称") == "耶梦加得(你)"
    assert PEOPLE.who(99999, "冒充", is_self=True) == "耶梦加得(你)"


def test_mention_rewrite() -> None:
    text = f"@{SELF} 总结一下 @{FRIEND} 和 @10001 还有 @老名字 @全体成员 mail 1@12345.com"
    assert PEOPLE.mentions(text) == (
        "@耶梦加得(你) 总结一下 @阿明(1000000002) 和 @群友(10001) 还有 @老名字 @全体成员 mail 1@12345.com"
    )
    # already rendered labels are left alone, short numbers are not QQ mentions
    assert PEOPLE.mentions("@阿明(1000000002) @1234") == "@阿明(1000000002) @1234"
    assert mentioned_ids([f"@{FRIEND} hi @{SELF}", "@老名字"]) == {FRIEND, SELF}


def test_normalize_mentions() -> None:
    segs: list[dict[str, Any]] = [
        {"type": "at", "data": {"qq": str(SELF)}},
        {"type": "text", "data": {"text": " 看看"}},
        {"type": "at", "data": {"qq": str(FRIEND)}},
        {"type": "at", "data": {"qq": "all"}},
    ]
    assert normalize(segs).text == f"@{SELF}  看看@{FRIEND} @全体成员"  # stored form is unchanged
    assert normalize(segs, PEOPLE.mention).text == "@耶梦加得(你) 看看@阿明(1000000002) @全体成员"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_context`` without a database: fixed names, no glossary, profiles or topics, nothing to flush."""

    async def resolve(group_id: int | None, user_ids: Any, *, bot_name: str, self_id: int | None, texts: Any) -> People:
        assert set(user_ids) >= {FRIEND, SELF}
        return People(bot_name, {FRIEND: "阿明"}, frozenset({self_id} if self_id else ()))

    async def nothing(*_: Any, **__: Any) -> Any:
        return []

    async def no_profiles(*_: Any) -> dict[int, Any]:
        return {}

    async def no_row(*_: Any) -> None:
        return None

    async def no_rows(*_: Any) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(context, "resolve", resolve)
    monkeypatch.setattr(context, "_glossary_hits", nothing)
    monkeypatch.setattr(context.WRITER, "flush", nothing)
    monkeypatch.setattr(search, "recent_messages", nothing)
    monkeypatch.setattr(search, "find_by_qq_id", no_row)
    monkeypatch.setattr(search, "find_by_qq_ids", no_rows)
    monkeypatch.setattr(search, "profiles", no_profiles)
    monkeypatch.setattr(search, "open_topics", nothing)


def _event(segs: Message, reply: dict[str, Any] | None = None) -> GroupMessageEvent:
    return group_message(segs, group_id=100, user_id=FRIEND, self_id=SELF, card="阿明", to_me=True, reply=reply)


async def test_trigger_renders_self_mention(offline: None) -> None:
    event = _event(Message([MessageSegment.at(SELF), MessageSegment.text(" 总结一下")]))
    resolved = Resolved("tldr", 1, "", (), "self", 0, False)
    ctx = await context.build_context(event, resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text="")
    assert "@耶梦加得(你) 总结一下" in ctx.prompt
    assert str(SELF) not in ctx.prompt
    assert '发送者="阿明(1000000002)"' in ctx.prompt


async def test_context_display_lists_only_the_recent_messages_the_prompt_kept(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    # Some messages span lines, so the cut can land inside one.
    recent = [
        MessageView(
            i, i, t0 + timedelta(minutes=i), FRIEND, "阿明", f"第{i}条{'\n续行' * (i % 3)}{'字' * 60}", None, False
        )
        for i in range(1, 41)
    ]

    async def recent_messages(*_: Any, **__: Any) -> list[MessageView]:
        return recent

    monkeypatch.setattr(search, "recent_messages", recent_messages)
    settings = Settings(context=ContextSettings(max_context_chars=1000))
    resolved = Resolved("chat", 1, "", (), "self", 0, False)
    event = _event(Message("问个问题"))
    ctx = await context.build_context(event, resolved, settings, self_id=SELF, bot_name="耶梦加得", request_text="")
    shown = [m.id for m in recent if f"] #{m.id} " in ctx.prompt]
    assert 1 not in shown
    assert shown[-1] == 40
    assert ctx.display is not None
    assert ctx.display.recent == shown


async def test_a_bare_tldr_gets_the_recent_chat_as_its_target(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The prod failure: a plain "tldr" right after someone posted a forward got no chat at all, so the model saw
    # nothing to summarize and stayed silent.
    t0 = datetime(2026, 10, 28, 16, 55, tzinfo=UTC)
    forward = MessageView(
        68950, 9, t0, 10001, "小红", "[合并转发]", None, False, [{"type": "forward", "data": {"id": "r"}}]
    )

    async def recent_messages(*_: Any, **__: Any) -> list[MessageView]:
        return [forward]

    monkeypatch.setattr(search, "recent_messages", recent_messages)
    resolved = Resolved("tldr", 1, "", (), "self", 0, False)
    bare = await context.build_context(
        _event(Message("tldr")), resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text=""
    )
    assert "#68950 小红(10001): [合并转发] (fwd#1)" in bare.prompt
    assert "目标是「最近群聊」里请求之前的内容" in bare.prompt

    # "tldr <topic>" names its own target: no chat up front (it would slant the summary).
    worded = await context.build_context(
        _event(Message("tldr qemu microvm")),
        resolved,
        Settings(),
        self_id=SELF,
        bot_name="耶梦加得",
        request_text="qemu microvm",
    )
    assert "#68950" not in worded.prompt


async def test_a_quoted_reply_brings_the_message_it_replies_to(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The prod failure: "tldr" on a one-line remark that replies to a forward. Only the remark reached the prompt,
    # so the forward had no handle and the model could not open it.
    t0 = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)
    remark_segs = [{"type": "reply", "data": {"id": "777"}}, {"type": "text", "data": {"text": "否定与自我否定"}}]
    remark = MessageView(73120, 2, t0, FRIEND, "阿明", "否定与自我否定", 777, False, remark_segs)
    forward_segs = [{"type": "forward", "data": {"id": "res-1"}}]
    forward = MessageView(73117, 777, t0 - timedelta(minutes=6), 10001, "老王", "[合并转发]", None, False, forward_segs)
    rows = {2: remark, 777: forward}

    async def find_by_qq_id(_group_id: int, qq_message_id: int) -> MessageView | None:
        return rows.get(qq_message_id)

    monkeypatch.setattr(search, "find_by_qq_id", find_by_qq_id)
    reply = {
        "time": int(t0.timestamp()),
        "message_type": "group",
        "message_id": 2,
        "real_id": 2,
        "sender": {"user_id": FRIEND, "card": "阿明"},
        "message": remark_segs,
    }
    event = _event(Message([MessageSegment.reply(2), MessageSegment.at(SELF), MessageSegment.text(" tldr")]), reply)
    resolved = Resolved("tldr", 1, "", (), "quoted", 0, False)
    ctx = await context.build_context(event, resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text="")

    # The forward shows up with a handle the model can pass to expand_forward, and the quoted remark has a number.
    assert "#73117 老王(10001): [合并转发] (fwd#1)" in ctx.prompt
    assert ctx.prompt.index("#73117") < ctx.prompt.index("否定与自我否定")
    assert '编号="#73120"' in ctx.prompt
    assert ctx.handles.get("fwd#1") is not None
    assert ctx.focus == ["fwd#1"]
    assert ctx.display is not None
    assert [m.id for m in ctx.display.chain] == [73117]


async def test_recent_chat_shows_who_replies_to_what(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The prod failure: "呃呃" replied to the bot's forward, but the chat lines dropped the reply, so later the
    # model guessed from timing that it answered someone else.
    t0 = datetime(2026, 11, 4, 1, 14, tzinfo=UTC)
    fwd_segs = [{"type": "forward", "data": {"id": "res-1", "title": "sudo-rs 不兼容"}}]
    answer = MessageView(86423, 501, t0, SELF, "耶梦加得", "[合并转发:sudo-rs 不兼容]", None, True, fwd_segs)
    aside = MessageView(86425, 502, t0 + timedelta(minutes=1), 10001, "小红", "看看", 300, False)
    react = MessageView(86426, 503, t0 + timedelta(minutes=1), 10002, "老王", "呃呃", 501, False)
    lost = MessageView(86427, 504, t0 + timedelta(minutes=2), 10001, "小红", "还在吗", 999, False)
    older = MessageView(86300, 300, t0 - timedelta(hours=2), 10002, "老王", "我的 landing page", None, False)
    looked_up: list[set[int]] = []

    async def recent_messages(*_: Any, **__: Any) -> list[MessageView]:
        return [answer, aside, react, lost]

    async def find_by_qq_ids(_group_id: int, ids: Any) -> dict[int, MessageView]:
        looked_up.append(set(ids))
        return {300: older}

    monkeypatch.setattr(search, "recent_messages", recent_messages)
    monkeypatch.setattr(search, "find_by_qq_ids", find_by_qq_ids)
    resolved = Resolved("chat", 1, "", (), "self", 0, False)
    ctx = await context.build_context(
        _event(Message("问个问题")), resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text=""
    )
    assert "#86423 耶梦加得(你): [合并转发:sudo-rs 不兼容] (fwd#1)" in ctx.prompt
    assert "#86426 老王(10002) 回复 #86423 耶梦加得(你): 呃呃" in ctx.prompt
    assert "#86425 小红(10001) 回复 #86300 老王(10002): 看看" in ctx.prompt
    assert "#86427 小红(10001) 回复一条更早的消息: 还在吗" in ctx.prompt
    assert looked_up == [{300, 999}]  # the bot's forward was already in the window


async def test_a_follow_up_prompt_has_only_the_chat_since_the_answer(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    t0 = datetime(2026, 11, 4, 1, 15, tzinfo=UTC)
    answer = MessageView(86427, 501, t0, SELF, "耶梦加得", "[合并转发:sudo-rs]", None, True)
    aside = MessageView(86428, 502, t0 + timedelta(seconds=10), 10001, "小红", "？", None, False)
    trigger_row = MessageView(86429, 7, t0 + timedelta(minutes=3), FRIEND, "阿明", "为什么", 501, False)
    later = MessageView(86430, 503, t0 + timedelta(minutes=4), 10001, "小红", "后来的话", None, False)
    asked: list[tuple[int | None, datetime]] = []

    async def messages_after(_group_id: int, after_id: int | None, since: datetime, **_: Any) -> list[MessageView]:
        asked.append((after_id, since))
        return [answer, aside, trigger_row, later]

    monkeypatch.setattr(search, "messages_after", messages_after)
    handles = Handles()
    handles.add("forward", {"id": "res-1"})
    handles.add("image", {"file": "old.png"})
    image = MessageSegment.image("https://example.com/new.png")
    reply = {
        "time": int(t0.timestamp()),
        "message_type": "group",
        "message_id": 501,
        "real_id": 501,
        "sender": {"user_id": SELF},
        "message": [],
    }
    event = group_message(
        Message([MessageSegment.reply(501), MessageSegment.text("为什么"), image]),
        group_id=100,
        user_id=FRIEND,
        self_id=SELF,
        message_id=7,
        card="阿明",
        reply=reply,
    )
    ctx = await context.build_followup_context(
        event,
        Settings(),
        self_id=SELF,
        bot_name="耶梦加得",
        request_text="为什么",
        handles=handles,
        nonce="ab12cd",
        answer_qq_id=501,
        seen_message_id=86426,
        since=t0,
        now="2026-11-04 09:18 Wednesday",
        recall=False,
    )
    assert asked == [(86426, t0)]
    # The answer is the model's own last turn already; the request comes on its own; later chat isn't in yet.
    assert "<中间的新增对话-ab12cd>" in ctx.prompt
    assert "#86428 小红(10001): ？" in ctx.prompt
    for absent in ("#86427", "#86429", "后来的话"):
        assert absent not in ctx.prompt
    assert '<请求-ab12cd 发送者="阿明(1000000002)">\n为什么 (img#2)' in ctx.prompt
    # What changed since the answer travels in this turn: the time, and whether this speaker may have it recalled.
    assert "现在是 2026-11-04 09:18 Wednesday" in ctx.prompt
    assert "只有当初提问的人和群主、群管理员能让你撤回它" in ctx.prompt
    assert ctx.focus == ["img#2"]  # the earlier run's handles keep their numbers
    assert ctx.handles.get("fwd#1") is not None
    assert ctx.display is not None
    assert ctx.display.recent == [86428]

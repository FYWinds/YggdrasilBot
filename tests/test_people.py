from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from yggdrasil.agent import context
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
    """``build_context`` without a database: fixed names, no glossary, nothing to flush."""

    async def resolve(group_id: int | None, user_ids: Any, *, bot_name: str, self_id: int | None, texts: Any) -> People:
        assert set(user_ids) >= {FRIEND, SELF}
        return People(bot_name, {FRIEND: "阿明"}, frozenset({self_id} if self_id else ()))

    async def nothing(*_: Any, **__: Any) -> Any:
        return []

    monkeypatch.setattr(context, "resolve", resolve)
    monkeypatch.setattr(context, "_glossary_hits", nothing)
    monkeypatch.setattr(context.WRITER, "flush", nothing)
    monkeypatch.setattr(search, "recent_messages", nothing)

    async def no_row(*_: Any) -> None:
        return None

    monkeypatch.setattr(search, "find_by_qq_id", no_row)


def _event(segs: Message, reply: dict[str, Any] | None = None) -> GroupMessageEvent:
    return GroupMessageEvent.model_validate(
        {
            "time": 1760000000,
            "self_id": SELF,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": FRIEND,
            "message_type": "group",
            "message_id": 1,
            "message": segs,
            "original_message": segs,
            "raw_message": "",
            "font": 0,
            "sender": {"user_id": FRIEND, "card": "阿明"},
            "to_me": True,
            "group_id": 100,
            "reply": reply,
        }
    )


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


async def test_a_quoted_reply_brings_the_message_it_replies_to(offline: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # The prod failure: "tldr" on a one-line remark that replies to a forward. Only the remark reached the prompt,
    # so the forward had no handle and the model could not open it.
    t0 = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)
    remark_segs = [{"type": "reply", "data": {"id": "777"}}, {"type": "text", "data": {"text": "否定与自我否定"}}]
    remark = MessageView(73120, 2, t0, FRIEND, "阿明", "否定与自我否定", 777, False, remark_segs)
    forward_segs = [{"type": "forward", "data": {"id": "res-1"}}]
    forward = MessageView(
        73117, 777, t0 - timedelta(minutes=6), 10001, "老王", "[合并转发]", None, False, forward_segs
    )
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

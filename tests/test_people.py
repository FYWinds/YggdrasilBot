from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from yggdrasil.agent import context
from yggdrasil.ingest.normalize import normalize
from yggdrasil.people import People, label, mentioned_ids, self_label
from yggdrasil.settings import Settings
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


async def test_trigger_renders_self_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(group_id: int | None, user_ids: Any, *, bot_name: str, self_id: int | None, texts: Any) -> People:
        assert set(user_ids) >= {FRIEND, SELF}
        return People(bot_name, {FRIEND: "阿明"}, frozenset({self_id} if self_id else ()))

    async def nothing(*_: Any, **__: Any) -> Any:
        return []

    monkeypatch.setattr(context, "resolve", resolve)
    monkeypatch.setattr(context, "_glossary_hits", nothing)
    monkeypatch.setattr(context.WRITER, "flush", nothing)
    segs = Message([MessageSegment.at(SELF), MessageSegment.text(" 总结一下")])
    event = GroupMessageEvent.model_validate(
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
        }
    )
    resolved = Resolved("tldr", 1, "", (), "self", 0, False)
    ctx = await context.build_context(event, resolved, Settings(), self_id=SELF, bot_name="耶梦加得", request_text="")
    assert "@耶梦加得(你) 总结一下" in ctx.prompt
    assert str(SELF) not in ctx.prompt
    assert '发送者="阿明(1000000002)"' in ctx.prompt

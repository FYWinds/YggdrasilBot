"""Other bots never trigger the bot. In prod a "name[bot]" account replied "tldr" to a message and got a summary; bots
answering bots can loop."""

import warnings
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, NetworkError

from yggdrasil import runtime
from yggdrasil.runtime import GroupInfo, Snapshot, TriggerRule
from yggdrasil.triggers import bots

GROUP, SELF = 990001, 10001
MEMBER, TAGGED, LISTED, OFFICIAL, OWNER, NEWCOMER = 20001, 20002, 20003, 20004, 20005, 20006
TLDR = TriggerRule(
    id=1,
    handler="tldr",
    scope="global",
    group_id=None,
    match_type="keyword_prefix",
    patterns=("tldr",),
    arg_template=None,
    require_mention=False,
    target="quoted_or_self",
    priority=10,
    cooldown_s=0,
    allowed_roles=(),
)


@dataclass
class FakeBot:
    """Our own connection; answers member lookups with QQ's robot flag."""

    robots: frozenset[int] = frozenset()
    down: bool = False
    self_id: str = str(SELF)
    lookups: list[int] = field(default_factory=list)

    async def call_api(self, api: str, **data: Any) -> dict[str, Any]:
        assert api == "get_group_member_info"
        self.lookups.append(data["user_id"])
        if self.down:
            raise NetworkError("WebSocket call api get_group_member_info timeout")
        return {"group_id": data["group_id"], "user_id": data["user_id"], "is_robot": data["user_id"] in self.robots}


@dataclass
class Writer:
    stored: list[int] = field(default_factory=list)

    def submit(self, msg: Any) -> None:
        self.stored.append(msg.user_id)


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    with warnings.catch_warnings():  # no adapter registered: routing doesn't need one
        warnings.filterwarnings("ignore", "No adapters found", RuntimeWarning)
        nonebot.require("nonebot_plugin_alconna")
        module = nonebot.require("yggdrasil.bot.plugin")
    monkeypatch.setattr(module, "WRITER", Writer())
    group = GroupInfo(GROUP, "g", True, {"interaction": {"ignore_users": [LISTED]}})
    snap = Snapshot(groups={GROUP: group}, triggers=(TLDR,), superusers=frozenset({OWNER}))
    monkeypatch.setattr(runtime, "_snapshot", snap)
    monkeypatch.setattr(bots, "_official", {})
    return module


def message(
    user_id: int, card: str, text: str = "tldr https://example.com/post", *, nickname: str = ""
) -> GroupMessageEvent:
    return GroupMessageEvent.model_validate(
        {
            "time": 1760000000,
            "self_id": SELF,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": user_id,
            "message_type": "group",
            "message_id": 1,
            "message": Message(text),
            "original_message": Message(text),
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "card": card, "nickname": nickname},
            "to_me": False,
            "group_id": GROUP,
        }
    )


async def routed(plugin: ModuleType, bot: FakeBot, event: GroupMessageEvent) -> str | None:
    """The command the message starts, if any."""
    state: dict[str, Any] = {}
    await plugin._route(bot, event, state)
    resolved = state.get(plugin.STATE_KEY)
    return resolved.handler if resolved else None


async def test_bots_never_trigger_but_their_messages_are_stored(plugin: ModuleType) -> None:
    bot = FakeBot(robots=frozenset({OFFICIAL}))
    assert await routed(plugin, bot, message(MEMBER, "阿明")) == "tldr"
    assert await routed(plugin, bot, message(TAGGED, "Mochi[bot]-测试中")) is None  # says it is one
    assert await routed(plugin, bot, message(TAGGED, "", nickname="mochi[BOT]")) is None
    assert await routed(plugin, bot, message(LISTED, "小助手")) is None  # interaction.ignore_users
    assert await routed(plugin, bot, message(OFFICIAL, "小精灵")) is None  # QQ's own robot flag
    assert await routed(plugin, bot, message(OWNER, "owner[bot]")) == "tldr"  # superusers never count as bots
    assert await routed(plugin, bot, message(NEWCOMER, "robot 爱好者 测试中")) == "tldr"  # only the tag counts
    assert plugin.WRITER.stored == [MEMBER, TAGGED, TAGGED, LISTED, OFFICIAL, OWNER, NEWCOMER]


async def test_qq_is_asked_once_per_sender_and_only_about_triggers(plugin: ModuleType) -> None:
    bot = FakeBot(robots=frozenset({OFFICIAL}))
    for _ in range(2):
        assert await routed(plugin, bot, message(OFFICIAL, "小精灵")) is None
        assert await routed(plugin, bot, message(MEMBER, "阿明")) == "tldr"
    assert await routed(plugin, bot, message(NEWCOMER, "新人", "大家好")) is None  # no trigger, no lookup
    assert bot.lookups == [OFFICIAL, MEMBER]

    down = FakeBot(down=True)
    for _ in range(2):  # a failed lookup counts as a person, without delaying each request with another try
        assert await routed(plugin, down, message(NEWCOMER, "新人")) == "tldr"
    assert down.lookups == [NEWCOMER]

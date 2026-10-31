"""One answer per tldr target. Prod answered twice when two members replied "tldr" to one message a second apart and
when someone pasted the same link again (two separate QQ messages each time, not a redelivered event)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import fakeredis
import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from yggdrasil import runtime
from yggdrasil.pipeline import invoke, ratelimit, repeats
from yggdrasil.runtime import GroupInfo, Snapshot
from yggdrasil.settings import InteractionSettings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE, BOB, OWNER = 990001, 10001, 20001, 20002, 20003
QUOTED, OTHER_QUOTED = 500, 501
TLDR = Resolved("tldr", 1, "", (), "quoted_or_self", 0, False)
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
SILENT = InteractionSettings().silent_emoji
BOT = cast(Bot, object())  # never called: runs and reactions are faked


@dataclass
class Group:
    started: list[int] = field(default_factory=list)  # trigger message ids that started a run
    reactions: list[tuple[int, str]] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)  # statuses the next runs end with ("ok" once empty)


@pytest.fixture
async def group(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Group]:
    g = Group()
    conn = fakeredis.FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(repeats, "redis_client", lambda: conn)
    monkeypatch.setattr(ratelimit, "redis_client", lambda: conn)
    no_limits = {"ratelimit": {"user_per_minute": 0, "group_per_minute": 0}}
    snap = Snapshot(groups={GROUP: GroupInfo(GROUP, "g", True, no_limits)}, superusers=frozenset({OWNER}))
    monkeypatch.setattr(runtime, "_snapshot", snap)

    async def react(_api: Any, message_id: int, emoji: str) -> None:
        g.reactions.append((message_id, emoji))

    async def run(self: invoke.Invocator) -> str:
        g.started.append(self.trigger_message_id)
        return g.outcomes.pop(0) if g.outcomes else "ok"

    monkeypatch.setattr(invoke, "react", react)
    monkeypatch.setattr(invoke.Invocator, "run", run)
    yield g
    await conn.aclose()


def message(
    message_id: int, user_id: int, text: str, *, quoting: int | None = None, image: bool = False
) -> GroupMessageEvent:
    segs = Message([MessageSegment.reply(quoting)] if quoting is not None else [])
    segs.append(MessageSegment.text(text))
    if image:
        segs.append(MessageSegment.image("https://example.com/cat.png"))
    quoted = {
        "time": 1760000000,
        "message_type": "group",
        "message_id": quoting,
        "real_id": quoting,
        "sender": {"user_id": 20009, "card": "老王"},
        "message": [{"type": "text", "data": {"text": "很长的一段话"}}],
    }
    return GroupMessageEvent.model_validate(
        {
            "time": 1760000000,
            "self_id": SELF,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": user_id,
            "message_type": "group",
            "message_id": message_id,
            "message": segs,
            "original_message": segs,
            "raw_message": "",
            "font": 0,
            "sender": {"user_id": user_id, "card": "群友"},
            "to_me": False,
            "group_id": GROUP,
            "reply": quoted if quoting is not None else None,
        }
    )


async def tldr(event: GroupMessageEvent, words: str = "") -> None:
    await invoke.run(BOT, event, TLDR, "tldr", words)


async def test_the_same_message_delivered_twice_starts_one_run(group: Group) -> None:
    event = message(1, ALICE, "tldr", quoting=QUOTED)
    await tldr(event)
    await tldr(event)
    assert group.started == [1]
    assert group.reactions == [(1, SILENT)]  # the repeat is only marked


async def test_one_answer_per_target(group: Group) -> None:
    await tldr(message(1, ALICE, "tldr", quoting=QUOTED))
    await tldr(message(2, BOB, "tldr", quoting=QUOTED))  # another member, a second later
    await tldr(message(3, ALICE, "tldr https://example.com/post"), "https://example.com/post")
    await tldr(message(4, ALICE, "tldr  https://example.com/post "), " https://example.com/post ")  # pasted again
    assert group.started == [1, 3]
    assert group.reactions == [(2, SILENT), (4, SILENT)]


async def test_links_that_differ_only_in_case_are_different_targets(group: Group) -> None:
    await tldr(message(1, ALICE, "tldr https://example.com/s/aBc12"), "https://example.com/s/aBc12")
    await tldr(message(2, BOB, "tldr https://example.com/s/AbC12"), "https://example.com/s/AbC12")  # another video
    assert group.started == [1, 2]
    assert group.reactions == []


async def test_other_requests_still_run(group: Group) -> None:
    await tldr(message(1, ALICE, "tldr", quoting=QUOTED))
    await tldr(message(2, BOB, "tldr 只说价格", quoting=QUOTED), "只说价格")  # a focus asks something else
    await tldr(message(3, BOB, "tldr", quoting=OTHER_QUOTED))
    await tldr(message(4, ALICE, "tldr"))  # a bare tldr's target, the recent chat, moves on
    await tldr(message(5, ALICE, "tldr"))
    await tldr(message(6, BOB, "tldr", image=True))  # attachments can't be told apart
    await tldr(message(7, BOB, "tldr", image=True))
    await tldr(message(8, OWNER, "tldr", quoting=QUOTED))  # superusers re-run on purpose
    for i in (9, 10):  # a chat answer is a conversation with its asker
        await invoke.run(BOT, message(i, ALICE, "早", quoting=QUOTED), CHAT, "chat", "早")
    assert group.started == list(range(1, 11))
    assert group.reactions == []


async def test_a_failed_run_can_be_asked_for_again(group: Group) -> None:
    group.outcomes = ["failed"]
    for i in (1, 2, 3):
        await tldr(message(i, ALICE, "tldr", quoting=QUOTED))
    assert group.started == [1, 2]  # the retry ran; its answer stands
    assert group.reactions == [(3, SILENT)]

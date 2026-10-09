import time
from typing import Any

import pytest
from nonebot.adapters.onebot.v11 import (
    GroupBanNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupRequestEvent,
    NoticeEvent,
)

from yggdrasil import runtime
from yggdrasil.events import observe
from yggdrasil.runtime import GroupInfo, Snapshot

BOT, ENABLED, OTHER = 1000, 2000, 3000


@pytest.fixture(autouse=True)
def _groups(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = Snapshot(groups={ENABLED: GroupInfo(ENABLED, "g", True, {}), OTHER: GroupInfo(OTHER, "o", False, {})})
    monkeypatch.setattr(runtime, "_snapshot", snap)


def _notice(cls: type, notice_type: str, **fields: Any):
    base = {"time": int(time.time()), "self_id": BOT, "post_type": "notice", "notice_type": notice_type}
    return cls.model_validate({**base, **fields})


def test_bot_self_events_are_kept_for_any_group_member_changes_only_for_enabled_ones() -> None:
    kicked = _notice(
        GroupDecreaseNoticeEvent, "group_decrease", sub_type="kick_me", group_id=OTHER, user_id=BOT, operator_id=5
    )
    o = observe(kicked, BOT)
    assert o is not None
    assert (o.kind, o.group_id, o.operator_id) == ("notice.group_decrease.kick_me", OTHER, 5)

    left = {"sub_type": "leave", "user_id": 42, "operator_id": 42}
    assert observe(_notice(GroupDecreaseNoticeEvent, "group_decrease", group_id=OTHER, **left), BOT) is None
    o = observe(_notice(GroupDecreaseNoticeEvent, "group_decrease", group_id=ENABLED, **left), BOT)
    assert o is not None
    assert o.kind == "notice.group_decrease.leave"

    joined = _notice(
        GroupIncreaseNoticeEvent, "group_increase", sub_type="invite", group_id=OTHER, user_id=BOT, operator_id=7
    )
    o = observe(joined, BOT)
    assert o is not None
    assert o.kind == "notice.group_increase.self"


def test_bans_are_split_into_self_whole_group_and_member() -> None:
    def ban(group: int, user: int) -> str | None:
        ev = _notice(
            GroupBanNoticeEvent, "group_ban", sub_type="ban", group_id=group, user_id=user, operator_id=9, duration=600
        )
        o = observe(ev, BOT)
        return o.kind if o else None

    assert ban(OTHER, BOT) == "notice.group_ban.self.ban"
    assert ban(OTHER, 42) is None
    assert ban(ENABLED, 0) == "notice.group_ban.all.ban"
    assert ban(ENABLED, 42) == "notice.group_ban.member.ban"


def test_requests_keep_their_flag_and_offline_notices_are_recognised() -> None:
    invite = GroupRequestEvent.model_validate(
        {
            "time": 1,
            "self_id": BOT,
            "post_type": "request",
            "request_type": "group",
            "sub_type": "invite",
            "group_id": OTHER,
            "user_id": 1000000002,
            "flag": "abc",
            "comment": "",
        }
    )
    o = observe(invite, BOT)
    assert o is not None
    assert (o.kind, o.is_request, o.data["flag"]) == ("request.group.invite", True, "abc")

    offline = _notice(NoticeEvent, "bot_offline")
    o = observe(offline, BOT)
    assert o is not None
    assert o.kind == "notice.bot_offline"

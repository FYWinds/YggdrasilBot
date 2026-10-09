import random

import pytest
from sqlalchemy import delete

from yggdrasil.agent.permissions import may_recall
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.ops.tools import merge_patch, napcat_gate
from yggdrasil.tools.errors import ToolError


def test_napcat_gate_reads_freely_writes_by_allowlist_and_never_leaks_or_leaves() -> None:
    allowed = ["set_group_ban", "set_group_leave"]
    assert napcat_gate("get_group_list", allowed) is False
    assert napcat_gate("_get_group_notice", allowed) is False
    assert napcat_gate("set_group_ban", allowed) is True
    for action in ("get_cookies", "get_csrf_token", "nc_get_rkey", "set_group_kick", "set_group_leave"):
        with pytest.raises(ToolError):
            napcat_gate(action, allowed)


def test_merge_patch_merges_nested_and_null_removes_back_to_inherited() -> None:
    doc = {"moderation": {"default_rule": {"threshold": 0.5}, "enabled": True}, "persona": {"name": "x"}}
    out = merge_patch(doc, {"moderation": {"default_rule": {"threshold": None}, "check_input": False}, "persona": None})
    # Emptied sections disappear entirely so the inherited value applies again.
    assert out == {"moderation": {"enabled": True, "check_input": False}}
    assert doc["persona"] == {"name": "x"}  # input untouched


async def test_only_the_asker_or_group_staff_may_recall_a_bot_answer() -> None:
    group_id, asker, other = -random.randint(10**6, 10**7), 111, 222
    bot_msg = random.randint(1, 2**31)
    async with get_session() as s:
        s.add(Invocation(group_id=group_id, user_id=asker, handler="chat", reply_qq_message_id=bot_msg, status="ok"))
    try:
        assert await may_recall("member", asker, group_id, bot_msg)
        assert not await may_recall("member", other, group_id, bot_msg)
        assert not await may_recall("member", asker, group_id, bot_msg + 1)  # not an answer we know of
        assert await may_recall("admin", other, group_id, bot_msg)
        assert await may_recall("superuser", other, group_id, bot_msg + 1)
    finally:
        async with get_session() as s:
            await s.execute(delete(Invocation).where(Invocation.group_id == group_id))

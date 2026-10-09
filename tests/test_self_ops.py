import random

from sqlalchemy import delete

from yggdrasil.agent.permissions import may_recall
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.ops.tools import merge_patch


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

"""The invocation log names who asked, by their latest group card."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.db.models import Invocation, UserAlias
from yggdrasil.db.session import get_session

GROUP, USER, STRANGER = 990061, 10001, 10002


@pytest.fixture
async def handler() -> AsyncIterator[str]:
    """A handler name unique to this run, so the list holds only these rows."""
    handler = f"test.{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    async with get_session() as s:
        s.add_all(
            [
                UserAlias(group_id=GROUP, user_id=USER, name="旧名片", last_seen=now - timedelta(days=3)),
                UserAlias(group_id=GROUP, user_id=USER, name="新名片", last_seen=now),
                Invocation(group_id=GROUP, user_id=USER, handler=handler, created_at=now - timedelta(minutes=3)),
                Invocation(group_id=None, user_id=USER, handler=handler, created_at=now - timedelta(minutes=2)),
                Invocation(group_id=GROUP, user_id=STRANGER, handler=handler, created_at=now - timedelta(minutes=1)),
            ]
        )
    yield handler
    async with get_session() as s:
        await s.execute(delete(Invocation).where(Invocation.handler == handler))
        await s.execute(delete(UserAlias).where(UserAlias.group_id == GROUP))


async def test_list_and_detail_name_the_requester(client: httpx.AsyncClient, handler: str) -> None:
    r = await client.get("/api/invocations", params={"handler": handler})
    assert r.status_code == 200
    items = r.json()["items"]
    # Newest first: an unknown member and a private chat have no name; a renamed member shows the latest card.
    assert [(i["group_id"], i["user_id"], i["user_name"]) for i in items] == [
        (GROUP, STRANGER, ""),
        (None, USER, ""),
        (GROUP, USER, "新名片"),
    ]
    detail = (await client.get(f"/api/invocations/{items[2]['id']}")).json()
    assert detail["user_name"] == "新名片"

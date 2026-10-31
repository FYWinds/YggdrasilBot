"""Memory console: fact scores and archive state, restoring an archived fact (which curation then keeps)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.db.models import UserProfile, UserProfileFact
from yggdrasil.db.session import get_session
from yggdrasil.memory.curation import cap_facts
from yggdrasil.settings import MemorySettings

GROUP, USER = 990093, 20003


async def _cleanup() -> None:
    async with get_session() as s:
        await s.execute(delete(UserProfileFact).where(UserProfileFact.group_id == GROUP))
        await s.execute(delete(UserProfile).where(UserProfile.group_id == GROUP))


@pytest.fixture
async def facts() -> AsyncIterator[dict[str, int]]:
    await _cleanup()
    now = datetime.now(UTC)
    rows = {
        "engineer": UserProfileFact(fact="是后端工程师", importance=5, weight=5.0, scored_at=now),
        "arch": UserProfileFact(
            fact="用 Arch Linux", importance=3, weight=3.0, scored_at=now, archived_at=now, archive_reason="cap"
        ),
    }
    async with get_session() as s:
        s.add(UserProfile(group_id=GROUP, user_id=USER, display_name="老王"))
        for f in rows.values():
            f.group_id, f.user_id, f.source_msg_ids = GROUP, USER, [1]
        s.add_all(rows.values())
        await s.flush()
        ids = {k: f.id for k, f in rows.items()}
    yield ids
    await _cleanup()


async def test_restored_fact_comes_back_locked_and_curation_keeps_it(
    client: httpx.AsyncClient, facts: dict[str, int]
) -> None:
    base = f"/api/memory/profiles/{GROUP}/{USER}"
    shown = (await client.get(base)).json()["facts"]
    # Active facts first; scores and archive state are shown.
    assert [(f["fact"], f["importance"], f["weight"], f["archive_reason"], f["locked"]) for f in shown] == [
        ("是后端工程师", 5, 5.0, None, False),
        ("用 Arch Linux", 3, 3.0, "cap", False),
    ]

    r = await client.post(f"{base}/facts/{facts['arch']}/restore")
    assert r.status_code == 200
    assert (r.json()["archived_at"], r.json()["archive_reason"], r.json()["locked"]) == (None, None, True)
    assert (await client.post(f"{base}/facts/{facts['engineer']}/restore")).status_code == 409
    assert (await client.post(f"/api/memory/profiles/{GROUP}/1/facts/{facts['arch']}/restore")).status_code == 404

    # A cap of one active fact would archive it again, but a restored fact is kept.
    async with get_session() as s:
        assert await cap_facts(s, GROUP, MemorySettings(facts_per_user=1)) == 0
    shown = (await client.get(base)).json()["facts"]
    assert [(f["fact"], f["archived_at"]) for f in shown] == [("是后端工程师", None), ("用 Arch Linux", None)]

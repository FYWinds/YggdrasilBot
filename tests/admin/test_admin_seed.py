"""The dev seed (``yggdrasil.devtools.seed``): every seeded run reads back as a transcript in the console, seeding
again replaces rather than duplicates, and reset removes only what the seed wrote."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete, func, select

from tests.support.groups import fresh_group
from yggdrasil.db.models import Invocation, Message, Transcript
from yggdrasil.db.session import get_session
from yggdrasil.devtools import seed
from yggdrasil.devtools.seed.data import GROUPS, JOB_IDS, TRACE_PREFIX
from yggdrasil.devtools.seed.scenarios import RUNS

pytestmark = pytest.mark.db

OTHER = fresh_group()


@pytest.fixture
async def unrelated() -> AsyncIterator[list[uuid.UUID]]:
    """Rows the seed must leave alone: another group's invocation and message, a member's private invocation."""
    rows = [
        Invocation(group_id=OTHER, user_id=20001, handler="chat", status="ok"),
        Invocation(group_id=None, user_id=20001, handler="ops", status="done", trace_id="abc123"),
    ]
    async with get_session() as s:
        s.add_all(rows)
        s.add(Message(sent_at=datetime.now(UTC), group_id=OTHER, user_id=20001, qq_message_id=1, segments=[]))
    yield [r.id for r in rows]
    async with get_session() as s:
        await s.execute(delete(Invocation).where(Invocation.id.in_([r.id for r in rows])))
        await s.execute(delete(Message).where(Message.group_id == OTHER))


async def _count(model: type, *where: object) -> int:
    async with get_session() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where)) or 0  # pyright: ignore[reportArgumentType]


async def test_seed_round_trip(client: httpx.AsyncClient, unrelated: list[uuid.UUID]) -> None:
    try:
        await seed.seed()
        await seed.seed()
        async with get_session() as s:
            seeded = (await s.scalars(select(Invocation).where(Invocation.trace_id.startswith(TRACE_PREFIX)))).all()
        assert len(seeded) == len(RUNS) + 2  # plus the private ops run and the job report
        for i in seeded:
            r = await client.get(f"/api/invocations/{i.id}/transcript")
            assert r.status_code == 200
            body = r.json()
            # Runs that reached the model were recorded; the others show their request.
            assert body["available"] is (i.status not in ("blocked_input", "ratelimited", "running")), i.status
            assert body["entries"]
        for job_id in JOB_IDS:
            assert (await client.get(f"/api/sandbox/jobs/{job_id}/transcript")).status_code == 200
    finally:
        await seed.reset()
    assert await _count(Invocation, Invocation.trace_id.startswith(TRACE_PREFIX)) == 0
    assert await _count(Transcript, Transcript.job_id.in_(JOB_IDS)) == 0
    assert await _count(Message, Message.group_id.in_(list(GROUPS))) == 0
    assert await _count(Invocation, Invocation.id.in_(unrelated)) == 2
    assert await _count(Message, Message.group_id == OTHER) == 1

"""Memory jobs never keep a transaction open while they wait on something else.

2026-11-01: chunk_and_embed read ``messages`` and, with that transaction still open, looked up names through a second
connection. The daily partition DDL queued for an exclusive lock between the two, every later query on ``messages``
queued behind the DDL, and ingest and replies stalled for 90 minutes: a lock cycle the server can't see.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from tests.support.db import embed_space, insert_message, purge_groups, unit_vector
from tests.support.groups import fresh_group
from yggdrasil.db.models import Message, MessageChunk
from yggdrasil.db.session import get_session
from yggdrasil.llm import registry
from yggdrasil.memory import chunking, vectors

pytestmark = pytest.mark.db

GROUP = fresh_group()


async def _open_transactions() -> int:
    """The test run's other connections sitting in an open transaction (all of them carry its application_name, so
    a bot sharing the database doesn't count)."""
    async with get_session() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = current_setting('application_name') "
                    "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                )
            )
            or 0
        )


@pytest.fixture
async def chat() -> AsyncIterator[list[int]]:
    """Two closed bursts of chat, an hour old."""
    await purge_groups(GROUP)
    t0 = datetime.now(UTC) - timedelta(hours=3)
    ids = [
        await insert_message(
            GROUP, f"第{i}条消息", user_id=10001 + i % 2, sent_at=t0 + timedelta(minutes=minute), qq_message_id=500 + i
        )
        for i, minute in enumerate((0, 1, 2, 90, 91))
    ]
    yield ids
    await purge_groups(GROUP)


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Open-transaction counts seen whenever the job looks up names (on its own connection)."""
    seen: list[int] = []
    real = chunking.people_for

    async def people_for(*args: Any, **kwargs: Any) -> Any:
        seen.append(await _open_transactions())
        return await real(*args, **kwargs)

    monkeypatch.setattr(chunking, "people_for", people_for)
    return seen


async def test_chunking_looks_up_names_with_no_transaction_open(chat: list[int], watched: list[int]) -> None:
    assert (await chunking.chunk_group(GROUP))["created"] == 2
    # A recall rebuilds the chunk it was in; that name lookup must not run inside the read either.
    async with get_session() as s:
        await s.execute(
            update(Message).where(Message.id == chat[1]).values(recalled=True, recalled_at=datetime.now(UTC))
        )
    assert (await chunking.chunk_group(GROUP))["rebuilt"] == 1
    assert watched
    assert all(n == 0 for n in watched)
    async with get_session() as s:
        texts = list(await s.scalars(select(MessageChunk.text).where(MessageChunk.group_id == GROUP)))
    assert len(texts) == 2
    assert all("第1条消息" not in t for t in texts)


async def test_embedding_runs_with_no_transaction_open(chat: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    await chunking.chunk_group(GROUP)
    seen: list[int] = []

    async def embed(texts: list[str], **_: Any) -> list[list[float]]:
        seen.append(await _open_transactions())
        return [unit_vector(1.0) for _ in texts]

    monkeypatch.setattr(registry, "embed", embed)
    async with embed_space() as space:
        counts = await vectors.embed_missing(space, GROUP)
    assert counts["chunk"] == 2
    assert seen
    assert all(n == 0 for n in seen)

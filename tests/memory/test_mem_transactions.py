"""Memory jobs never keep a transaction open while they wait on something else.

2026-11-01: chunk_and_embed read ``messages`` and, with that transaction still open, looked up names through a second
connection. The daily partition DDL queued for an exclusive lock between the two, every later query on ``messages``
queued behind the DDL, and ingest and replies stalled for 90 minutes: a lock cycle the server can't see.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update

from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import EmbedSpace, MemoryVector, Message, MessageChunk, PipelineState, Provider
from yggdrasil.db.session import get_session
from yggdrasil.llm import registry
from yggdrasil.memory import chunking, vectors

GROUP = 990041


async def _open_transactions() -> int:
    """Other connections of this database sitting in an open transaction."""
    async with get_session() as s:
        return int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                )
            )
            or 0
        )


async def _cleanup() -> None:
    async with get_session() as s:
        await s.execute(delete(MemoryVector).where(MemoryVector.group_id == GROUP))
        await s.execute(delete(MessageChunk).where(MessageChunk.group_id == GROUP))
        await s.execute(delete(Message).where(Message.group_id == GROUP))
        await s.execute(delete(PipelineState).where(PipelineState.group_id == GROUP))


@pytest.fixture
async def chat() -> AsyncIterator[list[int]]:
    """Two closed bursts of chat, an hour old."""
    await _cleanup()
    t0 = datetime.now(UTC) - timedelta(hours=3)
    ids: list[int] = []
    async with get_session() as s:
        for i, minute in enumerate((0, 1, 2, 90, 91)):
            m = Message(
                sent_at=t0 + timedelta(minutes=minute),
                group_id=GROUP,
                user_id=10001 + i % 2,
                qq_message_id=500 + i,
                plain_text=f"第{i}条消息",
                segments=[{"type": "text", "data": {"text": f"第{i}条消息"}}],
            )
            s.add(m)
            await s.flush()
            ids.append(m.id)
    yield ids
    await _cleanup()


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
        return [[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts]

    monkeypatch.setattr(registry, "embed", embed)
    async with get_session() as s:
        provider = Provider(name="test-transactions-embed", base_url="http://embed.example.com/v1")
        s.add(provider)
        await s.flush()
        es = EmbedSpace(provider_id=provider.id, model="test-embed", dim=EMBED_DIM)
        s.add(es)
        await s.flush()
        space = vectors.EmbedSpaceInfo(es.id, provider.id, es.model, es.dim)
    try:
        counts = await vectors.embed_missing(space, GROUP)
        assert counts["chunk"] == 2
        assert seen
        assert all(n == 0 for n in seen)
    finally:
        async with get_session() as s:
            await s.execute(delete(EmbedSpace).where(EmbedSpace.id == space.id))
            await s.execute(delete(Provider).where(Provider.name == "test-transactions-embed"))

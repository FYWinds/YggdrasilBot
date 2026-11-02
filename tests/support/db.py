"""Rows for database tests (marked ``db``; see tests/conftest.py)."""

import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete

from yggdrasil.db.base import EMBED_DIM, Base
from yggdrasil.db.models import EmbedSpace, Message, Provider
from yggdrasil.db.session import get_session
from yggdrasil.napcat.api import text_seg
from yggdrasil.runtime import EmbedSpaceInfo


async def insert_message(
    group_id: int,
    text: str = "",
    *,
    user_id: int = 20001,
    sender_name: str = "群友",
    sent_at: datetime | None = None,
    qq_message_id: int = 1,
    segments: list[dict[str, Any]] | None = None,
    recalled: bool = False,
) -> int:
    """Store a chat message (by default a text message sent now); its row id."""
    async with get_session() as s:
        m = Message(
            sent_at=sent_at or datetime.now(UTC),
            group_id=group_id,
            user_id=user_id,
            qq_message_id=qq_message_id,
            sender_name=sender_name,
            plain_text=text,
            segments=segments if segments is not None else [text_seg(text)] if text else [],
            recalled=recalled,
            recalled_at=datetime.now(UTC) if recalled else None,
        )
        s.add(m)
        await s.flush()
        return m.id


async def purge_groups(*group_ids: int) -> None:
    """Delete the groups' rows from every table keyed by group."""
    async with get_session() as s:
        for table in reversed(Base.metadata.sorted_tables):
            if "group_id" in table.c:
                await s.execute(delete(table).where(table.c.group_id.in_(group_ids)))


def unit_vector(cos: float) -> list[float]:
    """A unit vector at cosine ``cos`` to the first axis."""
    vec = [0.0] * EMBED_DIM
    vec[0], vec[1] = cos, math.sqrt(1 - cos * cos)
    return vec


@asynccontextmanager
async def embed_space() -> AsyncIterator[EmbedSpaceInfo]:
    """An embedding space of its own provider, both removed afterwards."""
    async with get_session() as s:
        provider = Provider(name=f"test-embed-{uuid.uuid4().hex[:8]}", base_url="http://embed.example.com/v1")
        s.add(provider)
        await s.flush()
        es = EmbedSpace(provider_id=provider.id, model="test-embed", dim=EMBED_DIM)
        s.add(es)
        await s.flush()
        space = EmbedSpaceInfo(es.id, provider.id, es.model, es.dim)
    try:
        yield space
    finally:
        async with get_session() as s:
            await s.execute(delete(EmbedSpace).where(EmbedSpace.id == space.id))
            await s.execute(delete(Provider).where(Provider.id == space.provider_id))

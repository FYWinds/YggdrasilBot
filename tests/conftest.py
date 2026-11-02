"""Fixtures shared by every test directory: the runtime config a test runs under, the bot's own messages, and the
embedding space search runs against."""

import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from tests.support.db import embed_space, unit_vector
from yggdrasil import runtime
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.llm import registry


@pytest.fixture
def snapshot(monkeypatch: pytest.MonkeyPatch) -> Callable[..., runtime.Snapshot]:
    """Install a runtime config for this test: ``snapshot(groups={...}, routes={...})``. Calls add up (later fields
    win) on top of an empty ``Snapshot``; the previous one is back after the test."""
    current = runtime.Snapshot()

    def install(**fields: Any) -> runtime.Snapshot:
        nonlocal current
        current = dataclasses.replace(current, _settings_cache={}, **fields)
        monkeypatch.setattr(runtime, "_snapshot", current)
        return current

    return install


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[IncomingMessage]:
    """What the code hands the ingest writer (the bot's own messages), kept here instead of stored."""
    out: list[IncomingMessage] = []
    monkeypatch.setattr(WRITER, "submit", out.append)
    return out


@pytest.fixture
async def search_space(
    snapshot: Callable[..., runtime.Snapshot], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[int]:
    """The active embedding space (its id). Every query embeds to the first axis, so a stored vector's cosine to any
    query is its first coordinate (``unit_vector``)."""

    async def embed(texts: list[str], **_: Any) -> list[list[float]]:
        return [unit_vector(1.0) for _ in texts]

    async with embed_space() as space:
        snapshot(embed_space=space)
        monkeypatch.setattr(registry, "embed", embed)
        yield space.id

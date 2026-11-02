"""Fixtures shared by every test directory.

Database: tests marked ``db`` run only with ``YGG_TEST_DB=1``, against the throwaway database the ``POSTGRES_*``
variables name, which must then be reachable; without it they skip. Any other test that reaches the database fails.

Redis: every ``kv.redis_client()`` is one in-process fake, emptied before each test; no test reaches a server.

Also the runtime config a test runs under, the bot's own messages, and the embedding space search runs against."""

import dataclasses
import os
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from types import SimpleNamespace
from typing import Any, NoReturn

import fakeredis
import pytest
from sqlalchemy import event, text

from tests.support.db import embed_space, unit_vector
from yggdrasil import kv, runtime
from yggdrasil.config import DB_CONFIG
from yggdrasil.db import session
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.llm import registry

TEST_DB = os.environ.get("YGG_TEST_DB") == "1"
# Tagged on every connection the suite opens, so a test counting connections skips a bot sharing the database.
APPLICATION_NAME = "ygg-tests"
_REDIS = fakeredis.FakeServer()
_reached_db = pytest.StashKey[bool]()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if TEST_DB:
        return
    skip = pytest.mark.skip(reason="needs the test database: set YGG_TEST_DB=1 and POSTGRES_* (its rows get deleted)")
    for item in items:
        if item.get_closest_marker("db"):
            item.add_marker(skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    yield
    if item.stash.get(_reached_db, False):  # the test swallowed the failure
        pytest.fail(f"{item.nodeid} reached the database without @pytest.mark.db", pytrace=False)


@pytest.fixture(scope="session")
async def _db_engine() -> AsyncIterator[None]:
    """The engine of the test database: reachable, its connections tagged, disposed after the run."""
    engine = session.init_engine()

    @event.listens_for(engine.sync_engine, "do_connect")
    def _tag(_dialect: Any, _record: Any, _args: Any, params: dict[str, Any]) -> None:
        params["server_settings"] = {**params.get("server_settings", {}), "application_name": APPLICATION_NAME}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        where = f"{DB_CONFIG.host}:{DB_CONFIG.port}/{DB_CONFIG.database}"
        unreachable = f"YGG_TEST_DB=1 but the test database {where} is unreachable: {e!r}"
        raise pytest.fail.Exception(unreachable, pytrace=False) from None
    yield
    await session.dispose_engine()


@pytest.fixture(autouse=True)
def _database(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """``db`` tests get the test database; any other test that reaches it fails."""
    if request.node.get_closest_marker("db"):
        request.getfixturevalue("_db_engine")
        return

    def refuse(**_: Any) -> NoReturn:
        request.node.stash[_reached_db] = True
        pytest.fail(f"{request.node.nodeid} reached the database without @pytest.mark.db", pytrace=False)

    monkeypatch.setattr(session, "init_engine", refuse)


@pytest.fixture(scope="session", autouse=True)
def _redis() -> Iterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(server=_REDIS, decode_responses=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(kv, "aioredis", SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *_, **__: client)))
        kv.redis_client.cache_clear()
        yield client
        kv.redis_client.cache_clear()


@pytest.fixture(autouse=True)
def _empty_redis() -> None:
    fakeredis.FakeRedis(server=_REDIS).flushall()


@pytest.fixture
def fake_redis(_redis: fakeredis.FakeAsyncRedis) -> fakeredis.FakeAsyncRedis:
    """The Redis every ``kv.redis_client()`` gets: in-process (Lua via lupa), empty when the test starts."""
    return _redis


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

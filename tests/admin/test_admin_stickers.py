"""Sticker browsing in the admin API against the local database."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.db.models import Group, Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session

GROUP, NAMED_GROUP = 990041, 990042


@dataclass
class Rows:
    tag: str  # unique search term, so rows left by other runs never match
    eyeroll: int
    gore: int
    pending: int
    failed: int


def _image(**kw: object) -> Sticker:
    return Sticker(kind="image", ref=uuid.uuid4().hex.upper(), summary="动画表情", **kw)


@pytest.fixture
async def rows() -> AsyncIterator[Rows]:
    tag = uuid.uuid4().hex[:8]
    eyeroll = _image(status="ready", description="翻白眼", emotions=["无语", f"t{tag}"], mime="image/png")
    gore = _image(status="ready", description="血腥", ocr_text=f"字{tag}", unsafe_category="gore")
    pending = _image(status="fetching")
    failed = _image(status="failed", error="too_large")
    async with get_session() as session:
        await session.execute(delete(Group).where(Group.group_id == NAMED_GROUP))
        session.add(Group(group_id=NAMED_GROUP, name="测试群"))
        session.add_all([eyeroll, gore, pending, failed])
        await session.flush()
        session.add(StickerBlob(sticker_id=eyeroll.id, data=b"\x89PNG-bytes"))
        session.add(StickerUsage(sticker_id=eyeroll.id, group_id=GROUP, count=3))
        session.add(StickerUsage(sticker_id=eyeroll.id, group_id=NAMED_GROUP, count=5))
    yield Rows(tag, eyeroll.id, gore.id, pending.id, failed.id)
    async with get_session() as session:
        await session.execute(delete(Sticker).where(Sticker.id.in_([eyeroll.id, gore.id, pending.id, failed.id])))
        await session.execute(delete(Group).where(Group.group_id == NAMED_GROUP))


async def _ids(client: httpx.AsyncClient, **params: str | int) -> list[int]:
    r = await client.get("/api/stickers", params=params)
    assert r.status_code == 200
    return [item["id"] for item in r.json()["items"]]


async def test_search_matches_tag_substrings_and_image_text(client: httpx.AsyncClient, rows: Rows) -> None:
    assert await _ids(client, q=f"t{rows.tag}"[:-1]) == [rows.eyeroll]  # partial tag
    assert await _ids(client, q=f"字{rows.tag}") == [rows.gore]


async def test_flagged_and_group_filters(client: httpx.AsyncClient, rows: Rows) -> None:
    flagged = await _ids(client, flagged="true", limit=200)
    assert rows.gore in flagged
    assert rows.eyeroll not in flagged
    r = await client.get("/api/stickers", params={"group_id": GROUP})
    assert [(i["id"], i["group_count"]) for i in r.json()["items"]] == [(rows.eyeroll, 2)]


async def test_failed_stickers_are_hidden_unless_asked_for(client: httpx.AsyncClient, rows: Rows) -> None:
    assert rows.failed not in await _ids(client, limit=200, order="recent")
    assert rows.failed in await _ids(client, status="failed", limit=200, order="recent")
    assert rows.failed in await _ids(client, include_failed="true", limit=200, order="recent")


async def test_detail_lists_each_group_most_used_first(client: httpx.AsyncClient, rows: Rows) -> None:
    r = await client.get(f"/api/stickers/{rows.eyeroll}")
    assert r.status_code == 200
    body = r.json()
    assert [(g["group_id"], g["group_name"], g["count"]) for g in body["groups"]] == [
        (NAMED_GROUP, "测试群", 5),
        (GROUP, "", 3),
    ]
    assert body["group_count"] == 2
    assert (await client.get(f"/api/stickers/{rows.failed}")).json()["error"] == "too_large"
    assert (await client.get("/api/stickers/0")).status_code == 404


async def test_image_is_served_from_the_saved_original(client: httpx.AsyncClient, rows: Rows) -> None:
    r = await client.get(f"/api/stickers/{rows.eyeroll}/image")
    assert (r.status_code, r.headers["content-type"], r.content) == (200, "image/png", b"\x89PNG-bytes")
    assert (await client.get(f"/api/stickers/{rows.pending}/image")).status_code == 404

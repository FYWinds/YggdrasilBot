"""Sticker browsing in the admin API against the local database."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import delete

from yggdrasil.db.models import Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session

GROUP = 990041


@dataclass
class Rows:
    tag: str  # unique search term, so rows left by other runs never match
    eyeroll: int
    gore: int
    pending: int


def _image(**kw: object) -> Sticker:
    return Sticker(kind="image", ref=uuid.uuid4().hex.upper(), summary="动画表情", **kw)


@pytest.fixture
async def rows() -> AsyncIterator[Rows]:
    tag = uuid.uuid4().hex[:8]
    eyeroll = _image(status="ready", description="翻白眼", emotions=["无语", f"t{tag}"], mime="image/png")
    gore = _image(status="ready", description="血腥", ocr_text=f"字{tag}", unsafe_category="gore")
    pending = _image(status="fetching")
    async with get_session() as session:
        session.add_all([eyeroll, gore, pending])
        await session.flush()
        session.add(StickerBlob(sticker_id=eyeroll.id, data=b"\x89PNG-bytes"))
        session.add(StickerUsage(sticker_id=eyeroll.id, group_id=GROUP, count=3))
    yield Rows(tag, eyeroll.id, gore.id, pending.id)
    async with get_session() as session:
        await session.execute(delete(Sticker).where(Sticker.id.in_([eyeroll.id, gore.id, pending.id])))


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
    assert [(i["id"], i["group_count"]) for i in r.json()["items"]] == [(rows.eyeroll, 1)]


async def test_image_is_served_from_the_saved_original(client: httpx.AsyncClient, rows: Rows) -> None:
    r = await client.get(f"/api/stickers/{rows.eyeroll}/image")
    assert (r.status_code, r.headers["content-type"], r.content) == (200, "image/png", b"\x89PNG-bytes")
    assert (await client.get(f"/api/stickers/{rows.pending}/image")).status_code == 404

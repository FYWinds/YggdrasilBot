"""Transcripts for the console: the stored conversation plus what the pipeline did around it, ready to render.

``build`` merges events (moderation verdicts, the final outcome) into the entries by time, resolves a ``src`` for
every image segment of the QQ messages it shows (the bytes the run kept, a stored sticker, an mface from QQ's CDN,
else the original QQ link) and falls back to a few synthetic entries for runs recorded before transcripts existed.
"""

import copy
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import select, tuple_

from yggdrasil.admin.common import not_found
from yggdrasil.db.models import Sticker, StickerBlob, Transcript
from yggdrasil.db.session import get_session
from yggdrasil.stickers.collect import sighting
from yggdrasil.transcript import store
from yggdrasil.transcript.notes import image_key
from yggdrasil.transcript.schema import Entry, QQContextDisplay, QQForwardDisplay, QQMessage

router = APIRouter(prefix="/transcripts", tags=["transcripts"])

BLOB_URL = "/api/transcripts/blobs/{}"
STICKER_URL = "/api/stickers/{}/image"


class BotOut(BaseModel):
    user_id: int
    name: str


class TranscriptOut(BaseModel):
    available: bool  # False: nothing was recorded (the run predates transcripts); entries are a stand-in
    source: str
    content: Literal["full", "metadata"]
    live: bool = False  # the run is still going; poll for more
    entries: list[Entry]
    messages: dict[str, QQMessage] = {}  # QQ message snapshots by messages.id, image segments with ``src``
    names: dict[str, str] = {}  # latest group card by QQ number, for @mentions and senders
    bot: BotOut | None = None  # the bot's QQ number and persona name when the run recorded them
    updated_at: datetime | None = None


def _mface_src(data: dict[str, Any]) -> str | None:
    if url := data.get("url"):
        return str(url)
    emoji = str(data.get("emoji_id") or "")
    return f"https://gxh.vip.qq.com/club/item/parcel/item/{emoji[:2]}/{emoji}/raw300.gif" if emoji else None


async def _stickers(segments: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Stored custom stickers among these segments: ``(kind, ref) -> sticker id`` (only those with an image)."""
    keys = {(s.kind, s.ref) for seg in segments if (s := sighting(seg)) is not None and s.kind == "image"}
    if not keys:
        return {}
    async with get_session() as session:
        rows = await session.execute(
            select(Sticker.kind, Sticker.ref, Sticker.id)
            .join(StickerBlob, StickerBlob.sticker_id == Sticker.id)
            .where(tuple_(Sticker.kind, Sticker.ref).in_(keys))
        )
        return {(k, r): i for k, r, i in rows.all()}


class _Sources:
    def __init__(self, image_keys: dict[str, str], stickers: dict[tuple[str, str], int]) -> None:
        self._keys, self._stickers = image_keys, stickers

    def segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = copy.deepcopy(segments)
        for seg in out:
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            if seg.get("type") == "mface":
                data["src"] = _mface_src(data)
            elif seg.get("type") == "image":
                key = image_key(data)
                s = sighting(seg)
                if key and key in self._keys:
                    data["src"] = BLOB_URL.format(self._keys[key])
                elif s is not None and (sid := self._stickers.get((s.kind, s.ref))) is not None:
                    data["src"] = STICKER_URL.format(sid)
                else:
                    data["src"] = data.get("url")
        return out

    def message(self, m: QQMessage) -> QQMessage:
        return m.model_copy(update={"segments": self.segments(m.segments)})


def _merge(items: list[Entry], events: list[Entry]) -> list[Entry]:
    """Insert each event before the first entry that happened after it (events without a time go last)."""
    out = list(items)
    for event in sorted(events, key=lambda e: (e.at is None, e.at or datetime.min)):
        idx = len(out)
        if event.at is not None:
            idx = next((i for i, e in enumerate(out) if e.at is not None and e.at > event.at), len(out))
        out.insert(idx, event)
    return out


async def build(
    row: Transcript | None, *, events: list[Entry], fallback: list[Entry], live: bool = False
) -> TranscriptOut:
    if row is None:
        return TranscriptOut(
            available=False, source="none", content="full", live=live, entries=_merge(fallback, events)
        )
    items = [Entry.model_validate(e) for e in row.entries]
    context = row.context or {}
    messages = {k: QQMessage.model_validate(v) for k, v in (context.get("messages") or {}).items()}
    # Every QQ message the page will show, for one sticker lookup.
    shown: list[list[dict[str, Any]]] = [m.segments for m in messages.values()]
    for e in items:
        if isinstance(e.display, QQContextDisplay):
            shown += [m.segments for m in (e.display.trigger, e.display.quoted) if m is not None]
        elif isinstance(e.display, QQForwardDisplay):
            shown += [n.segments for n in e.display.nodes]
    sources = _Sources(context.get("image_keys") or {}, await _stickers([s for segs in shown for s in segs]))
    for e in items:
        if isinstance(e.display, QQContextDisplay):
            d = e.display
            e.display = d.model_copy(
                update={
                    "trigger": sources.message(d.trigger) if d.trigger else None,
                    "quoted": sources.message(d.quoted) if d.quoted else None,
                }
            )
        elif isinstance(e.display, QQForwardDisplay):
            for node in e.display.nodes:
                node.segments = sources.segments(node.segments)
    return TranscriptOut(
        available=True,
        source=row.source,
        content="metadata" if row.content == "metadata" else "full",
        live=live,
        entries=_merge(items, events),
        messages={k: sources.message(m) for k, m in messages.items()},
        names=context.get("names") or {},
        bot=BotOut.model_validate(context["bot"]) if context.get("bot") else None,
        updated_at=row.updated_at,
    )


@router.get("/blobs/{sha256}", response_class=Response, responses={200: {"content": {"image/*": {}}}})
async def transcript_blob(sha256: str) -> Response:
    found = await store.blob(sha256)
    if found is None:
        raise not_found("图片")
    return Response(
        found.data, media_type=found.mime, headers={"Cache-Control": "private, max-age=31536000, immutable"}
    )

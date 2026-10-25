"""Sticker collection in the bot process: record each sticker sighting and save the image while its link is fresh.

QQ image links carry an rkey that rotates roughly hourly, and every link signed with the old rkey stops working at
once, so a custom sticker's image is downloaded right after the message is stored; if the link already fails, the
protocol side re-signs it by file name (``get_image``). A later sighting retries stickers whose download failed
(except oversized ones).
"""

import asyncio
import io
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image
from sqlalchemy import Update, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.agent.media import download
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.ingest.normalize import is_sticker_image
from yggdrasil.tools.errors import ToolError

MAX_BYTES = 5 * 1024 * 1024
TOO_LARGE = "too_large"
_MD5_FILE = re.compile(r"^([0-9A-Fa-f]{32})(?:\.\w+)?$")
_CONCURRENCY = 4

type UsageKey = tuple[str, str, int]
"""(kind, ref, group_id)."""


@dataclass(slots=True)
class Sighting:
    """One distinct sticker seen in a batch of messages."""

    kind: str
    ref: str
    summary: str
    url: str = ""  # image: the download link from the newest sighting
    file: str = ""  # image: QQ file name, for re-signing the link
    emoji_id: str | None = None
    package_id: str | None = None
    key: str = ""


@dataclass(frozen=True, slots=True)
class PendingFetch:
    sticker_id: int
    url: str
    file: str


def sighting(segment: dict[str, Any]) -> Sighting | None:
    """The sticker in a raw OneBot segment, or ``None`` if it is not one."""
    data = segment.get("data") or {}
    summary = str(data.get("summary") or "").strip("[]")
    if segment.get("type") == "mface" and data.get("emoji_id") and data.get("emoji_package_id"):
        package, emoji = str(data["emoji_package_id"]), str(data["emoji_id"])
        return Sighting(
            "mface", f"{package}:{emoji}", summary, emoji_id=emoji, package_id=package, key=str(data.get("key", ""))
        )
    if segment.get("type") == "image" and is_sticker_image(data):
        match = _MD5_FILE.match(str(data.get("file") or ""))
        if match is None:
            return None  # no stable identity to deduplicate on
        return Sighting(
            "image", match.group(1).upper(), summary, url=str(data.get("url") or ""), file=str(data["file"])
        )
    return None


async def record(
    session: AsyncSession, seen: dict[tuple[str, str], Sighting], usage: Counter[UsageKey]
) -> list[PendingFetch]:
    """Upsert the batch's stickers and per-group counts; returns the images that still need downloading."""
    counts = Counter[tuple[str, str]]()
    for (kind, ref, _group), n in usage.items():
        counts[kind, ref] += n
    stmt = insert(Sticker).values(
        [
            {
                "kind": s.kind,
                "ref": s.ref,
                "summary": s.summary,
                "emoji_id": s.emoji_id,
                "package_id": s.package_id,
                "key": s.key,
                "status": "fetching" if s.kind == "image" else "ready",
                "seen_count": counts[k],
            }
            for k, s in seen.items()
        ]
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=["kind", "ref"],
        set_={
            "seen_count": Sticker.seen_count + stmt.excluded.seen_count,
            "last_seen": func.now(),
            "summary": stmt.excluded.summary,
            "key": stmt.excluded.key,
        },
    ).returning(Sticker.id, Sticker.kind, Sticker.ref, Sticker.status, Sticker.error)
    rows = (await session.execute(upsert)).all()
    ids = {(kind, ref): sid for sid, kind, ref, _, _ in rows}
    usage_stmt = insert(StickerUsage).values(
        [{"sticker_id": ids[kind, ref], "group_id": g, "count": n} for (kind, ref, g), n in usage.items()]
    )
    await session.execute(
        usage_stmt.on_conflict_do_update(
            index_elements=["sticker_id", "group_id"],
            set_={"count": StickerUsage.count + usage_stmt.excluded.count, "last_seen": func.now()},
        )
    )
    # A failed download is retried with the newer link; an oversized image is not worth fetching again.
    unfetched = [
        (sid, kind, ref)
        for sid, kind, ref, status, error in rows
        if kind == "image" and status in ("fetching", "failed") and error != TOO_LARGE
    ]
    if not unfetched:
        return []
    blob_ids = select(StickerBlob.sticker_id).where(StickerBlob.sticker_id.in_([sid for sid, _, _ in unfetched]))
    have_blob = set((await session.scalars(blob_ids)).all())
    return [
        PendingFetch(sid, seen[kind, ref].url, seen[kind, ref].file)
        for sid, kind, ref in unfetched
        if sid not in have_blob
    ]


class Collector:
    """Downloads sticker images in the background, a few at a time, one task per sticker."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._inflight: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, items: list[PendingFetch]) -> None:
        for item in items:
            if item.sticker_id in self._inflight:
                continue
            self._inflight.add(item.sticker_id)
            task = asyncio.create_task(self._fetch(item), name=f"sticker-fetch-{item.sticker_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for scheduled downloads (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _fetch(self, item: PendingFetch) -> None:
        try:
            async with self._sem:
                blob = await fetch_image(item)
                mime, width, height, frames = await asyncio.to_thread(inspect_image, blob)
            async with get_session() as session:
                await session.execute(
                    insert(StickerBlob).values(sticker_id=item.sticker_id, data=blob).on_conflict_do_nothing()
                )
                await session.execute(
                    _unfetched(item.sticker_id).values(
                        status="untagged",
                        error="",
                        mime=mime,
                        size_bytes=len(blob),
                        width=width,
                        height=height,
                        frames=frames,
                    )
                )
        except Exception as exc:
            LOGGER.warning("sticker {} download failed: {!r}", item.sticker_id, exc)
            error = TOO_LARGE if isinstance(exc, ToolError) else f"download: {exc}"[:300]
            async with get_session() as session:
                await session.execute(_unfetched(item.sticker_id).values(status="failed", error=error))
        finally:
            self._inflight.discard(item.sticker_id)


def _unfetched(sticker_id: int) -> Update:
    return update(Sticker).where(Sticker.id == sticker_id, Sticker.status.in_(("fetching", "failed")))


async def fetch_image(item: PendingFetch) -> bytes:
    """The sticker image; re-signs the link through the protocol side when it no longer works."""
    if item.url:
        try:
            return await download(item.url, MAX_BYTES)
        except httpx.HTTPStatusError as exc:
            if not 400 <= exc.response.status_code < 500:
                raise
    from yggdrasil.bot.state import any_bot
    from yggdrasil.napcat.api import NapCatAPI

    bot = any_bot()
    if bot is None:
        raise RuntimeError("link expired and no bot connection to re-sign it")
    fresh = (await NapCatAPI(bot).get_image(file=item.file)).url
    if not fresh or fresh == item.url:
        raise RuntimeError("link expired and could not be re-signed")
    return await download(fresh, MAX_BYTES)


def inspect_image(blob: bytes) -> tuple[str, int, int, int]:
    """(mime, width, height, frames); raises for data that is not an image."""
    with Image.open(io.BytesIO(blob)) as img:
        mime = Image.MIME.get(img.format or "", "")
        if not mime:
            raise ValueError(f"unsupported image format {img.format!r}")
        return mime, img.width, img.height, int(getattr(img, "n_frames", 1))


COLLECTOR = Collector()

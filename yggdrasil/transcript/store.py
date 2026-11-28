"""Persist transcripts and their images.

Invocations are written while they run (:class:`LiveWriter`, so the console can follow a run) and once more when
the run ends (any outcome). Jobs grow while they run: new entries are appended with ``entries || :new``. Saving
never raises into the caller: a transcript is a record, not part of the reply.
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Message, Transcript, TranscriptBlob
from yggdrasil.db.session import get_session
from yggdrasil.people import names_in_groups
from yggdrasil.transcript.capture import TranscriptRecorder, entries
from yggdrasil.transcript.notes import Blob, TraceNotes
from yggdrasil.transcript.schema import VERSION, Entry, QQForwardDisplay, QQMessage


def dump(items: list[Entry]) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json", exclude_none=True) for e in items]


def qq_from_row(m: Message) -> QQMessage:
    return QQMessage(
        id=m.id,
        qq_message_id=m.qq_message_id,
        user_id=m.user_id,
        sender_name=m.sender_name,
        sent_at=m.sent_at,
        segments=list(m.segments),
        is_self=m.is_self,
        recalled=m.recalled,
    )


async def snapshot_messages(ids: set[int]) -> dict[str, QQMessage]:
    if not ids:
        return {}
    async with get_session() as session:
        rows = (await session.scalars(select(Message).where(Message.id.in_(ids)))).all()
    return {str(m.id): qq_from_row(m) for m in rows}


def _mentioned(segments: list[dict[str, Any]]) -> set[int]:
    out: set[int] = set()
    for seg in segments:
        uid = (seg.get("data") or {}).get("user_id") if seg.get("type") == "mention" else None
        if isinstance(uid, int) and uid > 0:
            out.add(uid)
    return out


async def _names(group_id: int | None, messages: list[QQMessage]) -> dict[str, str]:
    """Latest group card of every sender and @mentioned member, so the console can label them."""
    if group_id is None:
        return {}
    uids = {m.user_id for m in messages} | {u for m in messages for u in _mentioned(m.segments)}
    names = await names_in_groups((group_id, u) for u in uids)
    return {str(u): n for (_, u), n in names.items()}


async def store_blobs(blobs: dict[str, Blob]) -> None:
    if not blobs:
        return
    rows = [{"sha256": sha, "mime": b.mime, "size": len(b.data), "data": b.data} for sha, b in blobs.items()]
    async with get_session() as session:
        await session.execute(pg_insert(TranscriptBlob).values(rows).on_conflict_do_nothing(index_elements=["sha256"]))


async def build_context(notes: TraceNotes, group_id: int | None, items: list[Entry]) -> dict[str, Any]:
    """What the entries point at: message snapshots, group cards, the image keys whose bytes we kept."""
    context: dict[str, Any] = {"version": VERSION, "group_id": group_id}
    if notes.bot:
        context["bot"] = {"user_id": notes.bot[0], "name": notes.bot[1]}
    if not notes.full:
        return context
    ids = set(notes.message_ids)
    if notes.context:
        ids.update(notes.context.recent)
    messages = await snapshot_messages(ids)
    shown = list(messages.values())
    if notes.context:
        shown += [m for m in (notes.context.trigger, notes.context.quoted) if m is not None]
    for e in items:
        if isinstance(e.display, QQForwardDisplay):
            shown += [QQMessage(user_id=n.user_id, segments=n.segments) for n in e.display.nodes]
    context["messages"] = {k: m.model_dump(mode="json") for k, m in messages.items()}
    names = await _names(group_id, shown)
    if notes.bot:
        names[str(notes.bot[0])] = notes.bot[1]  # the bot has no group card history; label it with its persona
    context["names"] = names
    context["image_keys"] = notes.image_keys
    return context


async def save_invocation(
    invocation_id: UUID, recorder: TranscriptRecorder, notes: TraceNotes, group_id: int | None, *, live: bool = False
) -> None:
    """Write the run's transcript, replacing what an earlier call wrote; ``live`` while the run is still going."""
    try:
        items = entries(recorder, notes, live=live)
        context = await build_context(notes, group_id, items)
        new = {sha: b for sha, b in notes.blobs.items() if sha not in notes.stored}
        await store_blobs(new)
        notes.stored.update(new)
        payload = dump(items)
        values = {
            "invocation_id": invocation_id,
            "source": "pydantic-ai",
            "content": "full" if notes.full else "metadata",
            "entries": payload,
            "context": context,
            "size_bytes": len(json.dumps(payload, ensure_ascii=False)) + len(json.dumps(context, ensure_ascii=False)),
        }
        stmt = pg_insert(Transcript).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["invocation_id"],
            set_={k: stmt.excluded[k] for k in ("entries", "context", "size_bytes", "content")}
            | {"updated_at": func.now()},
        )
        async with get_session() as session:
            await session.execute(stmt)
    except Exception:
        LOGGER.exception("saving the transcript of invocation {} failed", invocation_id)


LIVE_INTERVAL_S = 2.0  # the console polls a running invocation every 3 s


class LiveWriter:
    """Writes a running invocation's transcript again whenever it changed: ``poke()`` after every step.

    One write at a time, ``interval`` apart, each with the state of that moment, so a burst of steps costs one
    write. ``stop()`` before the final save: nothing of this may land after it.
    """

    def __init__(self, write: Callable[[], Awaitable[None]], interval: float = LIVE_INTERVAL_S) -> None:
        self._write, self._interval = write, interval
        self._dirty = False
        self._stopped = False
        self._task: asyncio.Task[None] | None = None

    def poke(self) -> None:
        self._dirty = True
        if not self._stopped and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._dirty:
            self._dirty = False
            try:
                await self._write()
            except Exception:
                LOGGER.exception("writing a running transcript failed")
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


async def append_job(
    job_id: str, items: list[Entry], blobs: dict[str, Blob], *, full: bool, source: str = "omp"
) -> None:
    """Add entries to a job's transcript, creating it on first use."""
    if not items:
        return
    try:
        await store_blobs(blobs)
        payload = dump(items)
        size = len(json.dumps(payload, ensure_ascii=False))
        stmt = pg_insert(Transcript).values(
            job_id=job_id,
            source=source,
            content="full" if full else "metadata",
            entries=payload,
            context={"version": VERSION},
            size_bytes=size,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["job_id"],
            set_={
                "entries": Transcript.entries.op("||")(stmt.excluded.entries),
                "size_bytes": Transcript.size_bytes + size,
                "updated_at": func.now(),
            },
        )
        async with get_session() as session:
            await session.execute(stmt)
    except Exception:
        LOGGER.exception("appending to the transcript of job {} failed", job_id)


async def for_invocation(invocation_id: UUID) -> Transcript | None:
    async with get_session() as session:
        return await session.scalar(select(Transcript).where(Transcript.invocation_id == invocation_id))


async def for_job(job_id: str) -> Transcript | None:
    async with get_session() as session:
        return await session.scalar(select(Transcript).where(Transcript.job_id == job_id))


async def blob(sha256: str) -> TranscriptBlob | None:
    async with get_session() as session:
        return await session.get(TranscriptBlob, sha256)

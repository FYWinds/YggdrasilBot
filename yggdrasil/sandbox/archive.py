"""Finished background jobs, kept for good in ``sandbox_jobs`` (running ones live in Redis, see ``jobs``)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from yggdrasil.db.models import SandboxJob
from yggdrasil.db.session import get_session
from yggdrasil.sandbox.jobs import Job, JobEvent


@dataclass(frozen=True)
class ArchivedJob:
    job: Job
    events: list[JobEvent]  # oldest first


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


async def archive_job(job: Job, events: list[JobEvent]) -> None:
    """Store (or refresh) a finished job with its whole event log, ``events`` oldest first."""
    values: dict[str, Any] = {
        "id": job.id,
        "group_id": job.group_id,
        "user_id": job.user_id,
        "kind": job.kind,
        "workspace": job.workspace,
        "title": job.title,
        "status": job.status,
        "created_at": _ts(job.created_at),
        "ended_at": _ts(job.ended_at) if job.ended_at is not None else None,
        "record": job.to_hash(),
        "events": [{"at": e.at, "text": e.text} for e in events],
    }
    stmt = insert(SandboxJob).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={**{k: stmt.excluded[k] for k in values if k != "id"}, "archived_at": func.now()},
    )
    async with get_session() as session:
        await session.execute(stmt)


def _job(record: dict[str, Any]) -> Job | None:
    return Job.from_hash({k: str(v) for k, v in record.items()})


async def get(job_id: str) -> ArchivedJob | None:
    async with get_session() as session:
        query = select(SandboxJob.record, SandboxJob.events).where(SandboxJob.id == job_id)
        row = (await session.execute(query)).first()
    if row is None or (job := _job(row.record)) is None:
        return None
    return ArchivedJob(job, [JobEvent(at=float(e["at"]), text=str(e["text"])) for e in row.events])


def matches(job: Job, q: str) -> bool:
    """The console's text search, for jobs still in Redis (same fields as :func:`search`)."""
    needle = q.lower()
    return any(needle in s.lower() for s in (job.id, job.title, job.spec, job.result))


async def search(
    *,
    group_id: int | None = None,
    statuses: Collection[str] | None = None,
    kind: str | None = None,
    q: str | None = None,
    exclude: Collection[str] = (),
    limit: int,
    offset: int,
) -> tuple[list[Job], int]:
    """Archived jobs newest first, without their events; and how many match."""
    cond: list[ColumnElement[bool]] = []
    if group_id is not None:
        cond.append(SandboxJob.group_id == group_id)
    if statuses is not None:
        cond.append(SandboxJob.status.in_(list(statuses)))
    if kind is not None:
        cond.append(SandboxJob.kind == kind)
    if exclude:
        cond.append(SandboxJob.id.not_in(list(exclude)))
    if q:
        like = f"%{q}%"
        cond.append(
            or_(
                SandboxJob.id.ilike(like),
                SandboxJob.title.ilike(like),
                SandboxJob.record["spec"].astext.ilike(like),
                SandboxJob.record["result"].astext.ilike(like),
            )
        )
    async with get_session() as session:
        total = await session.scalar(select(func.count()).select_from(SandboxJob).where(*cond)) or 0
        records = (
            await session.scalars(
                select(SandboxJob.record)
                .where(*cond)
                .order_by(SandboxJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    return [j for r in records if (j := _job(r)) is not None], total

"""Sandbox workspaces (a few per QQ group, one per topic): list them, destroy one or all; background jobs: list,
details and cancel (running ones from Redis, finished ones from the permanent archive)."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, Page, change, not_found, unavailable
from yggdrasil.sandbox import WorkspaceInfo, WorkspaceManager, archive, manager
from yggdrasil.sandbox.jobs import Job, JobEvent, runner

type JobStatusFilter = Literal["running", "succeeded", "failed", "timeout", "cancelled"]
_STATUSES: dict[str, tuple[str, ...]] = {"running": ("starting", "running")}

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class LinkOut(BaseModel):
    port: int
    url: str
    command: str
    pid: int | None
    created_at: datetime


class WorkspaceOut(BaseModel):
    group_id: int
    name: str
    description: str
    pending: bool  # reserved or reset: no pod yet, created on first use
    claim: str | None
    sandbox: str | None
    pod_ip: str
    created_at: datetime
    last_used: datetime
    expires_at: datetime | None
    max_until: datetime | None
    active: int
    recreate: bool
    links: list[LinkOut]
    jobs: list[str]  # ids of the background jobs running in it


class DestroyedOut(BaseModel):
    destroyed: int


class JobEventOut(BaseModel):
    at: datetime
    text: str


class JobUsageOut(BaseModel):
    requests: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int


class JobLinkOut(BaseModel):
    port: int
    url: str


class JobOut(BaseModel):
    id: str
    kind: str
    group_id: int
    workspace: str
    user_id: int
    title: str
    status: str
    created_at: datetime
    deadline: datetime
    ended_at: datetime | None
    progress: str
    error: str
    model: str
    usage: JobUsageOut | None
    links: list[JobLinkOut]
    cancel: str
    report: str


class JobDetail(JobOut):
    spec: str  # the script (command) or the task (agent)
    result: str
    images: list[str]
    report_invocation: str | None
    events: list[JobEventOut]  # oldest first


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _out(w: WorkspaceInfo) -> WorkspaceOut:
    return WorkspaceOut(
        group_id=w.group_id,
        name=w.name,
        description=w.description,
        pending=w.pending,
        claim=w.claim or None,
        sandbox=w.sandbox,
        pod_ip=w.pod_ip,
        created_at=_ts(w.created_at),
        last_used=_ts(w.last_used),
        expires_at=None if w.pending else _ts(w.expires_at),
        max_until=None if w.pending else _ts(w.max_until),
        active=w.active,
        recreate=w.recreate,
        links=[
            LinkOut(port=x.port, url=x.url, command=x.command, pid=x.pid, created_at=_ts(x.created_at)) for x in w.links
        ],
        jobs=list(w.jobs),
    )


def _job_out(job: Job) -> JobOut:
    u = job.usage
    return JobOut(
        id=job.id,
        kind=job.kind,
        group_id=job.group_id,
        workspace=job.workspace,
        user_id=job.user_id,
        title=job.title,
        status=job.status,
        created_at=_ts(job.created_at),
        deadline=_ts(job.deadline),
        ended_at=_ts(job.ended_at) if job.ended_at is not None else None,
        progress=job.progress,
        error=job.error,
        model=job.model,
        usage=JobUsageOut(
            requests=u.get("requests", 0),
            input_tokens=u.get("input_tokens", 0),
            cached_tokens=u.get("cached_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
        )
        if u
        else None,
        links=[JobLinkOut(port=int(x.get("port", 0)), url=str(x.get("url", ""))) for x in job.links],
        cancel=job.cancel,
        report=job.report,
    )


def _job_detail(job: Job, events: list[JobEvent]) -> JobDetail:
    return JobDetail(
        **_job_out(job).model_dump(),
        spec=job.spec,
        result=job.result,
        images=[x.get("path", "") for x in job.images],
        report_invocation=job.report_invocation or None,
        events=[JobEventOut(at=_ts(e.at), text=e.text) for e in events],
    )


def _manager() -> WorkspaceManager:
    mgr = manager()
    if not mgr.configured:
        raise unavailable("工作区未配置（没有设置 YGG_SANDBOX_TOKEN）")
    return mgr


@router.get("/workspaces")
async def list_workspaces() -> Page[WorkspaceOut]:
    """Every group's workspaces, grouped by group (most recently used group first), each group's most recently
    used first."""
    items = await _manager().list()
    latest: dict[int, float] = {}
    for w in items:
        latest[w.group_id] = max(latest.get(w.group_id, 0.0), w.last_used)
    items.sort(key=lambda w: (-latest[w.group_id], w.group_id, -w.last_used))
    return Page[WorkspaceOut](items=[_out(w) for w in items], total=len(items))


@router.delete("/workspaces/{group_id}/{name}")
async def destroy_workspace(group_id: int, name: str, actor: Actor) -> Ok:
    """Destroy one workspace now (its files, links and record), even while an invocation or job is using it."""
    mgr = _manager()
    async with change(actor, "sandbox", "destroy", f"{group_id}/{name}"):
        if not await mgr.destroy(group_id, name, reason=f"admin {actor}"):
            raise not_found("工作区")
    return Ok()


@router.post("/workspaces/destroy-all")
async def destroy_all_workspaces(actor: Actor) -> DestroyedOut:
    mgr = _manager()
    async with change(actor, "sandbox", "destroy_all") as c:
        n = await mgr.destroy_all(reason=f"admin {actor}")
        c.detail = {"destroyed": n}
    return DestroyedOut(destroyed=n)


@router.get("/jobs")
async def list_jobs(
    group_id: int | None = None,
    status: JobStatusFilter | None = None,
    kind: Literal["command", "agent"] | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[JobOut]:
    """Background jobs of every group, newest first: those still running (or reporting) first, then the archive."""
    statuses = _STATUSES.get(status, (status,)) if status else None
    active = await runner().active()
    live = [
        j
        for j in active
        if (group_id is None or j.group_id == group_id)
        and (statuses is None or j.status in statuses)
        and (kind is None or j.kind == kind)
        and (not q or archive.matches(j, q))
    ]
    shown = live[offset : offset + limit]
    archived, archived_total = await archive.search(
        group_id=group_id,
        statuses=statuses,
        kind=kind,
        q=q,
        exclude={j.id for j in active},
        limit=limit - len(shown),
        offset=max(0, offset - len(live)),
    )
    return Page[JobOut](items=[_job_out(j) for j in [*shown, *archived]], total=len(live) + archived_total)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> JobDetail:
    r = runner()
    job = await r.get(job_id)
    if job is not None:
        return _job_detail(job, list(reversed(await r.events(job_id))))
    archived = await archive.get(job_id)
    if archived is None:
        raise not_found("后台任务")
    return _job_detail(archived.job, archived.events)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, actor: Actor) -> JobOut:
    """Stop a running job; the poller ends it within seconds and the group still gets its report."""
    _manager()
    r = runner()
    if await r.get(job_id) is None:
        raise not_found("后台任务")
    async with change(actor, "sandbox", "cancel_job", job_id):
        job = await r.cancel(job_id, by=f"admin:{actor}")
    if job is None:
        raise not_found("后台任务")
    return _job_out(job)

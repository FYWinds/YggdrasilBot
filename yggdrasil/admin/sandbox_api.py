"""Sandbox workspaces (one per QQ group): list them, destroy one or all."""

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, Page, change, unavailable
from yggdrasil.sandbox import WorkspaceInfo, WorkspaceManager, manager

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


class LinkOut(BaseModel):
    port: int
    url: str
    command: str
    created_at: datetime


class WorkspaceOut(BaseModel):
    group_id: int
    claim: str
    sandbox: str | None
    pod_ip: str
    created_at: datetime
    last_used: datetime
    expires_at: datetime
    active: int
    recreate: bool
    links: list[LinkOut]


class DestroyedOut(BaseModel):
    destroyed: int


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _out(w: WorkspaceInfo) -> WorkspaceOut:
    return WorkspaceOut(
        group_id=w.group_id,
        claim=w.claim,
        sandbox=w.sandbox,
        pod_ip=w.pod_ip,
        created_at=_ts(w.created_at),
        last_used=_ts(w.last_used),
        expires_at=_ts(w.expires_at),
        active=w.active,
        recreate=w.recreate,
        links=[LinkOut(port=x.port, url=x.url, command=x.command, created_at=_ts(x.created_at)) for x in w.links],
    )


def _manager() -> WorkspaceManager:
    mgr = manager()
    if not mgr.configured:
        raise unavailable("工作区未配置（没有设置 YGG_SANDBOX_TOKEN）")
    return mgr


@router.get("/workspaces")
async def list_workspaces() -> Page[WorkspaceOut]:
    items = sorted(await _manager().list(), key=lambda w: w.last_used, reverse=True)
    return Page[WorkspaceOut](items=[_out(w) for w in items], total=len(items))


@router.delete("/workspaces/{group_id}")
async def destroy_workspace(group_id: int, actor: Actor) -> Ok:
    """Destroy one group's workspace now, even while an invocation is using it."""
    mgr = _manager()
    async with change(actor, "sandbox", "destroy", group_id):
        await mgr.destroy(group_id, reason=f"admin {actor}", force=True)
    return Ok()


@router.post("/workspaces/destroy-all")
async def destroy_all_workspaces(actor: Actor) -> DestroyedOut:
    mgr = _manager()
    async with change(actor, "sandbox", "destroy_all") as c:
        n = await mgr.destroy_all(reason=f"admin {actor}")
        c.detail = {"destroyed": n}
    return DestroyedOut(destroyed=n)

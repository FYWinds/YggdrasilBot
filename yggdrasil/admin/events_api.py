"""Bot events (requests and notices) with request decisions."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Page, change, conflict
from yggdrasil.db.models import BotEvent
from yggdrasil.db.session import get_session
from yggdrasil.events import EventError, decide_request

router = APIRouter(prefix="/events", tags=["events"])

EventStatus = Literal["pending", "approved", "rejected", "expired", "failed"]


class EventOut(BaseModel):
    id: int
    at: datetime
    kind: str
    group_id: int | None
    user_id: int | None
    operator_id: int | None
    data: dict[str, Any]
    status: str | None
    handled_by: str | None
    handled_at: datetime | None


class EventDecideIn(BaseModel):
    approve: bool
    reason: str | None = None


def _out(e: BotEvent) -> EventOut:
    return EventOut(
        id=e.id,
        at=e.at,
        kind=e.kind,
        group_id=e.group_id,
        user_id=e.user_id,
        operator_id=e.operator_id,
        data=dict(e.data),
        status=e.status,
        handled_by=e.handled_by,
        handled_at=e.handled_at,
    )


@router.get("")
async def list_events(
    kind: str | None = Query(None, description="类型前缀，例如 request. 或 notice.group_ban"),
    status: EventStatus | None = None,
    group_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EventOut]:
    cond = []
    if kind:
        cond.append(BotEvent.kind.startswith(kind))
    if status:
        cond.append(BotEvent.status == status)
    if group_id is not None:
        cond.append(BotEvent.group_id == group_id)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(BotEvent).where(*cond)) or 0
        rows = await s.scalars(select(BotEvent).where(*cond).order_by(BotEvent.at.desc()).limit(limit).offset(offset))
        return Page[EventOut](items=[_out(e) for e in rows.all()], total=total)


@router.post("/{event_id}/decide")
async def decide_event(event_id: int, body: EventDecideIn, actor: Actor) -> EventOut:
    try:
        row = await decide_request(event_id, body.approve, actor, body.reason)
    except EventError as e:
        raise conflict(str(e)) from e
    async with change(actor, "event", "approve" if body.approve else "reject", event_id) as c:
        c.detail = {"kind": row.kind, "status": row.status}
    return _out(row)

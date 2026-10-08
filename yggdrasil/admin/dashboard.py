"""Dashboard home: who am I, and 24h counters."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from yggdrasil import runtime
from yggdrasil.admin.auth import Actor
from yggdrasil.config import APP_CONFIG
from yggdrasil.db.models import Group, Invocation, Message, ReviewItem
from yggdrasil.db.session import get_session

router = APIRouter(tags=["dashboard"])


class Me(BaseModel):
    email: str
    environment: str


@router.get("/me")
async def get_me(actor: Actor) -> Me:
    return Me(email=actor, environment=APP_CONFIG.environment)


class GroupCount(BaseModel):
    group_id: int
    name: str
    enabled: bool
    count: int


class StatusCount(BaseModel):
    status: str
    count: int


class Stats(BaseModel):
    messages_24h: list[GroupCount]
    invocations_24h: list[StatusCount]
    pending_reviews: int
    snapshot_version: int


@router.get("/stats")
async def get_stats() -> Stats:
    since = datetime.now(UTC) - timedelta(hours=24)
    async with get_session() as s:
        msg_rows = (
            await s.execute(
                select(Message.group_id, func.count())
                .where(Message.sent_at >= since)
                .group_by(Message.group_id)
                .order_by(func.count().desc())
            )
        ).all()
        groups = {g.group_id: g for g in (await s.scalars(select(Group))).all()}
        inv_rows = (
            await s.execute(
                select(Invocation.status, func.count())
                .where(Invocation.created_at >= since)
                .group_by(Invocation.status)
                .order_by(func.count().desc())
            )
        ).all()
        pending = await s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending"))
    return Stats(
        messages_24h=[
            GroupCount(
                group_id=gid,
                name=groups[gid].name if gid in groups else "",
                enabled=gid in groups and groups[gid].enabled,
                count=n,
            )
            for gid, n in msg_rows
        ],
        invocations_24h=[StatusCount(status=st, count=n) for st, n in inv_rows],
        pending_reviews=pending or 0,
        snapshot_version=runtime.snapshot().version,
    )

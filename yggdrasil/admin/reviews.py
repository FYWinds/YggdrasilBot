"""Moderation review queue."""

from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Page, change, conflict, unavailable
from yggdrasil.db.models import ReviewItem
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/reviews", tags=["reviews"])

ReviewStatus = Literal["pending", "approved", "rejected", "expired"]


class ReviewOut(BaseModel):
    id: int
    created_at: datetime
    invocation_id: UUID | None
    group_id: int
    user_id: int
    request_text: str
    reply: dict[str, Any]
    verdict: dict[str, Any]
    status: str
    decided_by: str | None
    decided_at: datetime | None
    expires_at: datetime


class DecideIn(BaseModel):
    action: Literal["approve", "reject", "edit"]
    text: str | None = None

    @model_validator(mode="after")
    def _edit_needs_text(self) -> Self:
        if self.action == "edit" and not (self.text or "").strip():
            raise ValueError("修改后发送需要填写文本")
        return self


def _out(r: ReviewItem) -> ReviewOut:
    return ReviewOut(
        id=r.id,
        created_at=r.created_at,
        invocation_id=r.invocation_id,
        group_id=r.group_id,
        user_id=r.user_id,
        request_text=r.request_text,
        reply=dict(r.reply),
        verdict=dict(r.verdict),
        status=r.status,
        decided_by=r.decided_by,
        decided_at=r.decided_at,
        expires_at=r.expires_at,
    )


@router.get("")
async def list_reviews(
    status: ReviewStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ReviewOut]:
    cond = [ReviewItem.status == status] if status else []
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(ReviewItem).where(*cond)) or 0
        rows = await s.scalars(
            select(ReviewItem).where(*cond).order_by(ReviewItem.id.desc()).limit(limit).offset(offset)
        )
        return Page[ReviewOut](items=[_out(r) for r in rows.all()], total=total)


@router.post("/{review_id}/decide")
async def decide_review(review_id: int, body: DecideIn, actor: Actor) -> ReviewOut:
    try:
        from yggdrasil.moderation.review import ReviewError, decide
    except ImportError as e:
        raise unavailable(f"审核模块不可用: {e}") from e
    try:
        item = await decide(review_id, body.action, actor, edited_text=body.text if body.action == "edit" else None)
    except ReviewError as e:
        raise conflict(str(e)) from e
    async with change(actor, "review", body.action, review_id) as c:
        c.detail = {"status": item.status, "edited": body.action == "edit"}
    return _out(item)

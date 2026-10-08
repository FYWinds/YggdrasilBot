"""Sticker (market face) pool for quick acks."""

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Page, change, not_found
from yggdrasil.db.models import Sticker
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/stickers", tags=["stickers"])


class StickerOut(BaseModel):
    id: int
    emoji_id: str
    package_id: str
    key: str
    summary: str
    seen_count: int
    enabled: bool
    last_seen: datetime


class StickerEnabledIn(BaseModel):
    enabled: bool


def _out(st: Sticker) -> StickerOut:
    return StickerOut(
        id=st.id,
        emoji_id=st.emoji_id,
        package_id=st.package_id,
        key=st.key,
        summary=st.summary,
        seen_count=st.seen_count,
        enabled=st.enabled,
        last_seen=st.last_seen,
    )


@router.get("")
async def list_stickers(
    enabled: bool | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[StickerOut]:
    cond = []
    if enabled is not None:
        cond.append(Sticker.enabled.is_(enabled))
    if q:
        cond.append(Sticker.summary.ilike(f"%{q}%"))
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Sticker).where(*cond)) or 0
        rows = await s.scalars(
            select(Sticker).where(*cond).order_by(Sticker.seen_count.desc(), Sticker.id).limit(limit).offset(offset)
        )
        items = [_out(st) for st in rows.all()]
    return Page[StickerOut](items=items, total=total)


@router.post("/{sticker_id}/enabled")
async def set_sticker_enabled(sticker_id: int, body: StickerEnabledIn, actor: Actor) -> StickerOut:
    async with change(actor, "sticker", "enable" if body.enabled else "disable", sticker_id) as c:
        st = await c.session.get(Sticker, sticker_id)
        if st is None:
            raise not_found("表情包")
        st.enabled = body.enabled
        c.detail = {"summary": st.summary}
    return _out(st)

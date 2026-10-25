"""Collected stickers: listing, details, the original image, and enable/disable."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from sqlalchemy import ScalarSelect, func, or_, select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Page, change, not_found
from yggdrasil.db.models import Group, Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/stickers", tags=["stickers"])

type StickerKind = Literal["image", "mface"]
type StickerStatus = Literal["fetching", "untagged", "ready", "failed"]


class StickerOut(BaseModel):
    id: int
    kind: StickerKind
    summary: str
    status: StickerStatus
    error: str
    mime: str
    size_bytes: int | None
    width: int | None
    height: int | None
    frames: int | None
    description: str
    usage: str
    ocr_text: str
    emotions: list[str]
    unsafe_category: str
    tagged_at: datetime | None
    seen_count: int
    group_count: int
    enabled: bool
    first_seen: datetime
    last_seen: datetime


class StickerGroupUsage(BaseModel):
    group_id: int
    group_name: str
    count: int
    last_seen: datetime


class StickerDetail(StickerOut):
    ref: str  # image: the md5 QQ uses as file name; mface: "<package_id>:<emoji_id>"
    emoji_id: str | None
    package_id: str | None
    key: str
    tag_attempts: int
    groups: list[StickerGroupUsage]  # most used first


class StickerEnabledIn(BaseModel):
    enabled: bool


def _out(st: Sticker, group_count: int) -> StickerOut:
    return StickerOut(
        id=st.id,
        kind=st.kind,  # pyright: ignore[reportArgumentType]
        summary=st.summary,
        status=st.status,  # pyright: ignore[reportArgumentType]
        error=st.error,
        mime=st.mime,
        size_bytes=st.size_bytes,
        width=st.width,
        height=st.height,
        frames=st.frames,
        description=st.description,
        usage=st.usage,
        ocr_text=st.ocr_text,
        emotions=list(st.emotions),
        unsafe_category=st.unsafe_category,
        tagged_at=st.tagged_at,
        seen_count=st.seen_count,
        group_count=group_count,
        enabled=st.enabled,
        first_seen=st.first_seen,
        last_seen=st.last_seen,
    )


def _group_count() -> ScalarSelect[int]:
    return (
        select(func.count())
        .select_from(StickerUsage)
        .where(StickerUsage.sticker_id == Sticker.id)
        .correlate(Sticker)
        .scalar_subquery()
    )


@router.get("")
async def list_stickers(
    kind: StickerKind | None = None,
    status: StickerStatus | None = None,
    include_failed: bool = Query(False, description="without a status filter, also list failed stickers"),
    enabled: bool | None = None,
    flagged: bool | None = None,
    group_id: int | None = None,
    q: str | None = None,
    order: Literal["seen", "recent"] = "seen",
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[StickerOut]:
    cond = []
    if kind is not None:
        cond.append(Sticker.kind == kind)
    if status is not None:
        cond.append(Sticker.status == status)
    elif not include_failed:
        cond.append(Sticker.status != "failed")
    if enabled is not None:
        cond.append(Sticker.enabled.is_(enabled))
    if group_id is not None:
        cond.append(Sticker.id.in_(select(StickerUsage.sticker_id).where(StickerUsage.group_id == group_id)))
    if flagged is not None:
        cond.append(Sticker.unsafe_category != "" if flagged else Sticker.unsafe_category == "")
    if q:
        like = f"%{q}%"
        cond.append(
            or_(
                Sticker.summary.ilike(like),
                Sticker.description.ilike(like),
                Sticker.usage.ilike(like),
                Sticker.ocr_text.ilike(like),
                func.array_to_string(Sticker.emotions, " ").ilike(like),
            )
        )
    ordering = (Sticker.last_seen.desc(),) if order == "recent" else (Sticker.seen_count.desc(), Sticker.id)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Sticker).where(*cond)) or 0
        rows = await s.execute(
            select(Sticker, _group_count()).where(*cond).order_by(*ordering).limit(limit).offset(offset)
        )
        items = [_out(st, n) for st, n in rows.all()]
    return Page[StickerOut](items=items, total=total)


@router.get("/{sticker_id}")
async def get_sticker(sticker_id: int) -> StickerDetail:
    async with get_session() as s:
        st = await s.get(Sticker, sticker_id)
        if st is None:
            raise not_found("表情包")
        rows = (
            await s.execute(
                select(StickerUsage.group_id, Group.name, StickerUsage.count, StickerUsage.last_seen)
                .outerjoin(Group, Group.group_id == StickerUsage.group_id)
                .where(StickerUsage.sticker_id == sticker_id)
                .order_by(StickerUsage.count.desc(), StickerUsage.group_id)
            )
        ).all()
    groups = [StickerGroupUsage(group_id=g, group_name=name or "", count=n, last_seen=at) for g, name, n, at in rows]
    return StickerDetail(
        **_out(st, len(groups)).model_dump(),
        ref=st.ref,
        emoji_id=st.emoji_id,
        package_id=st.package_id,
        key=st.key,
        tag_attempts=st.tag_attempts,
        groups=groups,
    )


@router.get("/{sticker_id}/image", response_class=Response, responses={200: {"content": {"image/*": {}}}})
async def sticker_image(sticker_id: int) -> Response:
    async with get_session() as s:
        row = (
            await s.execute(
                select(StickerBlob.data, Sticker.mime)
                .join(Sticker, Sticker.id == StickerBlob.sticker_id)
                .where(StickerBlob.sticker_id == sticker_id)
            )
        ).first()
    if row is None:
        raise not_found("表情包图片")
    data, mime = row
    # A sticker's image never changes once saved.
    return Response(
        data, media_type=mime or "application/octet-stream", headers={"Cache-Control": "private, max-age=86400"}
    )


@router.post("/{sticker_id}/enabled")
async def set_sticker_enabled(sticker_id: int, body: StickerEnabledIn, actor: Actor) -> StickerOut:
    async with change(actor, "sticker", "enable" if body.enabled else "disable", sticker_id) as c:
        st = await c.session.get(Sticker, sticker_id)
        if st is None:
            raise not_found("表情包")
        st.enabled = body.enabled
        c.detail = {"summary": st.summary, "description": st.description}
        group_count = await c.session.scalar(
            select(func.count()).select_from(StickerUsage).where(StickerUsage.sticker_id == sticker_id)
        )
    return _out(st, group_count or 0)

"""Group whitelist, names, settings overrides and history backfill."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unavailable
from yggdrasil.admin.settings_api import global_doc, validate_doc
from yggdrasil.db.models import Group
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/groups", tags=["groups"])


class GroupOut(BaseModel):
    group_id: int
    name: str
    enabled: bool
    settings: dict[str, Any]
    backfilled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GroupIn(BaseModel):
    name: str = ""
    enabled: bool = False
    settings: dict[str, Any] | None = Field(None, description="省略则保留原有覆盖")


def _out(g: Group) -> GroupOut:
    return GroupOut(
        group_id=g.group_id,
        name=g.name,
        enabled=g.enabled,
        settings=dict(g.settings),
        backfilled_at=g.backfilled_at,
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


@router.get("")
async def list_groups() -> list[GroupOut]:
    async with get_session() as s:
        return [_out(g) for g in (await s.scalars(select(Group).order_by(Group.group_id))).all()]


@router.put("/{group_id}")
async def upsert_group(group_id: int, body: GroupIn, actor: Actor) -> GroupOut:
    if body.settings is not None:
        validate_doc(await global_doc(), body.settings)
    async with change(actor, "group", "upsert", group_id) as c:
        g = await c.session.get(Group, group_id)
        if g is None:
            g = Group(group_id=group_id, settings={})
            c.session.add(g)
        c.detail = {"before_enabled": g.enabled, "enabled": body.enabled, "name": body.name}
        g.name = body.name.strip()
        g.enabled = body.enabled
        if body.settings is not None:
            g.settings = body.settings
            c.detail["settings"] = body.settings
        await c.session.flush()
        await c.session.refresh(g)
    return _out(g)


@router.delete("/{group_id}")
async def delete_group(group_id: int, actor: Actor) -> Ok:
    async with change(actor, "group", "delete", group_id) as c:
        g = await c.session.get(Group, group_id)
        if g is None:
            raise not_found(f"群 {group_id} ")
        c.detail = {"name": g.name, "settings": dict(g.settings)}
        await c.session.delete(g)
    return Ok()


@router.post("/{group_id}/backfill")
async def backfill_group(group_id: int, actor: Actor) -> Ok:
    try:
        from yggdrasil.backfill import request_backfill
    except ImportError as e:
        raise unavailable(f"回填模块不可用: {e}") from e
    async with get_session() as s:
        if await s.get(Group, group_id) is None:
            raise not_found(f"群 {group_id} ")
    if not request_backfill(group_id):
        raise conflict("这个群的回填正在进行中")
    async with change(actor, "group", "backfill", group_id):
        pass
    return Ok()

"""Admins: ``qq`` superusers (reviews, admin commands) and ``email`` dashboard users."""

from datetime import datetime
from typing import Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found
from yggdrasil.config import APP_CONFIG
from yggdrasil.db.models import Admin
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/admins", tags=["admins"])

AdminKind = Literal["qq", "email"]


class AdminOut(BaseModel):
    kind: AdminKind
    value: str
    note: str
    created_at: datetime | None
    bootstrap: bool  # from environment variables; not removable here


class AdminIn(BaseModel):
    kind: AdminKind
    value: str
    note: str = ""

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        self.value = self.value.strip()
        if self.kind == "qq" and not self.value.isdigit():
            raise ValueError("QQ 号必须是数字")
        if self.kind == "email":
            self.value = self.value.lower()
            if "@" not in self.value:
                raise ValueError("邮箱格式不对")
        return self


@router.get("")
async def list_admins() -> list[AdminOut]:
    async with get_session() as s:
        rows = (await s.scalars(select(Admin).order_by(Admin.kind, Admin.created_at))).all()
    out = [AdminOut(kind=a.kind, value=a.value, note=a.note, created_at=a.created_at, bootstrap=False) for a in rows]  # pyright: ignore[reportArgumentType]
    seen = {(a.kind, a.value) for a in out}
    out += [
        AdminOut(kind="qq", value=str(q), note="YGG_SUPERUSERS", created_at=None, bootstrap=True)
        for q in APP_CONFIG.bootstrap_superusers
        if ("qq", str(q)) not in seen
    ]
    out += [
        AdminOut(kind="email", value=e.lower(), note="YGG_ADMIN_EMAILS", created_at=None, bootstrap=True)
        for e in APP_CONFIG.bootstrap_admin_emails
        if ("email", e.lower()) not in seen
    ]
    return out


@router.post("")
async def add_admin(body: AdminIn, actor: Actor) -> AdminOut:
    try:
        async with change(actor, "admin", "add", f"{body.kind}:{body.value}") as c:
            a = Admin(kind=body.kind, value=body.value, note=body.note)
            c.session.add(a)
            await c.session.flush()
            await c.session.refresh(a)
    except IntegrityError as e:
        raise conflict("已经是管理员") from e
    return AdminOut(kind=body.kind, value=a.value, note=a.note, created_at=a.created_at, bootstrap=False)


@router.delete("/{kind}/{value}")
async def remove_admin(kind: AdminKind, value: str, actor: Actor) -> Ok:
    value = value.strip().lower() if kind == "email" else value.strip()
    if kind == "email" and value == actor:
        raise conflict("不能移除自己")
    async with change(actor, "admin", "remove", f"{kind}:{value}") as c:
        a = await c.session.get(Admin, (kind, value))
        if a is None:
            raise not_found("管理员")
        await c.session.delete(a)
    return Ok()

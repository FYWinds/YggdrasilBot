"""Command trigger rules (``command_triggers``) and the 试匹配 endpoint."""

import re
import string
from datetime import datetime
from typing import Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from yggdrasil import runtime
from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, not_found
from yggdrasil.db.models import CommandTrigger
from yggdrasil.db.session import get_session
from yggdrasil.triggers.index import MATCH_TYPES, ROLES, TARGETS, MatchInput, TriggerIndex

router = APIRouter(prefix="/triggers", tags=["triggers"])

HANDLERS: dict[str, str] = {
    "tldr": "关键词解释和 TL;DR",
    "chat": "普通对话回复",
    "privacy.optout": "不要记我",
    "privacy.optin": "记得我",
    "privacy.forget": "忘记我",
    "admin.review": "私聊审核：通过 / 拒绝 / 修改",
    "admin.status": "私聊状态查询",
    "admin.ops": "超级管理员用自然语言查询和修改配置（私聊）",
}
SCOPES = ("global", "group", "private")
_PATTERN_TYPES = ("keyword_prefix", "keyword_exact", "regex")


class TriggerFields(BaseModel):
    handler: str
    scope: str = "global"
    group_id: int | None = None
    match_type: str
    patterns: list[str] = Field(default_factory=list)
    arg_template: str | None = None
    require_mention: bool = False
    target: str = "quoted_or_self"
    priority: int = 100
    enabled: bool = True
    cooldown_s: int = Field(0, ge=0)
    allowed_roles: list[str] = Field(default_factory=list)
    note: str = ""


class TriggerIn(TriggerFields):
    scope: Literal["global", "group", "private"] = "global"

    @field_validator("handler")
    @classmethod
    def _handler(cls, v: str) -> str:
        if v not in HANDLERS:
            raise ValueError(f"未知 handler {v!r}，可选: {', '.join(HANDLERS)}")
        return v

    @field_validator("match_type")
    @classmethod
    def _match_type(cls, v: str) -> str:
        if v not in MATCH_TYPES:
            raise ValueError(f"未知匹配方式 {v!r}，可选: {', '.join(MATCH_TYPES)}")
        return v

    @field_validator("target")
    @classmethod
    def _target(cls, v: str) -> str:
        if v not in TARGETS:
            raise ValueError(f"未知目标 {v!r}，可选: {', '.join(TARGETS)}")
        return v

    @field_validator("allowed_roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if r not in ROLES]
        if bad:
            raise ValueError(f"未知角色 {bad}，可选: {', '.join(ROLES)}")
        return list(dict.fromkeys(v))

    @field_validator("patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        return [p for p in (p.strip() for p in v) if p]

    @field_validator("arg_template")
    @classmethod
    def _arg_template(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(v) if f is not None]
        except ValueError as e:
            raise ValueError(f"参数模板格式错误: {e}") from e
        for f in fields:
            name = re.split(r"[.\[!:]", f, maxsplit=1)[0]
            if name not in ("", "rest") and not name.isdigit():
                raise ValueError(f"参数模板只能引用 {{0}}、{{1}}… 或 {{rest}}，不能引用 {{{f}}}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.scope == "group" and self.group_id is None:
            raise ValueError("群级规则必须指定 group_id")
        if self.scope != "group" and self.group_id is not None:
            raise ValueError("只有群级规则可以指定 group_id")
        if self.match_type in _PATTERN_TYPES and not self.patterns:
            raise ValueError(f"{self.match_type} 规则至少需要一个 pattern")
        if self.match_type == "regex":
            for p in self.patterns:
                try:
                    re.compile(p, re.IGNORECASE | re.DOTALL)
                except re.error as e:
                    raise ValueError(f"正则 {p!r} 无法编译: {e}") from e
        return self


class TriggerOut(TriggerFields):
    id: int
    created_at: datetime
    updated_at: datetime


def _out(t: CommandTrigger) -> TriggerOut:
    return TriggerOut(
        id=t.id,
        handler=t.handler,
        scope=t.scope,
        group_id=t.group_id,
        match_type=t.match_type,
        patterns=list(t.patterns),
        arg_template=t.arg_template,
        require_mention=t.require_mention,
        target=t.target,
        priority=t.priority,
        enabled=t.enabled,
        cooldown_s=t.cooldown_s,
        allowed_roles=list(t.allowed_roles),
        note=t.note,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _apply(t: CommandTrigger, body: TriggerIn) -> None:
    for key, value in body.model_dump().items():
        setattr(t, key, value)


class HandlerInfo(BaseModel):
    name: str
    description: str


class TriggerMeta(BaseModel):
    handlers: list[HandlerInfo]
    match_types: list[str]
    targets: list[str]
    roles: list[str]
    scopes: list[str]


@router.get("/meta")
async def get_trigger_meta() -> TriggerMeta:
    return TriggerMeta(
        handlers=[HandlerInfo(name=k, description=v) for k, v in HANDLERS.items()],
        match_types=list(MATCH_TYPES),
        targets=list(TARGETS),
        roles=list(ROLES),
        scopes=list(SCOPES),
    )


@router.get("")
async def list_triggers() -> list[TriggerOut]:
    async with get_session() as s:
        rows = await s.scalars(select(CommandTrigger).order_by(CommandTrigger.priority, CommandTrigger.id))
        return [_out(t) for t in rows.all()]


@router.post("")
async def create_trigger(body: TriggerIn, actor: Actor) -> TriggerOut:
    async with change(actor, "trigger", "create") as c:
        t = CommandTrigger()
        _apply(t, body)
        c.session.add(t)
        await c.session.flush()
        await c.session.refresh(t)
        c.entity_id, c.detail = str(t.id), body.model_dump()
    return _out(t)


@router.put("/{trigger_id}")
async def update_trigger(trigger_id: int, body: TriggerIn, actor: Actor) -> TriggerOut:
    async with change(actor, "trigger", "update", trigger_id) as c:
        t = await c.session.get(CommandTrigger, trigger_id)
        if t is None:
            raise not_found("规则")
        _apply(t, body)
        c.detail = body.model_dump()
        await c.session.flush()
        await c.session.refresh(t)
    return _out(t)


@router.delete("/{trigger_id}")
async def delete_trigger(trigger_id: int, actor: Actor) -> Ok:
    async with change(actor, "trigger", "delete", trigger_id) as c:
        t = await c.session.get(CommandTrigger, trigger_id)
        if t is None:
            raise not_found("规则")
        c.detail = {"handler": t.handler, "patterns": list(t.patterns)}
        await c.session.delete(t)
    return Ok()


class TriggerTestIn(BaseModel):
    text: str
    scope: Literal["group", "private"] = "group"
    group_id: int | None = None
    to_me: bool = False
    replied_to_bot: bool = False
    role: Literal["member", "admin", "owner", "superuser"] = "member"


class ResolvedOut(BaseModel):
    handler: str
    rule_id: int
    args: str
    captures: list[str]
    target: str
    cooldown_s: int
    named: bool


class TriggerTestOut(BaseModel):
    resolved: ResolvedOut | None
    snapshot_version: int


@router.post("/test")
async def test_trigger(body: TriggerTestIn) -> TriggerTestOut:
    """Run the exact runtime index against the currently loaded snapshot."""
    snap = runtime.snapshot()
    hit = TriggerIndex(snap).match(
        MatchInput(
            scope=body.scope,
            group_id=body.group_id if body.scope == "group" else None,
            text=body.text,
            to_me=body.to_me or body.scope == "private",
            replied_to_bot=body.replied_to_bot,
            role=body.role,
        )
    )
    resolved = None
    if hit is not None:
        resolved = ResolvedOut(
            handler=hit.handler,
            rule_id=hit.rule_id,
            args=hit.args,
            captures=list(hit.captures),
            target=hit.target,
            cooldown_s=hit.cooldown_s,
            named=hit.named,
        )
    return TriggerTestOut(resolved=resolved, snapshot_version=snap.version)

"""Prompt templates: versioned overrides of the repo defaults."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from jinja2 import TemplateError, meta
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil import prompts
from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import change, not_found
from yggdrasil.db.models import Prompt, PromptVersion
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/prompts", tags=["prompts"])


class PromptSummary(BaseModel):
    key: str
    active_version_id: int | None
    active_created_at: datetime | None
    versions: int


class PromptVersionOut(BaseModel):
    id: int
    body: str
    note: str
    created_by: str
    created_at: datetime


class PromptDetail(BaseModel):
    key: str
    default_body: str
    active_version_id: int | None
    variables: list[str] = Field(description="模板引用的变量（默认模板与当前生效版本的并集）")
    versions: list[PromptVersionOut]


class PromptVersionIn(BaseModel):
    body: str = Field(min_length=1)
    note: str = ""
    activate: bool = True


class ActivateIn(BaseModel):
    version_id: int


class PreviewIn(BaseModel):
    body: str | None = Field(None, description="省略则渲染当前生效版本")
    variables: dict[str, Any] = Field(default_factory=dict)


class PreviewOut(BaseModel):
    rendered: str


def _check_key(key: str) -> str:
    try:
        return prompts.default_body(key)
    except KeyError:
        raise not_found(f"提示词 {key} ") from None


def _variables(*bodies: str) -> list[str]:
    out: set[str] = set()
    for body in bodies:
        try:
            out |= meta.find_undeclared_variables(prompts._env.parse(body))
        except TemplateError:
            continue
    return sorted(out)


def _syntax_error(e: TemplateError) -> HTTPException:
    line = getattr(e, "lineno", None)
    where = f"第 {line} 行: " if line else ""
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        [{"loc": ["body", "body"], "msg": f"模板语法错误 {where}{e}", "type": "template_syntax"}],
    )


@router.get("")
async def list_prompts() -> list[PromptSummary]:
    keys = prompts.default_keys()
    async with get_session() as s:
        active = {
            key: (vid, at)
            for key, vid, at in (
                await s.execute(
                    select(Prompt.key, Prompt.active_version_id, PromptVersion.created_at).join(
                        PromptVersion, PromptVersion.id == Prompt.active_version_id, isouter=True
                    )
                )
            ).all()
        }
        counts = dict(
            (await s.execute(select(PromptVersion.key, func.count()).group_by(PromptVersion.key))).tuples().all()
        )
    return [
        PromptSummary(
            key=k,
            active_version_id=active.get(k, (None, None))[0],
            active_created_at=active.get(k, (None, None))[1],
            versions=counts.get(k, 0),
        )
        for k in keys
    ]


@router.get("/{key}")
async def get_prompt(key: str) -> PromptDetail:
    default = _check_key(key)
    async with get_session() as s:
        p = await s.get(Prompt, key)
        versions = (
            await s.scalars(select(PromptVersion).where(PromptVersion.key == key).order_by(PromptVersion.id.desc()))
        ).all()
    active_id = p.active_version_id if p else None
    active_body = next((v.body for v in versions if v.id == active_id), default)
    return PromptDetail(
        key=key,
        default_body=default,
        active_version_id=active_id,
        variables=_variables(default, active_body),
        versions=[
            PromptVersionOut(id=v.id, body=v.body, note=v.note, created_by=v.created_by, created_at=v.created_at)
            for v in versions
        ],
    )


async def _set_active(session: AsyncSession, key: str, version_id: int | None) -> None:
    p = await session.get(Prompt, key)
    if p is None:
        session.add(Prompt(key=key, active_version_id=version_id))
    else:
        p.active_version_id = version_id


@router.post("/{key}/versions")
async def create_prompt_version(key: str, body: PromptVersionIn, actor: Actor) -> PromptVersionOut:
    _check_key(key)
    try:
        prompts._env.parse(body.body)
    except TemplateError as e:
        raise _syntax_error(e) from e
    async with change(actor, "prompt", "create_version", key) as c:
        v = PromptVersion(key=key, body=body.body, note=body.note, created_by=actor)
        c.session.add(v)
        await c.session.flush()
        await c.session.refresh(v)
        if body.activate:
            await _set_active(c.session, key, v.id)
        c.detail = {"version_id": v.id, "activated": body.activate, "note": body.note}
    return PromptVersionOut(id=v.id, body=v.body, note=v.note, created_by=v.created_by, created_at=v.created_at)


@router.post("/{key}/activate")
async def activate_prompt_version(key: str, body: ActivateIn, actor: Actor) -> PromptDetail:
    _check_key(key)
    async with change(actor, "prompt", "activate", key) as c:
        v = await c.session.get(PromptVersion, body.version_id)
        if v is None or v.key != key:
            raise not_found(f"版本 {body.version_id} ")
        await _set_active(c.session, key, v.id)
        c.detail = {"version_id": v.id}
    return await get_prompt(key)


@router.post("/{key}/reset")
async def reset_prompt(key: str, actor: Actor) -> PromptDetail:
    """Go back to the repo default (versions are kept for rollback)."""
    _check_key(key)
    async with change(actor, "prompt", "reset", key) as c:
        p = await c.session.get(Prompt, key)
        if p is not None:
            c.detail = {"previous_version_id": p.active_version_id}
            p.active_version_id = None
    return await get_prompt(key)


@router.post("/{key}/preview")
async def preview_prompt(key: str, body: PreviewIn) -> PreviewOut:
    default = _check_key(key)
    source = body.body
    if source is None:
        async with get_session() as s:
            source = await s.scalar(
                select(PromptVersion.body)
                .join(Prompt, Prompt.active_version_id == PromptVersion.id)
                .where(Prompt.key == key)
            )
        source = source or default
    try:
        rendered = prompts._compile(source).render(**body.variables).strip()
    except TemplateError as e:
        raise _syntax_error(e) from e
    return PreviewOut(rendered=rendered)

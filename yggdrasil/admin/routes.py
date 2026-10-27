"""Task → model routes and embedding spaces."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, not_found, unavailable, unprocessable
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import EmbedSpace, Provider, TaskRoute
from yggdrasil.db.session import get_session
from yggdrasil.llm.catalog import UPSTREAM_PREFIX, ModelCaps
from yggdrasil.llm.registry import TASK_NEEDS, TASKS

_NEED_LABELS = {"vision": "看图", "tools": "工具调用", "reasoning": "思考", "image_output": "出图"}
_THINKING_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}

router = APIRouter(tags=["routes"])


class RouteLimits(BaseModel):
    request_limit: int | None = Field(default=None, ge=1)
    tool_calls_limit: int | None = Field(default=None, ge=0)
    total_tokens_limit: int | None = Field(default=None, ge=1)


class RouteIn(BaseModel):
    provider_id: int
    model: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict, description="temperature, max_tokens, top_p, extra_body …")
    fallback_provider_id: int | None = None
    fallback_model: str | None = None
    limits: RouteLimits = RouteLimits()


class RouteOut(RouteIn):
    updated_at: datetime


class TaskRouteRow(BaseModel):
    task: str
    route: RouteOut | None


def _route_out(r: TaskRoute) -> RouteOut:
    return RouteOut(
        provider_id=r.provider_id,
        model=r.model,
        params=dict(r.params),
        fallback_provider_id=r.fallback_provider_id,
        fallback_model=r.fallback_model,
        limits=RouteLimits.model_validate(r.limits or {}),
        updated_at=r.updated_at,
    )


def _check_task(task: str) -> None:
    if task not in TASKS:
        raise not_found(f"任务 {task} ")


@router.get("/routes")
async def list_routes() -> list[TaskRouteRow]:
    async with get_session() as s:
        rows = {r.task: r for r in (await s.scalars(select(TaskRoute))).all()}
    return [TaskRouteRow(task=t, route=_route_out(rows[t]) if t in rows else None) for t in TASKS]


def _thinking_needs(params: dict[str, Any]) -> tuple[str, ...]:
    """``params.thinking`` (a level; unset = model default) asks for a reasoning model.

    No "off": Claude 5 rejects ``thinking.type=disabled`` and pydantic-ai sends nothing for
    ``false``, so it would silently mean "default".
    """
    level = params.get("thinking")
    if level is None:
        return ()
    if level in _THINKING_LEVELS:
        return ("reasoning",)
    raise unprocessable("thinking 只能是 minimal / low / medium / high / xhigh，不设置表示由模型决定")


def _missing_caps(task: str, p: Provider, model: str, extra: tuple[str, ...] = ()) -> str | None:
    """Why ``model`` can't serve ``task`` per its known capabilities; None if fine or unknown."""
    raw = (p.model_caps or {}).get(model)
    if raw is None:
        return None
    caps = ModelCaps.model_validate(raw)
    missing = [_NEED_LABELS[n] for n in (*TASK_NEEDS.get(task, ()), *extra) if not getattr(caps, n)]
    if not missing:
        return None
    source = "上游 /models" if caps.catalog_id.startswith(UPSTREAM_PREFIX) else f"models.dev: {caps.catalog_id}"
    return f"{p.name}/{model}（{source}）不支持{'、'.join(missing)}，不能用于 {task}"


@router.put("/routes/{task}")
async def upsert_route(task: str, body: RouteIn, actor: Actor) -> TaskRouteRow:
    _check_task(task)
    fallback_model = (body.fallback_model or "").strip() or None
    fallback_provider_id = body.fallback_provider_id if fallback_model else None
    thinking = _thinking_needs(body.params)
    if task == "agent" and (body.params or body.limits.model_dump(exclude_none=True)):
        raise unprocessable("agent 路由的参数和限制由工作区里的 agent 自己决定（预算在设置里），这里不能设置")
    async with change(actor, "route", "upsert", task) as c:
        for pid, model in {(body.provider_id, body.model.strip()), (fallback_provider_id, fallback_model)}:
            if pid is None or model is None:
                continue
            p = await c.session.get(Provider, pid)
            if p is None:
                raise not_found(f"provider {pid} ")
            if task == "agent" and p.api_format != "anthropic":
                raise unprocessable(f"agent 路由只能用 Anthropic 格式的 provider，{p.name} 是 {p.api_format}")
            if why := _missing_caps(task, p, model, thinking):
                raise unprocessable(why)
        r = await c.session.get(TaskRoute, task)
        if r is None:
            r = TaskRoute(task=task)
            c.session.add(r)
        r.provider_id = body.provider_id
        r.model = body.model.strip()
        r.params = body.params
        r.fallback_provider_id = fallback_provider_id
        r.fallback_model = fallback_model
        r.limits = body.limits.model_dump(exclude_none=True)
        c.detail = {"provider_id": r.provider_id, "model": r.model, "fallback_model": fallback_model}
        await c.session.flush()
        await c.session.refresh(r)
    return TaskRouteRow(task=task, route=_route_out(r))


@router.delete("/routes/{task}")
async def delete_route(task: str, actor: Actor) -> Ok:
    _check_task(task)
    async with change(actor, "route", "delete", task) as c:
        r = await c.session.get(TaskRoute, task)
        if r is None:
            raise not_found(f"任务 {task} 的路由")
        await c.session.delete(r)
    return Ok()


class EmbedSpaceOut(BaseModel):
    id: int
    provider_id: int
    provider_name: str | None
    model: str
    dim: int
    active: bool
    status: str
    created_at: datetime
    updated_at: datetime


class EmbedSpaceIn(BaseModel):
    provider_id: int
    model: str = Field(min_length=1)


@router.get("/embed-spaces")
async def list_embed_spaces() -> list[EmbedSpaceOut]:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(EmbedSpace, Provider.name)
                .join(Provider, Provider.id == EmbedSpace.provider_id, isouter=True)
                .order_by(EmbedSpace.id.desc())
            )
        ).all()
    return [
        EmbedSpaceOut(
            id=e.id,
            provider_id=e.provider_id,
            provider_name=name,
            model=e.model,
            dim=e.dim,
            active=e.active,
            status=e.status,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
        for e, name in rows
    ]


async def _kick_reembed(space_id: int) -> None:
    try:
        from yggdrasil.worker.tasks import reembed_space
    except ImportError as e:
        raise unavailable(f"worker 任务模块不可用: {e}") from e
    try:
        await reembed_space.kiq(space_id)
    except Exception as e:
        raise unavailable(f"重嵌入任务入队失败: {e}") from e


@router.post("/embed-spaces")
async def create_embed_space(body: EmbedSpaceIn, actor: Actor) -> EmbedSpaceOut:
    """Create a (not yet active) space and enqueue re-embedding of all memory into it."""
    async with change(actor, "embed_space", "create") as c:
        provider = await c.session.get(Provider, body.provider_id)
        if provider is None:
            raise not_found(f"provider {body.provider_id} ")
        e = EmbedSpace(provider_id=body.provider_id, model=body.model.strip(), dim=EMBED_DIM, active=False)
        c.session.add(e)
        await c.session.flush()
        await c.session.refresh(e)
        c.entity_id, c.detail = str(e.id), {"provider_id": e.provider_id, "model": e.model}
    await _kick_reembed(e.id)
    return EmbedSpaceOut(
        id=e.id,
        provider_id=e.provider_id,
        provider_name=provider.name,
        model=e.model,
        dim=e.dim,
        active=e.active,
        status=e.status,
        created_at=e.created_at,
        updated_at=e.updated_at,
    )


@router.post("/embed-spaces/{space_id}/reembed")
async def reembed_embed_space(space_id: int, actor: Actor) -> Ok:
    """Re-enqueue re-embedding (e.g. after the worker was down when the space was created)."""
    async with change(actor, "embed_space", "reembed", space_id) as c:
        if await c.session.get(EmbedSpace, space_id) is None:
            raise not_found("向量空间")
    await _kick_reembed(space_id)
    return Ok()

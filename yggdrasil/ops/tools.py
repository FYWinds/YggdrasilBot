"""Tools of the ops agent. Only superusers ever get them (checked before the agent is built).

Writes go through the dashboard's own handlers, so validation, the ``config_audit`` row (actor
``qq:<uid>``) and the hot reload are identical to the dashboard. Each undoable write pushes the
previous state onto a per-user Redis stack that ``undo_last_change`` pops.
"""

import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal

from fastapi import HTTPException
from nonebot.adapters.onebot.v11 import Bot
from pydantic import BaseModel
from pydantic_ai import RunContext, Tool
from sqlalchemy import func, select

from yggdrasil.admin import groups as groups_api
from yggdrasil.admin import reviews as reviews_api
from yggdrasil.admin import routes as routes_api
from yggdrasil.admin import settings_api
from yggdrasil.admin import triggers as triggers_api
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.db.models import CommandTrigger, Group, Invocation, Provider, ReviewItem, TaskRoute
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

_UNDO_KEEP = 20
_UNDO_TTL_S = 7 * 86400


@dataclass
class QuotedMessage:
    qq_message_id: int
    is_self: bool
    text: str


@dataclass
class OpsDeps:
    bot: Bot
    api: NapCatAPI
    user_id: int
    group_id: int | None
    quoted: QuotedMessage | None = None
    actions: list[str] = field(default_factory=list)
    tool_counts: Counter[str] = field(default_factory=Counter)

    @property
    def actor(self) -> str:
        return f"qq:{self.user_id}"


def _op[**P](name: str, *, write: bool = False) -> Callable[[Callable[P, Awaitable[str]]], Callable[P, Awaitable[str]]]:
    def deco(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
            ctx = args[0]
            assert isinstance(ctx, RunContext)
            ctx.deps.tool_counts[name] += 1
            started, outcome = time.perf_counter(), "ok"
            try:
                result = await fn(*args, **kwargs)
                if write:
                    ctx.deps.actions.append(result)
                return result
            except HTTPException as e:
                outcome = "error"
                return f"[失败] {_detail(e.detail)}"
            except ToolError as e:
                outcome = "error"
                return f"[失败] {e}"
            except Exception as e:
                outcome = "error"
                LOGGER.opt(exception=e).warning("ops tool {} failed", name)
                return f"[失败] {type(e).__name__}: {str(e)[:300]}"
            finally:
                TOOL_CALLS.add(1, {"tool": f"ops.{name}", "outcome": outcome})
                TOOL_DURATION.record(time.perf_counter() - started, {"tool": f"ops.{name}"})

        return wrapper

    return deco


def _detail(detail: Any) -> str:
    """FastAPI error detail (string or validation list) as one line."""
    if isinstance(detail, list):
        return "；".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
    return str(detail)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --- undo stack ----------------------------------------------------------------


def _undo_key(user_id: int) -> str:
    return f"ygg:ops:undo:{user_id}"


async def _push_undo(deps: OpsDeps, label: str, step: dict[str, Any]) -> None:
    r = redis_client()
    key = _undo_key(deps.user_id)
    await r.lpush(key, _json({"label": label, **step}))
    await r.ltrim(key, 0, _UNDO_KEEP - 1)
    await r.expire(key, _UNDO_TTL_S)


def merge_patch(doc: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7386-style: nested dicts merge, ``None`` removes the key (back to the inherited value)."""
    out = dict(doc)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = merge_patch(out[key], value)
            if merged:
                out[key] = merged
            else:
                out.pop(key)
        else:
            out[key] = value
    return out


# --- read ----------------------------------------------------------------------


@_op("status")
async def status(ctx: RunContext[OpsDeps]) -> str:
    """当前配置版本、启用的群、已配置的路由、待审核数量。"""
    snap = snapshot()
    async with get_session() as s:
        pending = await s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending"))
    groups = [f"{g.name}({g.group_id})" for g in snap.groups.values() if g.enabled]
    return _json(
        {
            "config_version": snap.version,
            "enabled_groups": groups,
            "routes": sorted(snap.routes),
            "pending_reviews": pending,
        }
    )


@_op("get_settings")
async def get_settings(ctx: RunContext[OpsDeps], group_id: int | None = None, section: str | None = None) -> str:
    """查看设置。group_id 为空看全局；section 指定一节（persona、interaction、moderation、ratelimit、context、
    tools、memory、privacy）时额外返回这一节的生效值。stored 是手动改过的部分，没写的项用默认值或全局值。"""
    base = await settings_api.global_doc()
    stored = base
    if group_id is not None:
        async with get_session() as s:
            g = await s.get(Group, group_id)
            if g is None:
                raise ToolError(f"群 {group_id} 不在列表里")
            stored = dict(g.settings)
    out: dict[str, Any] = {"scope": f"group {group_id}" if group_id is not None else "global", "stored": stored}
    if section:
        effective = settings_api.effective_settings(base, stored if group_id is not None else None)
        if section not in Settings.model_fields:
            raise ToolError(f"没有 {section} 这一节，可选：{', '.join(Settings.model_fields)}")
        out["effective"] = {section: getattr(effective, section).model_dump(mode="json")}
    return _json(out)


@_op("settings_reference")
async def settings_reference(ctx: RunContext[OpsDeps], section: str) -> str:
    """某一节设置有哪些字段：类型、默认值、说明。改设置前先查字段名。"""
    info = Settings.model_fields.get(section)
    model = info.annotation if info else None
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ToolError(f"没有 {section} 这一节，可选：{', '.join(Settings.model_fields)}")
    defaults = model().model_dump(mode="json")
    return _json(
        {
            name: {
                "type": str(f.annotation).replace("typing.", ""),
                "default": defaults.get(name),
                "desc": f.description,
            }
            for name, f in model.model_fields.items()
        }
    )


@_op("list_groups")
async def list_groups(ctx: RunContext[OpsDeps]) -> str:
    """所有已知的群：群号、名称、是否启用、有没有单群设置覆盖。"""
    groups = await groups_api.list_groups()
    return _json(
        [{"group_id": g.group_id, "name": g.name, "enabled": g.enabled, "overrides": g.settings} for g in groups]
    )


@_op("list_routes")
async def list_routes(ctx: RunContext[OpsDeps]) -> str:
    """每个任务用的 provider、模型和兜底模型，以及可用的 provider 和它们的模型列表。"""
    snap = snapshot()
    names = {p.id: p.name for p in snap.providers.values()}
    routes = {
        t: {
            "provider": names.get(r.provider_id),
            "model": r.model,
            "fallback": f"{names.get(r.fallback_provider_id or 0)}/{r.fallback_model}" if r.fallback_model else None,
        }
        for t, r in snap.routes.items()
    }
    providers = {p.name: list(p.models) for p in snap.providers.values() if p.enabled}
    return _json({"routes": routes, "providers": providers})


@_op("list_triggers")
async def list_triggers(ctx: RunContext[OpsDeps]) -> str:
    """所有触发规则（id、handler、范围、匹配方式、关键词、优先级、是否启用）。"""
    rows = await triggers_api.list_triggers()
    return _json(
        [
            t.model_dump(
                include={
                    "id",
                    "handler",
                    "scope",
                    "group_id",
                    "match_type",
                    "patterns",
                    "priority",
                    "enabled",
                    "allowed_roles",
                    "note",
                }
            )
            for t in rows
        ]
    )


@_op("list_reviews")
async def list_reviews(
    ctx: RunContext[OpsDeps], status: Literal["pending", "approved", "rejected", "expired"] = "pending"
) -> str:
    """审核队列（默认只看待审核）：R 号、群、请求、候选回复开头、审核结果。"""
    page = await reviews_api.list_reviews(status=status, limit=20, offset=0)
    return _json(
        [
            {
                "id": f"R{r.id}",
                "group_id": r.group_id,
                "request": r.request_text[:200],
                "reply": " ".join(s.get("content", "") for s in r.reply.get("sections", []))[:200],
                "verdict": {k: r.verdict.get(k) for k in ("verdict", "score", "categories")},
            }
            for r in page.items
        ]
    )


@_op("recent_invocations")
async def recent_invocations(ctx: RunContext[OpsDeps], limit: int = 10) -> str:
    """最近的调用记录：时间、群、触发人、handler、状态、耗时、错误。"""
    async with get_session() as s:
        rows = await s.scalars(select(Invocation).order_by(Invocation.created_at.desc()).limit(min(max(limit, 1), 30)))
        return _json(
            [
                {
                    "at": i.created_at.astimezone().strftime("%m-%d %H:%M"),
                    "group": i.group_id,
                    "user": i.user_id,
                    "handler": i.handler,
                    "status": i.status,
                    "ms": i.duration_ms,
                    "error": (i.error or "")[:200] or None,
                }
                for i in rows.all()
            ]
        )


# --- write ---------------------------------------------------------------------


@_op("update_settings", write=True)
async def update_settings(ctx: RunContext[OpsDeps], patch: dict[str, Any], group_id: int | None = None) -> str:
    """修改设置。patch 是按节嵌套的部分设置，例如 {"moderation": {"default_rule": {"threshold": 0.9}}}；
    值写 null 表示删掉这项手动设置（恢复默认，或单群恢复成全局值）。group_id 为空改全局，否则只改这个群。"""
    if group_id is None:
        before = await settings_api.global_doc()
        after = merge_patch(before, patch)
        await settings_api.put_global_settings(after, ctx.deps.actor)
    else:
        async with get_session() as s:
            g = await s.get(Group, group_id)
            if g is None:
                raise ToolError(f"群 {group_id} 不在列表里")
            before = dict(g.settings)
        after = merge_patch(before, patch)
        await settings_api.put_group_settings(group_id, after, ctx.deps.actor)
    scope = "全局" if group_id is None else f"群 {group_id} "
    await _push_undo(ctx.deps, f"{scope}设置", {"kind": "settings", "group_id": group_id, "doc": before})
    return f"已修改{scope}设置：{_json(patch)}"


@_op("set_group", write=True)
async def set_group(
    ctx: RunContext[OpsDeps], group_id: int, enabled: bool | None = None, name: str | None = None
) -> str:
    """启用或停用一个群（不在列表里的会新建），或者改群名。只改传入的项。"""
    async with get_session() as s:
        g = await s.get(Group, group_id)
        prev = {"name": g.name, "enabled": g.enabled} if g else None
    if prev is None and enabled is None:
        raise ToolError(f"群 {group_id} 不在列表里，要新增请同时指定 enabled")
    body = groups_api.GroupIn(
        name=name if name is not None else (prev or {}).get("name", ""),
        enabled=enabled if enabled is not None else bool(prev and prev["enabled"]),
        settings=None,
    )
    out = await groups_api.upsert_group(group_id, body, ctx.deps.actor)
    await _push_undo(ctx.deps, f"群 {group_id}", {"kind": "group", "group_id": group_id, "prev": prev})
    return f"群 {out.group_id}（{out.name}）：{'启用' if out.enabled else '停用'}"


@_op("backfill_group", write=True)
async def backfill_group(ctx: RunContext[OpsDeps], group_id: int) -> str:
    """回填一个群的历史消息（后台进行，数量由 memory.backfill_messages 决定）。不能撤销。"""
    await groups_api.backfill_group(group_id, ctx.deps.actor)
    return f"已开始回填群 {group_id}"


@_op("decide_review", write=True)
async def decide_review(
    ctx: RunContext[OpsDeps],
    action: Literal["approve", "reject", "edit"],
    review_id: int | None = None,
    text: str | None = None,
) -> str:
    """处理审核：approve 通过并发送原回复，reject 拒绝，edit 把 text 作为回复发出。review_id 不写时，
    用被回复的审核通知里的 R 号；还没有就取唯一一条待审核。不能撤销。"""
    if review_id is None and ctx.deps.quoted and (m := re.search(r"R(\d+)", ctx.deps.quoted.text)):
        review_id = int(m.group(1))
    if review_id is None:
        page = await reviews_api.list_reviews(status="pending", limit=5, offset=0)
        if page.total != 1:
            raise ToolError(f"待审核有 {page.total} 条，请说明是哪一条（R 号）")
        review_id = page.items[0].id
    out = await reviews_api.decide_review(review_id, reviews_api.DecideIn(action=action, text=text), ctx.deps.actor)
    return f"R{out.id} → {out.status}"


class TriggerSpec(triggers_api.TriggerIn):
    """Same fields and checks as the dashboard's trigger form."""


@_op("save_trigger", write=True)
async def save_trigger(ctx: RunContext[OpsDeps], trigger: TriggerSpec, trigger_id: int | None = None) -> str:
    """新增（trigger_id 为空）或修改一条触发规则。修改时要传完整的规则，先用 list_triggers 看原值。"""
    prev = None
    if trigger_id is not None:
        async with get_session() as s:
            row = await s.get(CommandTrigger, trigger_id)
            if row is None:
                raise ToolError(f"规则 {trigger_id} 不存在")
            prev = triggers_api.TriggerIn.model_validate(row, from_attributes=True).model_dump()
        out = await triggers_api.update_trigger(trigger_id, trigger, ctx.deps.actor)
    else:
        out = await triggers_api.create_trigger(trigger, ctx.deps.actor)
    await _push_undo(ctx.deps, f"触发规则 {out.id}", {"kind": "trigger", "id": out.id, "prev": prev})
    return f"规则 {out.id} 已保存"


@_op("delete_trigger", write=True)
async def delete_trigger(ctx: RunContext[OpsDeps], trigger_id: int) -> str:
    """删除一条触发规则。"""
    async with get_session() as s:
        row = await s.get(CommandTrigger, trigger_id)
        if row is None:
            raise ToolError(f"规则 {trigger_id} 不存在")
        prev = triggers_api.TriggerIn.model_validate(row, from_attributes=True).model_dump()
    await triggers_api.delete_trigger(trigger_id, ctx.deps.actor)
    await _push_undo(ctx.deps, f"删除规则 {trigger_id}", {"kind": "trigger", "id": None, "prev": prev})
    return f"规则 {trigger_id} 已删除"


async def _provider_id(name: str) -> int:
    async with get_session() as s:
        pid = await s.scalar(select(Provider.id).where(Provider.name == name))
    if pid is None:
        raise ToolError(f"没有叫 {name} 的 provider")
    return pid


@_op("set_route", write=True)
async def set_route(
    ctx: RunContext[OpsDeps],
    task: str,
    provider: str,
    model: str,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
) -> str:
    """更换某个任务的模型（provider 用名称）。不写兜底就去掉兜底；参数和限制保持不变。会按模型能力校验。"""
    async with get_session() as s:
        row = await s.get(TaskRoute, task)
        prev = (
            routes_api.RouteIn(
                provider_id=row.provider_id,
                model=row.model,
                params=dict(row.params),
                fallback_provider_id=row.fallback_provider_id,
                fallback_model=row.fallback_model,
                limits=routes_api.RouteLimits(**row.limits),
            ).model_dump()
            if row
            else None
        )
    body = routes_api.RouteIn(
        provider_id=await _provider_id(provider),
        model=model,
        params=(prev or {}).get("params", {}),
        fallback_provider_id=await _provider_id(fallback_provider) if fallback_provider and fallback_model else None,
        fallback_model=fallback_model if fallback_provider else None,
        limits=routes_api.RouteLimits(**(prev or {}).get("limits", {})),
    )
    await routes_api.upsert_route(task, body, ctx.deps.actor)
    await _push_undo(ctx.deps, f"路由 {task}", {"kind": "route", "task": task, "prev": prev})
    return f"{task} → {provider}/{model}"


@_op("undo_last_change", write=True)
async def undo_last_change(ctx: RunContext[OpsDeps]) -> str:
    """撤销你最近一次通过对话做的配置修改（设置、群、触发规则、路由）。连续调用会继续往前撤。审核和回填不能撤销。"""
    raw = await redis_client().lpop(_undo_key(ctx.deps.user_id))
    if raw is None:
        raise ToolError("没有可以撤销的修改")
    step = json.loads(str(raw))
    actor, kind = ctx.deps.actor, step["kind"]
    if kind == "settings":
        if step["group_id"] is None:
            await settings_api.put_global_settings(step["doc"], actor)
        else:
            await settings_api.put_group_settings(step["group_id"], step["doc"], actor)
    elif kind == "group":
        if step["prev"] is None:
            await groups_api.delete_group(step["group_id"], actor)
        else:
            await groups_api.upsert_group(step["group_id"], groups_api.GroupIn(**step["prev"]), actor)
    elif kind == "trigger":
        if step["prev"] is None:
            await triggers_api.delete_trigger(step["id"], actor)
        elif step["id"] is None:
            await triggers_api.create_trigger(triggers_api.TriggerIn(**step["prev"]), actor)
        else:
            await triggers_api.update_trigger(step["id"], triggers_api.TriggerIn(**step["prev"]), actor)
    elif kind == "route":
        if step["prev"] is None:
            await routes_api.delete_route(step["task"], actor)
        else:
            await routes_api.upsert_route(step["task"], routes_api.RouteIn(**step["prev"]), actor)
    return f"已撤销：{step['label']}"


@_op("recall_message", write=True)
async def recall_message(ctx: RunContext[OpsDeps]) -> str:
    """撤回被回复的那条 bot 消息。"""
    q = ctx.deps.quoted
    if q is None or not q.is_self or ctx.deps.group_id is None:
        raise ToolError("没有回复 bot 在群里发的消息")
    await ctx.deps.api.delete_msg(message_id=q.qq_message_id)
    return "已撤回"


_READ: tuple[Callable[..., Awaitable[str]], ...] = (
    status,
    get_settings,
    settings_reference,
    list_groups,
    list_routes,
    list_triggers,
    list_reviews,
    recent_invocations,
)
_WRITE: tuple[Callable[..., Awaitable[str]], ...] = (
    update_settings,
    set_group,
    backfill_group,
    decide_review,
    save_trigger,
    delete_trigger,
    set_route,
    undo_last_change,
)


def ops_tools(deps: OpsDeps) -> list[Tool[OpsDeps]]:
    tools: list[Tool[OpsDeps]] = [Tool(fn, takes_ctx=True) for fn in (*_READ, *_WRITE)]
    if deps.quoted is not None and deps.quoted.is_self and deps.group_id is not None:
        tools.append(Tool(recall_message, takes_ctx=True))
    return tools

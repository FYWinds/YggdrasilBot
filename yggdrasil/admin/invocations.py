"""Invocation log with Tempo trace links, and each invocation's transcript."""

import json
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from yggdrasil.admin import transcripts
from yggdrasil.admin.common import Page, VerdictOut, not_found
from yggdrasil.agent.schema import BotReply, OpsReply
from yggdrasil.config import APP_CONFIG
from yggdrasil.db.models import Invocation, ModerationResult
from yggdrasil.db.session import get_session
from yggdrasil.people import names_in_groups
from yggdrasil.transcript import store
from yggdrasil.transcript.schema import Entry, TextPart

router = APIRouter(prefix="/invocations", tags=["invocations"])


def trace_url(trace_id: str | None) -> str | None:
    """Grafana Explore link opening the trace in the Tempo datasource (uid ``tempo``)."""
    if not trace_id or not APP_CONFIG.grafana_url:
        return None
    left = {
        "datasource": "tempo",
        "queries": [
            {"refId": "A", "datasource": {"type": "tempo", "uid": "tempo"}, "queryType": "traceql", "query": trace_id}
        ],
        "range": {"from": "now-30d", "to": "now"},
    }
    return f"{APP_CONFIG.grafana_url.rstrip('/')}/explore?left={quote(json.dumps(left, separators=(',', ':')))}"


class InvocationSummary(BaseModel):
    id: UUID
    created_at: datetime
    group_id: int | None
    user_id: int
    user_name: str  # latest group card / nickname; "" when unknown (private chats)
    handler: str
    status: str
    request_text: str
    models: list[str]  # route labels (provider/model), the run's model first
    tokens_in: int
    tokens_out: int
    duration_ms: int | None
    ack_kind: str | None
    trace_id: str | None
    trace_url: str | None
    error: str | None


class ModerationOut(BaseModel):
    id: int
    created_at: datetime
    stage: str
    verdict: str
    score: float
    categories: list[str]
    action: str
    model: str
    latency_ms: int


class InvocationDetail(InvocationSummary):
    trigger_qq_message_id: int | None
    tokens_cached: int
    tool_calls: dict[str, int]  # calls per tool
    ack_latency_ms: int | None
    input_verdict: VerdictOut | None
    output_verdict: VerdictOut | None
    reply: BotReply | OpsReply | None  # OpsReply when handler is "ops"
    moderation: list[ModerationOut]


def _reply(i: Invocation) -> BotReply | OpsReply | None:
    if not i.reply:
        return None
    try:
        return OpsReply.model_validate(i.reply) if i.handler == "ops" else BotReply.model_validate(i.reply)
    except ValidationError:
        return None


def _verdict(raw: dict[str, Any] | None) -> VerdictOut | None:
    try:
        return VerdictOut.model_validate(raw) if raw else None
    except ValidationError:
        return None


def _summary_fields(i: Invocation, names: dict[tuple[int, int], str]) -> dict[str, Any]:
    return {
        "id": i.id,
        "created_at": i.created_at,
        "group_id": i.group_id,
        "user_id": i.user_id,
        "user_name": names.get((i.group_id or 0, i.user_id), ""),
        "handler": i.handler,
        "status": i.status,
        "request_text": i.request_text,
        "models": [m if isinstance(m, str) else json.dumps(m, ensure_ascii=False) for m in i.models],
        "tokens_in": i.tokens_in,
        "tokens_out": i.tokens_out,
        "duration_ms": i.duration_ms,
        "ack_kind": i.ack_kind,
        "trace_id": i.trace_id,
        "trace_url": trace_url(i.trace_id),
        "error": i.error,
    }


@router.get("")
async def list_invocations(
    q: str | None = None,
    group_id: int | None = None,
    handler: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[InvocationSummary]:
    cond = []
    if q:
        cond.append(Invocation.request_text.ilike(f"%{q}%"))
    if group_id is not None:
        cond.append(Invocation.group_id == group_id)
    if handler:
        cond.append(Invocation.handler == handler)
    if status:
        cond.append(Invocation.status == status)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Invocation).where(*cond)) or 0
        rows = (
            await s.scalars(
                select(Invocation).where(*cond).order_by(Invocation.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
    names = await names_in_groups((i.group_id, i.user_id) for i in rows)
    items = [InvocationSummary(**_summary_fields(i, names)) for i in rows]
    return Page[InvocationSummary](items=items, total=total)


@router.get("/{invocation_id}")
async def get_invocation(invocation_id: UUID) -> InvocationDetail:
    async with get_session() as s:
        i = await s.get(Invocation, invocation_id)
        if i is None:
            raise not_found("调用记录")
        mods = await s.scalars(
            select(ModerationResult)
            .where(ModerationResult.invocation_id == invocation_id)
            .order_by(ModerationResult.id)
        )
        moderation = [
            ModerationOut(
                id=m.id,
                created_at=m.created_at,
                stage=m.stage,
                verdict=m.verdict,
                score=m.score,
                categories=list(m.categories),
                action=m.action,
                model=m.model,
                latency_ms=m.latency_ms,
            )
            for m in mods.all()
        ]
    return InvocationDetail(
        **_summary_fields(i, await names_in_groups([(i.group_id, i.user_id)])),
        trigger_qq_message_id=i.trigger_qq_message_id,
        tokens_cached=i.tokens_cached,
        tool_calls={k: int(v) for k, v in dict(i.tool_calls).items()},
        ack_latency_ms=i.ack_latency_ms,
        input_verdict=_verdict(i.input_verdict),
        output_verdict=_verdict(i.output_verdict),
        reply=_reply(i),
        moderation=moderation,
    )


_STAGES = {"input": "输入", "output": "输出", "ack": "快速响应"}
_OUTCOMES: dict[str, tuple[Literal["info", "warn", "error"], str]] = {
    "ok": ("info", "已发出合并转发"),
    "silent": ("info", "选择不回复"),
    "done": ("info", "已完成操作，没有发消息"),
    "reply": ("info", "已回复"),
    "handoff": ("info", "不是管理操作，交给普通答疑"),
    "review": ("warn", "回复进入审核队列，等管理员处理"),
    "rejected": ("error", "回复没通过输出审核，没有发出"),
    "blocked_input": ("warn", "请求被输入审核拦下"),
    "ratelimited": ("warn", "被限流"),
    "cancelled": ("warn", "已取消"),
    "failed": ("error", "失败"),
}


def _events(i: Invocation, moderation: list[ModerationResult]) -> list[Entry]:
    """What the pipeline did around the agent run: moderation verdicts and how the invocation ended."""
    out = [
        Entry(
            kind="event",
            at=m.created_at,
            level="info" if m.action == "pass" else "warn",
            text=f"{_STAGES.get(m.stage, m.stage)}审核：{m.verdict}（{m.score:.2f}）"
            + (f" {'、'.join(m.categories)}" if m.categories else "")
            + f" → {m.action}",
        )
        for m in moderation
    ]
    if i.status in _OUTCOMES:
        level, text = _OUTCOMES[i.status]
        reply = _reply(i)
        if i.status == "silent" and isinstance(reply, BotReply) and reply.silent_reason:
            text += f"：{reply.silent_reason}"
        if i.error:
            text += f"：{i.error}"
        ended = i.created_at + timedelta(milliseconds=i.duration_ms) if i.duration_ms is not None else None
        out.append(Entry(kind="event", at=ended, level=level, text=text))
    return out


@router.get("/{invocation_id}/transcript")
async def get_invocation_transcript(invocation_id: UUID) -> transcripts.TranscriptOut:
    async with get_session() as s:
        i = await s.get(Invocation, invocation_id)
        if i is None:
            raise not_found("调用记录")
        moderation = list(
            (
                await s.scalars(
                    select(ModerationResult)
                    .where(ModerationResult.invocation_id == invocation_id)
                    .order_by(ModerationResult.id)
                )
            ).all()
        )
    fallback = [Entry(kind="user", at=i.created_at, parts=[TextPart(text=i.request_text, chars=len(i.request_text))])]
    return await transcripts.build(
        await store.for_invocation(invocation_id),
        events=_events(i, moderation),
        fallback=fallback,
        live=i.status == "running",
    )

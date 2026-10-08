"""Invocation log with Tempo trace links."""

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from yggdrasil.admin.common import Page, not_found
from yggdrasil.config import APP_CONFIG
from yggdrasil.db.models import Invocation, ModerationResult
from yggdrasil.db.session import get_session

router = APIRouter(prefix="/invocations", tags=["invocations"])


def trace_url(trace_id: str | None) -> str | None:
    """Grafana Explore link opening the trace in the Tempo datasource (uid ``tempo``)."""
    if not trace_id:
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
    handler: str
    status: str
    request_text: str
    models: list[Any]
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
    tool_calls: dict[str, Any]
    ack_latency_ms: int | None
    input_verdict: dict[str, Any] | None
    output_verdict: dict[str, Any] | None
    reply: dict[str, Any] | None
    moderation: list[ModerationOut]


def _summary_fields(i: Invocation) -> dict[str, Any]:
    return {
        "id": i.id,
        "created_at": i.created_at,
        "group_id": i.group_id,
        "user_id": i.user_id,
        "handler": i.handler,
        "status": i.status,
        "request_text": i.request_text,
        "models": list(i.models),
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
    group_id: int | None = None,
    handler: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[InvocationSummary]:
    cond = []
    if group_id is not None:
        cond.append(Invocation.group_id == group_id)
    if handler:
        cond.append(Invocation.handler == handler)
    if status:
        cond.append(Invocation.status == status)
    async with get_session() as s:
        total = await s.scalar(select(func.count()).select_from(Invocation).where(*cond)) or 0
        rows = await s.scalars(
            select(Invocation).where(*cond).order_by(Invocation.created_at.desc()).limit(limit).offset(offset)
        )
        items = [InvocationSummary(**_summary_fields(i)) for i in rows.all()]
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
        **_summary_fields(i),
        trigger_qq_message_id=i.trigger_qq_message_id,
        tokens_cached=i.tokens_cached,
        tool_calls=dict(i.tool_calls),
        ack_latency_ms=i.ack_latency_ms,
        input_verdict=i.input_verdict,
        output_verdict=i.output_verdict,
        reply=i.reply,
        moderation=moderation,
    )

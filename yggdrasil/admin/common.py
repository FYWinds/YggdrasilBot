"""Shared helpers for admin routers: audited mutations and paging."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil import runtime
from yggdrasil.db.models import ConfigAudit
from yggdrasil.db.session import get_session


class Page[T](BaseModel):
    items: list[T]
    total: int


class Ok(BaseModel):
    ok: bool = True


class VerdictOut(BaseModel):
    """A moderation verdict (``moderation.guard.Verdict``) as stored on invocations and review items."""

    stage: str
    verdict: str  # Safe | Unsafe | Controversial | Skipped | Error
    score: float
    categories: list[str] = []
    action: str = "pass"  # pass | review | drop
    model: str = ""
    latency_ms: int = 0


@dataclass
class Change:
    session: AsyncSession
    entity_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@asynccontextmanager
async def change(actor: str, entity: str, action: str, entity_id: object = "") -> AsyncIterator[Change]:
    """Session for one admin mutation: writes ``config_audit`` in the same transaction,
    then (after commit) notifies every process to reload its runtime snapshot.

    Set ``c.entity_id`` inside the block when the id is only known after insert.
    """
    async with get_session() as session:
        c = Change(session, str(entity_id))
        yield c
        session.add(ConfigAudit(actor=actor, entity=entity, entity_id=c.entity_id, action=action, detail=c.detail))
    await runtime.publish_change(f"{entity}.{action} {c.entity_id}".strip())


def not_found(what: str = "对象") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what}不存在")


def conflict(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, message)


def unavailable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, message)


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)

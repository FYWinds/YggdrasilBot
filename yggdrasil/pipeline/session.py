"""Continuing an answer's conversation when a group member replies to it.

Each delivered chat or tldr answer keeps its agent run (``InvocationSession``) for a while: the messages exactly as
they went to the model, the instructions, the route and the tool names. A chat request that replies to such an
answer runs again on that route with those instructions and tools and the messages as history, then adds only a user
turn with what changed: the time, what the group said since, and the request
(``agent.context.build_followup_context``). Everything before that new turn is byte for byte what the provider saw
last time, so its prompt cache hits; this is why instructions never depend on the request. The run's handles and
block nonce carry over, so ``fwd#1`` or ``<请求-ab12cd>`` in the old messages still mean what they meant.
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import delete, select

from yggdrasil.agent.deps import Handle, Handles
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, InvocationSession
from yggdrasil.db.session import get_session

# A bigger run (mostly inline images, kept as base64) is not kept: replies to it start over with the full context.
MAX_HISTORY_CHARS = 4_000_000
RETENTION = timedelta(days=1)  # rows older than this are deleted whenever a new one is saved


@dataclass(slots=True)
class Setup:
    """How the answering agent was built; a continuation rebuilds exactly this."""

    handler: str  # the route (model and its settings) and the transcript's handler
    instructions: str
    tools: list[str]  # ALL_TOOLS names, in order
    sandbox: bool  # had the workspace suite


@dataclass(slots=True)
class Session:
    invocation_id: UUID
    setup: Setup
    messages: list[ModelMessage]
    handles: Handles
    nonce: str
    seen_message_id: int | None  # the newest chat message the conversation had seen (messages.id)
    created_at: datetime


def dump_handles(handles: Handles) -> dict[str, Any]:
    return {
        "items": [{"key": h.key, "kind": h.kind, "data": h.data} for h in handles.items.values()],
        "counters": dict(handles._counters),  # pyright: ignore[reportPrivateUsage]
    }


def load_handles(raw: dict[str, Any]) -> Handles:
    items = {i["key"]: Handle(i["key"], i["kind"], i["data"]) for i in raw.get("items", [])}
    return Handles(items, Counter(raw.get("counters", {})))


async def save(
    invocation_id: UUID,
    setup: Setup,
    messages: Sequence[ModelMessage],
    handles: Handles,
    nonce: str,
    seen_message_id: int | None,
) -> None:
    """Keep the run of an answer about to be delivered. Never raises."""
    try:
        payload = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
        size = len(json.dumps(payload, ensure_ascii=False))
        if size > MAX_HISTORY_CHARS:
            LOGGER.info("session of {} not kept: {} chars", invocation_id, size)
            return
        async with get_session() as s:
            expired = InvocationSession.created_at < datetime.now(UTC) - RETENTION
            await s.execute(delete(InvocationSession).where(expired))
            s.add(
                InvocationSession(
                    invocation_id=invocation_id,
                    handler=setup.handler,
                    instructions=setup.instructions,
                    tools=setup.tools,
                    sandbox=setup.sandbox,
                    messages=payload,
                    handles=dump_handles(handles),
                    nonce=nonce,
                    seen_message_id=seen_message_id,
                )
            )
    except Exception:
        LOGGER.exception("saving the session of {} failed", invocation_id)


async def load(group_id: int, answer_qq_id: int, max_age: timedelta) -> Session | None:
    """The kept run of the delivered answer ``answer_qq_id`` from the last ``max_age``, if any."""
    async with get_session() as s:
        row = await s.scalar(
            select(InvocationSession)
            .join(Invocation, Invocation.id == InvocationSession.invocation_id)
            .where(
                Invocation.group_id == group_id,
                Invocation.reply_qq_message_id == answer_qq_id,
                Invocation.status == "ok",
                InvocationSession.created_at > datetime.now(UTC) - max_age,
            )
            .order_by(InvocationSession.created_at.desc())
            .limit(1)
        )
    if row is None:
        return None
    try:
        messages = ModelMessagesTypeAdapter.validate_python(row.messages)
    except ValidationError:
        LOGGER.warning("session of {} no longer loads; starting over", row.invocation_id)
        return None
    return Session(
        row.invocation_id,
        Setup(row.handler, row.instructions, list(row.tools), row.sandbox),
        messages,
        load_handles(row.handles),
        row.nonce,
        row.seen_message_id,
        row.created_at,
    )

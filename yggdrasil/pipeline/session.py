"""Continuing an answer's conversation when a group member replies to it.

Each delivered chat or tldr answer keeps its agent conversation (``InvocationSession``) for a while. A chat request
that replies to such an answer runs with that conversation as message history, so the model sees what it already
looked up and said; its prompt only adds what the group said since and the request
(``agent.context.build_followup_context``). The run's handles and block nonce carry over, so ``fwd#1`` or
``<请求-ab12cd>`` in the old messages still mean what they meant.

Stored conversations leave out what needn't or can't be sent again: thinking (its signature belongs to one provider,
and the route may have changed since), image and file bytes (their handles fetch them again), and the long tail of
tool results.
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from pydantic_ai.messages import (
    BinaryContent,
    FilePart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ThinkingPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import delete, select

from yggdrasil.agent.deps import Handle, Handles
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, InvocationSession
from yggdrasil.db.session import get_session

TOOL_RESULT_CHARS = 4000  # what a stored tool result keeps; the model can call the tool again for the rest
MAX_HISTORY_CHARS = 200_000  # a longer conversation is not kept: its replies start over with the full context
RETENTION = timedelta(days=1)  # rows older than this are deleted whenever a new one is saved

OMITTED_IMAGE = "[这里原来附着图片，已省略；需要再看就用前面写的句柄调用 view_image]"
OMITTED_FILE = "[这里原来附着文件内容，已省略]"


@dataclass(slots=True)
class Session:
    invocation_id: UUID
    messages: list[ModelMessage]
    handles: Handles
    nonce: str
    seen_message_id: int | None  # the newest chat message the conversation had seen (messages.id)
    created_at: datetime


def _clip(text: str) -> str:
    if len(text) <= TOOL_RESULT_CHARS:
        return text
    return f"{text[:TOOL_RESULT_CHARS]}\n…（后面还有 {len(text) - TOOL_RESULT_CHARS} 字已省略，需要时再调用一次工具）"


def _omit(item: Any) -> Any:
    if isinstance(item, BinaryContent):
        return OMITTED_IMAGE if item.is_image else OMITTED_FILE
    return item


def _request_part(part: ModelRequestPart) -> ModelRequestPart:
    if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
        return replace(part, content=[_omit(c) for c in part.content])
    if isinstance(part, ToolReturnPart):
        content = part.content
        if isinstance(content, list):
            content = [_omit(c) for c in content]  # pyright: ignore[reportUnknownVariableType]
        content = _omit(content)
        if isinstance(content, str):
            return replace(part, content=_clip(content))
        part = replace(part, content=content)
        text = part.model_response_str()
        return replace(part, content=_clip(text)) if len(text) > TOOL_RESULT_CHARS else part
    return part


def scrub(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """``messages`` as they are kept: no thinking, generated files or attached bytes, tool results cut short."""
    out: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            parts = [p for p in msg.parts if not isinstance(p, ThinkingPart | FilePart)]
            if parts:
                out.append(replace(msg, parts=parts))
        elif isinstance(msg, ModelRequest):
            out.append(replace(msg, parts=[_request_part(p) for p in msg.parts]))
    return out


def dump_handles(handles: Handles) -> dict[str, Any]:
    return {
        "items": [{"key": h.key, "kind": h.kind, "data": h.data} for h in handles.items.values()],
        "counters": dict(handles._counters),  # pyright: ignore[reportPrivateUsage]
    }


def load_handles(raw: dict[str, Any]) -> Handles:
    items = {i["key"]: Handle(i["key"], i["kind"], i["data"]) for i in raw.get("items", [])}
    return Handles(items, Counter(raw.get("counters", {})))


async def save(
    invocation_id: UUID, messages: Sequence[ModelMessage], handles: Handles, nonce: str, seen_message_id: int | None
) -> None:
    """Keep the conversation of an answer about to be delivered. Never raises."""
    try:
        payload = ModelMessagesTypeAdapter.dump_python(scrub(messages), mode="json")
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
                    messages=payload,
                    handles=dump_handles(handles),
                    nonce=nonce,
                    seen_message_id=seen_message_id,
                )
            )
    except Exception:
        LOGGER.exception("saving the session of {} failed", invocation_id)


async def load(group_id: int, answer_qq_id: int, max_age: timedelta) -> Session | None:
    """The kept conversation of the delivered answer ``answer_qq_id`` from the last ``max_age``, if any."""
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
        row.invocation_id, messages, load_handles(row.handles), row.nonce, row.seen_message_id, row.created_at
    )

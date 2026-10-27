"""Record a pydantic-ai run and turn it into transcript entries.

:class:`TranscriptRecorder` is a capability added to every agent whose run the console shows (chat, tldr, report,
ops). Each model request carries the whole history so far (``request_context.messages``, instructions included),
so keeping the latest one plus its response gives the complete conversation even when the run fails or is
cancelled later. Tool executions are timed per call id; that also covers tools that finished after the last model
request (their results never went back to the model).
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic_ai import ModelRetry, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    BinaryContent,
    FilePart,
    ImageUrl,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import TextPart as AiTextPart
from pydantic_ai.messages import ThinkingPart as AiThinkingPart
from pydantic_ai.messages import ToolCallPart as AiToolCallPart

from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import (
    Entry,
    ImagePart,
    Outcome,
    Part,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    Usage,
)

MAX_TEXT = 200_000  # characters kept per text part; tool results are capped far below this by the tools themselves


@dataclass(slots=True)
class ToolRecord:
    name: str
    started: float
    ended: float | None = None
    outcome: Outcome = "ok"
    result: Any = None


@dataclass
class TranscriptRecorder(AbstractCapability[Any]):
    history: list[ModelMessage] = field(default_factory=list)
    spans: list[tuple[float, float]] = field(default_factory=list)  # (start, end) of each answered model request
    tools: dict[str, ToolRecord] = field(default_factory=dict)
    failure: tuple[float, str] | None = None  # (when, error) of a model request that raised

    async def wrap_model_request(self, ctx: Any, *, request_context: Any, handler: Any) -> ModelResponse:
        self.history = list(request_context.messages)
        started = time.time()
        try:
            response = await handler(request_context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.failure = (time.time(), f"{type(e).__name__}: {e}"[:2000])
            raise
        self.spans.append((started, time.time()))
        self.history.append(response)
        return response

    async def wrap_tool_execute(self, ctx: Any, *, call: Any, tool_def: Any, args: Any, handler: Any) -> Any:
        record = self.tools[call.tool_call_id] = ToolRecord(call.tool_name, time.time())
        try:
            result = await handler(args)
        except BaseException as e:
            record.ended = time.time()
            if isinstance(e, asyncio.CancelledError):
                record.outcome = "cancelled"  # no result text: the error repr is just task internals
            elif isinstance(e, ModelRetry):
                record.outcome, record.result = "retry", str(e)
            else:
                record.outcome, record.result = "error", f"{type(e).__name__}: {e}"
            raise
        record.ended = time.time()
        record.result = result
        return result


def _at(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch else None


def _ms(start: float | None, end: float | None) -> int | None:
    return int((end - start) * 1000) if start and end else None


def text_part(text: str, full: bool) -> TextPart:
    return TextPart(text=text[:MAX_TEXT] if full else None, chars=len(text))


def _content(content: Any, notes: TraceNotes, full: bool) -> list[Part]:
    """User-side content: strings, images, other attachments."""
    items = content if isinstance(content, list) else [content]
    parts: list[Part] = []
    for item in items:
        if isinstance(item, str):
            if item:
                parts.append(text_part(item, full))
        elif isinstance(item, BinaryContent):
            if item.is_image:
                parts.append(
                    ImagePart(blob=notes.image(item.data, item.media_type), mime=item.media_type, bytes=len(item.data))
                )
            else:
                parts.append(text_part(f"[附件 {item.media_type}，{len(item.data)} 字节]", full))
        elif isinstance(item, ImageUrl):
            parts.append(ImagePart(url=item.url if full else None, mime=item.media_type or ""))
        else:
            parts.append(text_part(str(item), full))
    return parts


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return_value = getattr(result, "return_value", result)  # ToolReturn
    if isinstance(return_value, str):
        return return_value
    try:
        return json.dumps(return_value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(return_value)


def _response(msg: ModelResponse, span: tuple[float, float] | None, notes: TraceNotes, full: bool) -> Entry:
    parts: list[Part] = []
    for part in msg.parts:
        if isinstance(part, AiTextPart):
            if part.content:
                parts.append(text_part(part.content, full))
        elif isinstance(part, AiThinkingPart):
            redacted = not part.content and bool(part.signature)
            parts.append(
                ThinkingPart(text=part.content[:MAX_TEXT] if full else None, chars=len(part.content), redacted=redacted)
            )
        elif isinstance(part, AiToolCallPart):
            args = part.args_as_dict()
            parts.append(
                ToolCallPart(
                    id=part.tool_call_id,
                    name=part.tool_name,
                    args=args if full else None,
                    chars=len(part.args_as_json_str()),
                )
            )
        elif isinstance(part, FilePart) and isinstance(part.content, BinaryContent):
            data = part.content
            image = ImagePart(blob=notes.image(data.data, data.media_type), mime=data.media_type, bytes=len(data.data))
            parts.append(image)
        else:  # built-in (provider-side) tool parts and anything newer
            parts.append(text_part(f"[{part.part_kind}]", full))
    usage = msg.usage
    return Entry(
        kind="assistant",
        at=_at(span[1]) if span else msg.timestamp,
        parts=parts,
        model=msg.model_name,
        duration_ms=_ms(*span) if span else None,
        usage=Usage(input=usage.input_tokens, output=usage.output_tokens, cached=usage.cache_read_tokens),
        finish=msg.finish_reason,
    )


def _tool_entry(
    call_id: str, name: str, text: str | None, record: ToolRecord | None, notes: TraceNotes, at: datetime | None
) -> Entry:
    outcome: Outcome = record.outcome if record else "ok"
    if call_id in notes.errors and outcome == "ok":
        outcome = "error"
    parts: list[Part] = [text_part(text, notes.full)] if text is not None else []
    return Entry(
        kind="tool",
        at=_at(record.ended) if record and record.ended else at,
        call_id=call_id,
        name=name,
        parts=parts,
        outcome=outcome,
        duration_ms=_ms(record.started, record.ended) if record else None,
        display=notes.displays.get(call_id),
    )


def entries(recorder: TranscriptRecorder, notes: TraceNotes) -> list[Entry]:
    """The run's conversation; images referenced by the entries are added to ``notes.blobs``."""
    full = notes.full
    out: list[Entry] = []
    system: str | None = None
    responses = 0
    returned: set[str] = set()
    first_user = True
    for msg in recorder.history:
        if isinstance(msg, ModelResponse):
            span = recorder.spans[responses] if responses < len(recorder.spans) else None
            responses += 1
            out.append(_response(msg, span, notes, full))
            continue
        assert isinstance(msg, ModelRequest)
        if msg.instructions and msg.instructions != system:
            system = msg.instructions
            out.append(Entry(kind="system", at=msg.timestamp, parts=[text_part(system, full)]))
        # A ToolReturn's extra content (the image view_image hands over) comes back to the model as user content at
        # the end of the request; it is shown on the tool entry that produced it instead.
        extra_content = any(
            isinstance(part, ToolReturnPart)
            and isinstance(rec := recorder.tools.get(part.tool_call_id), ToolRecord)
            and isinstance(rec.result, ToolReturn)
            and rec.result.content
            for part in msg.parts
        )
        for part in msg.parts:
            if isinstance(part, SystemPromptPart):
                out.append(Entry(kind="system", at=part.timestamp, parts=[text_part(part.content, full)]))
            elif isinstance(part, UserPromptPart):
                if extra_content:
                    continue
                display = notes.context if first_user and full else None  # chat content: full mode only
                out.append(
                    Entry(kind="user", at=part.timestamp, parts=_content(part.content, notes, full), display=display)
                )
                first_user = False
            elif isinstance(part, ToolReturnPart):
                returned.add(part.tool_call_id)
                record = recorder.tools.get(part.tool_call_id)
                text = part.model_response_str()
                entry = _tool_entry(part.tool_call_id, part.tool_name, text, record, notes, part.timestamp)
                if record and isinstance(record.result, ToolReturn) and record.result.content:
                    entry.parts += _content(record.result.content, notes, full)
                out.append(entry)
            elif isinstance(part, RetryPromptPart):
                text = part.model_response()
                if part.tool_name:
                    returned.add(part.tool_call_id)
                    entry = _tool_entry(
                        part.tool_call_id,
                        part.tool_name,
                        text,
                        recorder.tools.get(part.tool_call_id),
                        notes,
                        part.timestamp,
                    )
                    entry.outcome = "retry"
                    out.append(entry)
                else:
                    out.append(
                        Entry(
                            kind="event",
                            at=part.timestamp,
                            level="warn",
                            text="输出不合格，要求模型重写",
                            parts=[text_part(text, full)],
                        )
                    )
    # Tools that ran after the last model request (the run failed or was cancelled before reporting back).
    for call_id, record in recorder.tools.items():
        if call_id in returned:
            continue
        text = _result_text(record.result) if record.result is not None else None
        entry = _tool_entry(call_id, record.name, text, record, notes, None)
        if record.ended is None:
            entry.outcome = "cancelled"
        out.append(entry)
    if recorder.failure:
        when, error = recorder.failure
        text = f"模型请求失败：{error}" if full else "模型请求失败"
        out.append(Entry(kind="event", at=_at(when), level="error", text=text))
    return out

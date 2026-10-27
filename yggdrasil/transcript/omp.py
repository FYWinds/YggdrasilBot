"""omp session messages (``message_end`` frames of a background agent job) as transcript entries.

Message shapes are omp's ``AgentMessage`` union (``rpc-wire.generated.ts``): ``user`` / ``developer`` with text or
content blocks, ``assistant`` with ``text`` / ``thinking`` / ``redactedThinking`` / ``toolCall`` / ``image`` blocks,
usage, ``duration`` and ``stopReason``, ``toolResult`` with text and base64 image blocks, plus summaries. Unknown
roles and blocks degrade to a short text, so a newer omp never breaks a transcript.
"""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

from yggdrasil.transcript.capture import text_part
from yggdrasil.transcript.notes import Blob, blob_id
from yggdrasil.transcript.schema import Entry, ImagePart, Part, ThinkingPart, ToolCallPart, Usage


def _at(ms: Any) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, UTC) if isinstance(ms, int | float) and ms > 0 else None


def _image(block: dict[str, Any], full: bool, blobs: dict[str, Blob]) -> ImagePart:
    mime = str(block.get("mimeType") or "image/png")
    try:
        data = base64.b64decode(str(block.get("data") or ""), validate=False)
    except (binascii.Error, ValueError):
        data = b""
    sha = None
    if full and data:
        sha = blob_id(data)
        blobs.setdefault(sha, Blob(data, mime))
    return ImagePart(blob=sha, mime=mime, bytes=len(data))


def _blocks(content: Any, full: bool, blobs: dict[str, Blob]) -> list[Part]:
    if isinstance(content, str):
        return [text_part(content, full)] if content else []
    parts: list[Part] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            if text := str(block.get("text") or ""):
                parts.append(text_part(text, full))
        elif kind == "image":
            parts.append(_image(block, full, blobs))
        elif kind == "thinking":
            text = str(block.get("thinking") or "")
            if block.get("narration"):  # prose meant for the user, sent as thinking
                parts.append(text_part(text, full))
            else:
                parts.append(ThinkingPart(text=text if full else None, chars=len(text)))
        elif kind == "redactedThinking":
            parts.append(ThinkingPart(redacted=True))
        elif kind == "toolCall":
            args = block.get("arguments")
            args = args if isinstance(args, dict) else {"value": args}
            parts.append(
                ToolCallPart(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or "?"),
                    args=args if full else None,
                    chars=len(json.dumps(args, ensure_ascii=False)),
                )
            )
        else:
            parts.append(text_part(f"[{kind}]", full))
    return parts


def message_entries(
    msg: dict[str, Any], *, full: bool, blobs: dict[str, Blob], tool_starts: dict[str, float]
) -> list[Entry]:
    """Entries for one finished message. ``tool_starts`` maps tool call ids to when their assistant message
    completed (epoch ms), filled here from assistant messages and used to time the matching results."""
    role = msg.get("role")
    at = _at(msg.get("timestamp"))
    if role == "user":
        return [Entry(kind="user", at=at, parts=_blocks(msg.get("content"), full, blobs))]
    if role == "developer":
        return [Entry(kind="system", at=at, parts=_blocks(msg.get("content"), full, blobs))]
    if role == "assistant":
        raw_usage = msg.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        duration = msg.get("duration")
        completed = msg.get("completedAt") or (
            msg.get("timestamp", 0) + duration if isinstance(duration, int | float) else msg.get("timestamp")
        )
        parts = _blocks(msg.get("content"), full, blobs)
        for part in parts:
            if isinstance(part, ToolCallPart) and isinstance(completed, int | float):
                tool_starts[part.id] = float(completed)
        error = msg.get("errorMessage")
        return [
            Entry(
                kind="assistant",
                at=_at(completed) or at,
                parts=parts,
                model=str(msg.get("model") or "") or None,
                duration_ms=int(duration) if isinstance(duration, int | float) else None,
                usage=Usage(
                    input=int(usage.get("input") or 0),
                    output=int(usage.get("output") or 0),
                    cached=int(usage.get("cacheRead") or 0),
                ),
                finish=str(msg.get("stopReason") or "") or None,
                error=(str(error) if full else "模型请求失败") if error else None,
            )
        ]
    if role == "toolResult":
        call_id = str(msg.get("toolCallId") or "")
        started = tool_starts.pop(call_id, None)
        ended = msg.get("timestamp")
        duration = int(ended - started) if started and isinstance(ended, int | float) else None
        return [
            Entry(
                kind="tool",
                at=at,
                call_id=call_id,
                name=str(msg.get("toolName") or "?"),
                outcome="error" if msg.get("isError") else "ok",
                duration_ms=duration if duration is None or duration >= 0 else None,
                parts=_blocks(msg.get("content"), full, blobs),
            )
        ]
    if role in ("bashExecution", "pythonExecution"):
        code = str(msg.get("command") or msg.get("code") or "")
        output = str(msg.get("output") or "")
        return [
            Entry(kind="user", at=at, parts=[text_part(code, full)]),
            Entry(
                kind="tool",
                at=at,
                name="bash" if role == "bashExecution" else "python",
                parts=[text_part(output, full)],
            ),
        ]
    if role in ("compactionSummary", "branchSummary"):
        summary = str(msg.get("summary") or "")
        text = "上下文已压缩" if role == "compactionSummary" else "会话分支摘要"
        return [
            Entry(kind="event", at=at, level="info", text=text, parts=[text_part(summary, full)] if summary else [])
        ]
    if role in ("custom", "hookMessage") and msg.get("display"):
        return [
            Entry(
                kind="event",
                at=at,
                level="info",
                text=str(msg.get("customType") or role),
                parts=_blocks(msg.get("content"), full, blobs),
            )
        ]
    if role == "fileMention":
        raw_files = msg.get("files")
        files: list[Any] = raw_files if isinstance(raw_files, list) else []
        names = "、".join(str(f.get("path")) for f in files if isinstance(f, dict))
        return [Entry(kind="event", at=at, level="info", text=f"引用文件：{names}")]
    return []

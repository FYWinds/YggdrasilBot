"""Drive ``omp --mode rpc`` running inside a group workspace, one poll at a time.

omp runs under ``/opt/ygg/ygg rpc start``: its stdin is a FIFO the bot writes with ``ygg rpc send``, its stdout
lines land in ``<job dir>/events.jsonl``. The bot never holds a stream open: every poll reads the new bytes of
that file, handles complete frames (protocol v2 ``rpc_chunk`` sequences reassembled), answers host-tool calls
and advances a small state machine whose state (``DriverState``) is stored with the job, so a restarted bot
picks up where the last poll stopped.

Session flow: ``ready`` -> setup (``negotiate_protocol`` v2, cache warming off, event filter, host tools,
``open_session``) -> ``prompt`` -> ``prompt_result`` (+ ``session_settled``) -> ``get_last_assistant_text`` ->
close stdin -> exit. Frame shapes follow omp's ``docs/rpc.md``.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from typing import Any, Literal, NamedTuple, Protocol

from yggdrasil.core.log import LOGGER

OMP_BIN = "/usr/local/bin/omp"
OMP_HOME = "/workspace"
OMP_CONFIG_DIR = ".ygg/omp"  # relative to HOME (omp joins PI_CONFIG_DIR onto it)
OMP_OVERLAY = "/etc/ygg/omp.yml"
MODELS_PATH = f"{OMP_HOME}/{OMP_CONFIG_DIR}/agent/models.yml"
SESSIONS_ROOT = f"{OMP_HOME}/{OMP_CONFIG_DIR}/sessions"
PROVIDER = "ygg"
TOKEN_ENV = "YGG_RELAY_TOKEN"
TOOLS = "bash,read,edit,write,grep,glob,eval,todo"  # no task (sub-agents) and no web_search
EVENTS = (
    "agent_start",
    "tool_execution_start",
    "tool_execution_end",
    "auto_retry_start",
    "auto_compaction_start",
    "notice",
)
"""Session events written to events.jsonl: enough for progress lines in the console, no streaming deltas."""

MAX_FRAME_BYTES = 4 * 1024 * 1024
"""Reassembled ``rpc_chunk`` sequences larger than this (huge tool results we never look at) are skipped."""
READ_LIMIT = 8 * 1024 * 1024
"""Bytes of events.jsonl read per poll; larger than any sequence we reassemble, base64 included."""
CLOSE_GRACE_S = 10.0
"""After closing stdin, how long omp may take to drain and exit before it is killed."""
_HANDLED_KEEP = 64

_HOST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "show",
        "label": "Show image",
        "description": (
            "把工作区里的一张图片（PNG/JPEG/GIF/WebP，最大 10MB，例如浏览器截图或画好的图）记下来，"
            "任务结束后随结果一起发到群里。path 是 /workspace 下的路径，caption 是一句说明。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "caption": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "loadMode": "essential",
    },
    {
        "name": "publish",
        "label": "Publish port",
        "description": (
            "给工作区里正在监听 0.0.0.0:port 的 Web 服务开一个公开链接（https），返回链接。"
            "链接任何人都能打开，任务结束后服务和链接保留，直到工作区回收或 unpublish。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
            "required": ["port"],
            "additionalProperties": False,
        },
        "loadMode": "essential",
    },
    {
        "name": "unpublish",
        "label": "Unpublish port",
        "description": "撤销某个端口的公开链接。",
        "parameters": {
            "type": "object",
            "properties": {"port": {"type": "integer"}},
            "required": ["port"],
            "additionalProperties": False,
        },
        "loadMode": "essential",
    },
    {
        "name": "progress",
        "label": "Report progress",
        "description": "用一句话汇报当前进度（只显示在管理后台，不会发到群里）。阶段性进展时调用，不要频繁调用。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "loadMode": "essential",
    },
]
LINK_TOOLS = frozenset({"publish", "unpublish"})


def host_tools(*, links: bool) -> list[dict[str, Any]]:
    """Host tool definitions for ``set_host_tools``; publish/unpublish only when the deployment has a link
    domain."""
    return [t for t in _HOST_TOOLS if links or t["name"] not in LINK_TOOLS]


Phase = Literal["starting", "setup", "prompting", "settling", "collecting", "closing", "done"]


# --- launch -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """One relay model as omp's models.yml describes it."""

    id: str
    name: str
    context_window: int
    max_tokens: int
    reasoning: bool
    vision: bool


def models_yaml(base_url: str, models: list[ModelSpec]) -> str:
    """``models.yml`` for the relay provider (JSON is valid YAML). ``apiKey`` names the env var holding the
    job's token; ``auth: apiKey`` avoids Claude-Code OAuth request shaping."""
    provider = {
        "baseUrl": base_url,
        "apiKey": TOKEN_ENV,
        "api": "anthropic-messages",
        "auth": "apiKey",
        "authHeader": True,
        "disableStrictTools": True,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "reasoning": m.reasoning,
                "input": ["text", "image"] if m.vision else ["text"],
                "contextWindow": m.context_window,
                "maxTokens": m.max_tokens,
            }
            for m in models
        ],
    }
    return json.dumps({"providers": {PROVIDER: provider}}, ensure_ascii=False, indent=2) + "\n"


def session_dir(group_id: int) -> str:
    return f"{SESSIONS_ROOT}/{group_id}"


def launch(
    *, group_id: int, model: str, rules_path: str, max_time_s: int, token: str
) -> tuple[list[str], dict[str, str]]:
    """argv and environment of the omp process (docs/SANDBOX.md §0.7)."""
    argv = [
        OMP_BIN,
        "--mode",
        "rpc",
        "--no-ui",
        "--cwd",
        OMP_HOME,
        "--allow-home",
        "--config",
        OMP_OVERLAY,
        "--session-dir",
        session_dir(group_id),
        "--model",
        f"{PROVIDER}/{model}",
        "--tools",
        TOOLS,
        "--no-lsp",
        "--no-pty",
        "--no-extensions",
        "--max-time",
        str(max_time_s),
        "--append-system-prompt",
        rules_path,
    ]
    env = {
        "HOME": OMP_HOME,
        "PI_CONFIG_DIR": OMP_CONFIG_DIR,
        "PI_AUTO_QA": "0",
        "PI_NO_PTY": "1",
        "PI_BROWSER_RELAY": "0",
        "OTEL_SDK_DISABLED": "true",
        TOKEN_ENV: token,
    }
    return argv, env


# --- frames -------------------------------------------------------------------------------------------------


@dataclass
class _Sequence:
    chunk_id: str
    count: int
    byte_length: int
    start: int  # offset of the sequence's first line
    parts: list[bytes] = field(default_factory=list)


class Decoded(NamedTuple):
    """One frame and where reading resumes once it has been handled (offset, skip state)."""

    frame: dict[str, Any]
    end: int
    skip: str


def decode_frames(data: bytes, offset: int, skip: str) -> tuple[list[Decoded], int, str]:
    """Split ``data`` (events.jsonl from ``offset``) into frames.

    Returns (frames, next offset, chunk id being skipped). Only complete lines are consumed; an ``rpc_chunk``
    sequence that is still incomplete at the end is left unconsumed (the next poll re-reads it from its first
    line), so no partial state has to survive between polls except ``skip``: the id of an oversized sequence
    whose remaining chunks are dropped as they arrive. Each frame carries the position after it, so a caller
    that fails halfway through the batch can resume at the first frame it did not handle."""
    frames: list[Decoded] = []
    committed = offset
    seq: _Sequence | None = None
    pos = 0
    while (nl := data.find(b"\n", pos)) >= 0:
        line, line_start, pos = data[pos:nl], offset + pos, nl + 1
        end = offset + pos
        try:
            frame = json.loads(line)
        except ValueError:
            frame = None
        if not isinstance(frame, dict):
            if line.strip():
                LOGGER.warning("omp events: dropping a non-JSON line at {}", line_start)
            if seq is None:
                committed = end
            continue
        if frame.get("type") != "rpc_chunk":
            if seq is not None:
                LOGGER.warning("omp events: chunk sequence {} interrupted", seq.chunk_id)
                seq = None
            frames.append(Decoded(frame, end, skip))
            committed = end
            continue
        chunk_id, index, count = str(frame.get("chunkId", "")), frame.get("index"), frame.get("count")
        if not isinstance(index, int) or not isinstance(count, int) or count < 1:
            seq, committed = None, end
            continue
        if skip and chunk_id == skip:
            if index >= count - 1:
                skip = ""
            committed = end
            continue
        if seq is None:
            byte_length = int(frame.get("byteLength") or 0)
            if index != 0:
                LOGGER.warning("omp events: chunk {} of sequence {} without its start", index, chunk_id)
                committed = end
                continue
            if byte_length > MAX_FRAME_BYTES:
                LOGGER.info("omp events: skipping a {} byte frame", byte_length)
                skip = chunk_id if count > 1 else ""
                committed = end
                continue
            seq = _Sequence(chunk_id, count, byte_length, line_start)
        elif chunk_id != seq.chunk_id or index != len(seq.parts):
            LOGGER.warning("omp events: chunk sequence {} broken by {}#{}", seq.chunk_id, chunk_id, index)
            seq, committed = None, end
            continue
        try:
            seq.parts.append(base64.b64decode(str(frame.get("data", "")), validate=True))
        except (binascii.Error, ValueError):
            seq, committed = None, end
            continue
        if len(seq.parts) == seq.count:
            blob = b"".join(seq.parts)
            whole: Any = None
            if len(blob) == seq.byte_length:
                try:
                    whole = json.loads(blob.decode("utf-8"))
                except ValueError:
                    whole = None
            if isinstance(whole, dict):
                frames.append(Decoded(whole, end, skip))
            else:
                LOGGER.warning("omp events: chunk sequence {} did not reassemble", seq.chunk_id)
            seq, committed = None, end
    if seq is None and pos < len(data) and not data[pos:].strip():
        committed = offset + len(data)
    return frames, committed, skip


# --- driver -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitInfo:
    exit: int | None
    signal: int | None


@dataclass(frozen=True)
class Poll:
    """One read of a job directory: ``gone`` when the directory itself is missing (workspace replaced)."""

    gone: bool
    exit: ExitInfo | None
    data: bytes


class RpcChannel(Protocol):
    async def send(self, frame: dict[str, Any]) -> None: ...
    async def close_stdin(self) -> None: ...
    async def kill(self) -> None: ...
    async def poll(self, offset: int, limit: int) -> Poll: ...
    async def stderr_tail(self, limit: int) -> str: ...


HostCall = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]
"""Runs one host tool: (tool name, arguments) -> (result text, is_error)."""


@dataclass
class DriverState:
    phase: Phase = "starting"
    offset: int = 0
    skip: str = ""
    pending: list[str] = field(default_factory=list)  # setup command ids still waiting for a response
    since: float = 0.0  # when the current phase started
    error: str = ""
    text: str | None = None  # the final assistant text
    status: str = ""  # prompt_result status
    handled: list[str] = field(default_factory=list)  # host_tool_call ids already answered
    accepted: bool = False  # omp acknowledged the prompt (a re-sent duplicate's rejection is then ignored)

    def to_json(self) -> str:
        return json.dumps(
            {
                "phase": self.phase,
                "offset": self.offset,
                "skip": self.skip,
                "pending": self.pending,
                "since": self.since,
                "error": self.error,
                "text": self.text,
                "status": self.status,
                "handled": self.handled,
                "accepted": self.accepted,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> DriverState:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(
            phase=data.get("phase", "starting"),
            offset=int(data.get("offset", 0)),
            skip=str(data.get("skip", "")),
            pending=list(data.get("pending", [])),
            since=float(data.get("since", 0.0)),
            error=str(data.get("error", "")),
            text=data.get("text"),
            status=str(data.get("status", "")),
            handled=list(data.get("handled", [])),
            accepted=bool(data.get("accepted", False)),
        )


@dataclass(frozen=True)
class Finished:
    """The omp process is done: ``ok`` when the prompt completed; ``text`` is its last answer (if any)."""

    ok: bool
    text: str | None
    error: str


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_event(frame: dict[str, Any]) -> str | None:
    """A one-line console entry for interesting session events."""
    kind = frame.get("type")
    if kind == "tool_execution_start":
        args = frame.get("args")
        detail = ""
        if isinstance(args, dict):
            for key in ("command", "path", "pattern", "code", "url"):
                if key in args:
                    detail = _short(args[key])
                    break
        return f"{frame.get('toolName', '?')}: {detail}".rstrip(": ")
    if kind == "tool_execution_end" and frame.get("isError"):
        return f"{frame.get('toolName', '?')} 出错"
    if kind == "auto_retry_start":
        return f"模型请求重试：{_short(frame.get('errorMessage') or frame.get('error') or '')}"
    if kind == "auto_compaction_start":
        return "压缩上下文"
    if kind == "notice":
        return f"提示：{_short(frame.get('message') or frame.get('text') or '')}"
    if kind == "agent_start":
        return "开始工作"
    return None


class OmpDriver:
    """One poll of an omp job. Mutates ``state``; the caller persists it after every ``step``."""

    def __init__(
        self,
        state: DriverState,
        channel: RpcChannel,
        host: HostCall,
        *,
        session_dir: str,
        model: str,
        task: str,
        tools: list[dict[str, Any]],
        on_event: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state = state
        self._ch = channel
        self._host = host
        self._session_dir = session_dir
        self._model = model
        self._task = task
        self._tools = tools
        self._on_event = on_event
        self._clock = clock
        self._exited_already = False  # omp is gone: frames still read, nothing written back

    async def step(self) -> Finished | None:
        """Read new frames and react. Returns ``Finished`` once omp has exited (or the job directory is gone).

        The read position advances frame by frame, only after a frame has been handled: when handling one fails
        (a ``ygg rpc send`` that failed or whose outcome is unknown, a cancelled poll), the state is rolled back
        to just before that frame and the exception propagates, so the next poll handles it again and re-sends
        whatever it had to send. omp tolerates the duplicates this can cause (see ``_response``)."""
        s = self.state
        poll = await self._ch.poll(s.offset, READ_LIMIT)
        if poll.gone:
            return Finished(False, s.text, "任务目录不见了（工作区可能被重置）")
        frames, end, skip = decode_frames(poll.data, s.offset, s.skip)
        self._exited_already = poll.exit is not None
        for item in frames:
            before = s.to_json()
            try:
                await self._handle(item.frame)
            except BaseException:
                self._restore(before)
                raise
            s.offset, s.skip = item.end, item.skip
        s.offset, s.skip = end, skip
        if poll.exit is not None:
            return await self._exited(poll.exit)
        if s.phase == "closing" and self._clock() - s.since > CLOSE_GRACE_S:
            await self._ch.kill()
        return None

    def _restore(self, saved: str) -> None:
        restored = DriverState.from_json(saved)
        for f in fields(DriverState):
            setattr(self.state, f.name, getattr(restored, f.name))

    async def _exited(self, info: ExitInfo) -> Finished:
        s = self.state
        if s.phase in ("closing", "done") and s.status in ("completed", ""):
            s.phase = "done"
            return Finished(not s.error, s.text, s.error)
        if not s.error:
            how = f"信号 {info.signal}" if info.signal else f"退出码 {info.exit}"
            tail = (await self._ch.stderr_tail(1500)).strip()
            s.error = f"omp 意外退出（{how}）" + (f"：\n{tail}" if tail else "")
        s.phase = "done"
        return Finished(False, s.text, s.error)

    def _enter(self, phase: Phase) -> None:
        self.state.phase = phase
        self.state.since = self._clock()

    async def _send(self, frame: dict[str, Any]) -> None:
        if not self._exited_already:
            await self._ch.send(frame)

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        s = self.state
        if kind == "ready":
            if s.phase == "starting":
                await self._setup()
            return
        if kind == "response":
            await self._response(frame)
            return
        if kind == "prompt_result" and frame.get("id") == "prompt":
            if s.status:
                return  # the first result counts (a re-sent prompt may produce a second one)
            s.status = str(frame.get("status", ""))
            if s.status == "error":
                err = frame.get("error") or {}
                s.error = f"模型请求失败：{_short(err.get('message', '') if isinstance(err, dict) else err, 500)}"
            elif s.status == "aborted" and not s.error:
                s.error = "任务被中止"
            if s.phase == "prompting":
                if frame.get("sessionSettled") or s.status != "completed":
                    await self._collect()
                else:
                    self._enter("settling")
            return
        if kind == "session_settled":
            if s.phase == "settling":
                await self._collect()
            return
        if kind == "host_tool_call":
            await self._host_call(frame)
            return
        if kind == "host_tool_cancel":
            return  # host tools are short; the result (if any) is simply ignored by omp
        if self._on_event is not None and (line := describe_event(frame)):
            await self._on_event(line)

    async def _setup(self) -> None:
        commands: list[dict[str, Any]] = [
            {"id": "setup-protocol", "type": "negotiate_protocol", "protocolVersion": 2},
            {"id": "setup-warming", "type": "set_cache_warming", "mode": "off"},
            {"id": "setup-events", "type": "set_event_filter", "events": list(EVENTS)},
            {"id": "setup-tools", "type": "set_host_tools", "tools": self._tools},
            {
                "id": "setup-session",
                "type": "open_session",
                "sessionDir": self._session_dir,
                "provider": PROVIDER,
                "modelId": self._model,
            },
        ]
        self.state.pending = [c["id"] for c in commands]
        self._enter("setup")
        for command in commands:
            await self._send(command)

    async def _response(self, frame: dict[str, Any]) -> None:
        s = self.state
        rid, ok = frame.get("id"), bool(frame.get("success"))
        if s.phase == "setup" and rid in s.pending:
            if not ok:
                s.error = f"omp 初始化失败（{frame.get('command')}）：{_short(frame.get('error', ''), 500)}"
                await self._close()
                return
            s.pending.remove(rid)
            if not s.pending:
                self._enter("prompting")
                await self._send({"id": "prompt", "type": "prompt", "message": self._task})
            return
        if rid == "prompt" and s.phase == "prompting":
            if not ok:
                if s.accepted:
                    return  # a prompt re-sent after an unknown send outcome: the first one is running
                s.error = f"omp 拒绝了任务：{_short(frame.get('error', ''), 500)}"
                await self._close()
                return
            s.accepted = True
            if isinstance(frame.get("data"), dict) and frame["data"].get("agentInvoked") is False:
                s.status = "completed"
                await self._collect()
            return
        if rid == "final" and s.phase == "collecting":
            data = frame.get("data")
            if ok and isinstance(data, dict) and isinstance(data.get("text"), str):
                s.text = data["text"]
            await self._close()

    async def _collect(self) -> None:
        self._enter("collecting")
        await self._send({"id": "final", "type": "get_last_assistant_text"})

    async def _close(self) -> None:
        self._enter("closing")
        if not self._exited_already:
            await self._ch.close_stdin()

    async def _host_call(self, frame: dict[str, Any]) -> None:
        s = self.state
        call_id = str(frame.get("id", ""))
        if not call_id or call_id in s.handled or self._exited_already:
            return
        name = str(frame.get("toolName", ""))
        args = frame.get("arguments")
        try:
            text, is_error = await self._host(name, args if isinstance(args, dict) else {})
        except Exception as e:
            LOGGER.exception("omp host tool {} failed", name)
            text, is_error = f"内部错误：{type(e).__name__}", True
        result: dict[str, Any] = {
            "type": "host_tool_result",
            "id": call_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
        if is_error:
            result["isError"] = True
        await self._send(result)
        s.handled = [*s.handled, call_id][-_HANDLED_KEEP:]

    async def stop(self, *, wait: Callable[[float], Awaitable[None]], budget_s: float = 8.0) -> Finished:
        """Cancel or time out: abort the run, ask for the last answer (partial work is still worth reporting),
        close stdin, then kill whatever is left. Never raises for a dead channel."""
        s = self.state
        deadline = self._clock() + budget_s
        try:
            if s.phase not in ("closing", "done", "starting"):
                await self._send({"id": "abort", "type": "abort"})
                if s.phase != "collecting":
                    await self._collect()
            while s.phase == "collecting" and self._clock() < deadline:
                await wait(0.5)
                if (finished := await self.step()) is not None:
                    return finished
            if s.phase != "closing":
                await self._close()
            while self._clock() < deadline + 3:
                await wait(0.5)
                if (finished := await self.step()) is not None:
                    return finished
        except Exception:
            LOGGER.opt(exception=True).warning("stopping omp cleanly failed; killing it")
        try:
            await self._ch.kill()
        except Exception:
            LOGGER.opt(exception=True).warning("killing omp failed")
        s.phase = "done"
        return Finished(False, s.text, s.error)

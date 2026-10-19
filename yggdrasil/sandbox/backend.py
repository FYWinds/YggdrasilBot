"""``SandboxdWorkspace``: a pydantic-ai workspace backend over one sandboxd (gRPC exec + REST files).

Commands run through ``ProcessService.Start`` (streamed, so a timeout still returns the partial output),
wrapped as ``sh -c 'exec "$@" </dev/null'`` so stdin is at EOF and a missing / non-executable program
exits 127 / 126 like a shell instead of failing the RPC. File contents move over REST; path checks and
metadata (stat, listing, mkdir, remove, realpath) run as small POSIX shell scripts through ``Execute``,
so they follow the command user's permissions and POSIX symlink semantics exactly (the REST listing
drops broken and looping links, and its atomic-rename PUT ignores file permissions).

The backend never talks to Kubernetes: a ``WorkspaceResolver`` (the manager's session, or a fixed one in
tests) finds or creates the sandbox on first use and tells whether it is gone after a connection failure.
"""

from __future__ import annotations

import asyncio
import posixpath
import shlex
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import anyio
import grpc
import httpx
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    WorkspaceCommand,
    WorkspaceError,
    WorkspaceOutputLimitError,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)
from pydantic_ai.workspaces.protocol import validate_timeout

from yggdrasil.core.log import LOGGER
from yggdrasil.sandbox._proto import process_pb2, process_pb2_grpc
from yggdrasil.sandbox.errors import SandboxToolError

if TYPE_CHECKING:
    from yggdrasil.settings import ToolSettings

PROVIDER = "sandboxd"
DEFAULT_ROOT = "/workspace"
REST_PORT = 8080
GRPC_PORT = 9090

GRPC_MAX_MESSAGE = 64 * 1024 * 1024
"""Raised from gRPC's 4 MiB default: ``Execute`` returns a whole file read (``cat``) in one message."""
MAX_COMMAND_OUTPUT = 32 * 1024 * 1024
"""Combined stdout+stderr a command may produce before it is stopped with ``WorkspaceOutputLimitError``."""
_INLINE_READ_BYTES = 4 * 1024 * 1024
"""Files up to this size are read with ``cat`` in the probe command itself; larger ones stream over REST."""
_INTERNAL_TIMEOUT = 60.0
"""Deadline of the backend's own helper commands (path checks, listings)."""
_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
"""Back-off between attempts while sandboxd is unreachable (e.g. its container is restarting)."""

_STDIN_EOF_WRAPPER = ("/bin/sh", "-c", 'exec "$@" </dev/null', "sh")
_SHELL = ("/bin/bash", "--norc", "--noprofile", "-c")

# Exit codes of the helper scripts: 100 + the Linux errno, as pydantic-ai's shell filesystem uses, outside the
# small codes the utilities return themselves.
_EXIT_NOT_FOUND = 102
_EXIT_PERMISSION = 113
_EXIT_EXISTS = 117
_EXIT_NOT_DIRECTORY = 120
_EXIT_IS_DIRECTORY = 121
_EXIT_NOT_REGULAR = 122
_EXIT_LARGE = 99

_CHECK_PARENTS = (
    'while [ "$parent" != / ]; do '
    f'if [ -e "$parent" ] && [ ! -d "$parent" ]; then exit {_EXIT_NOT_DIRECTORY}; fi; '
    f'if [ -d "$parent" ]; then [ -w "$parent" ] || exit {_EXIT_PERMISSION}; break; fi; '
    'parent=${parent%/*}; [ -n "$parent" ] || parent=/; done; '
)
_SCRIPT_READ = (
    f'if [ -d "$1" ]; then exit {_EXIT_IS_DIRECTORY}; fi; [ -e "$1" ] || exit {_EXIT_NOT_FOUND}; '
    f'[ -f "$1" ] || exit {_EXIT_NOT_REGULAR}; [ -r "$1" ] || exit {_EXIT_PERMISSION}; '
    f'if [ "$2" = cat ] || [ "$(wc -c <"$1")" -le {_INLINE_READ_BYTES} ]; then exec cat -- "$1"; fi; '
    f"exit {_EXIT_LARGE}"
)
_SCRIPT_WRITE_CHECK = (
    f'if [ -d "$1" ]; then exit {_EXIT_IS_DIRECTORY}; fi; '
    f'parent="${{1%/*}}"; [ -n "$parent" ] || parent=/; {_CHECK_PARENTS}'
    f'if [ -e "$1" ]; then [ -f "$1" ] || exit {_EXIT_NOT_REGULAR}; [ -w "$1" ] || exit {_EXIT_PERMISSION}; '
    'stat -L -c %a -- "$1"; fi'
)
_SCRIPT_STAT = f'[ -e "$1" ] || exit {_EXIT_NOT_FOUND}; if [ -d "$1" ]; then printf d; else stat -L -c %s -- "$1"; fi'
_SCRIPT_LIST = (
    f'if [ ! -d "$1" ]; then [ -e "$1" ] && exit {_EXIT_NOT_DIRECTORY}; exit {_EXIT_NOT_FOUND}; fi; '
    f'cd -- "$1" 2>/dev/null && [ -r . ] || exit {_EXIT_PERMISSION}; '
    "exec find . -mindepth 1 -maxdepth 1 -printf '%Y\\t%y\\t%s\\t%f\\0'"
)
_SCRIPT_MKDIR = (
    f'if [ -e "$1" ] && [ ! -d "$1" ]; then exit {_EXIT_EXISTS}; fi; '
    f'parent="${{1%/*}}"; [ -n "$parent" ] || parent=/; {_CHECK_PARENTS}'
    'mkdir -p -- "$1"'
)
_SCRIPT_REMOVE = f'if [ -e "$1" ] || [ -L "$1" ]; then exec rm -rf -- "$1"; fi; exit {_EXIT_NOT_FOUND}'
_SCRIPT_EXISTS = '[ -e "$1" ]'
# Like os.path.realpath(strict=False), in one round trip (adapted from pydantic-ai's shell filesystem): follow
# each symlink one level with readlink and walk its target in place, let `..` climb what is resolved so far,
# keep missing components as written, and give up following links after 40 (a loop keeps the rest as written).
_SCRIPT_REALPATH = (
    'rest="${1#/}"; resolved=; links=0; '
    'while [ -n "$rest" ]; do '
    'case "$rest" in */*) part="${rest%%/*}"; rest="${rest#*/}";; *) part="$rest"; rest=;; esac; '
    'case "$part" in ""|.) ;; ..) resolved="${resolved%/*}";; *) '
    'if [ "$links" -lt 40 ] && [ -L "$resolved/$part" ]; then '
    "links=$((links + 1)); "
    'target=$(readlink -n -- "$resolved/$part" && printf x) || exit 1; target="${target%x}"; '
    'case "$target" in /*) resolved=;; esac; rest="$target/$rest"; '
    'else resolved="$resolved/$part"; fi;; esac; done; '
    'printf %s "${resolved:-/}"'
)


@dataclass(frozen=True)
class Endpoint:
    """Where one sandboxd listens."""

    rest_url: str
    grpc_target: str

    @classmethod
    def for_pod(cls, ip: str) -> Endpoint:
        host = f"[{ip}]" if ":" in ip else ip
        return cls(rest_url=f"http://{host}:{REST_PORT}", grpc_target=f"{host}:{GRPC_PORT}")


@dataclass(frozen=True)
class Attachment:
    """What a resolver hands back on first use: the endpoint, plus a one-off notice for the model
    (e.g. that the group's previous workspace expired and this one is new)."""

    endpoint: Endpoint
    notice: str | None = None


class WorkspaceResolver(Protocol):
    async def acquire(self, ref: WorkspaceRef | None, on_ref: Callable[[WorkspaceRef], None]) -> Attachment:
        """Attach to ``ref`` (raise ``WorkspaceUnavailableError`` when it is gone, never replace it), or with no
        ref provide a workspace (attach or create). Call ``on_ref`` as soon as the identity is known, before any
        setup that can fail."""
        ...

    async def refresh(self, ref: WorkspaceRef) -> Endpoint | None:
        """After a connection failure: the current endpoint, or ``None`` when the environment is really gone.
        Errors mean "unknown" and are treated as transient."""
        ...

    async def touch(self, ref: WorkspaceRef) -> None:
        """Called before every operation once attached; keeps the environment alive while a run uses it. Must be
        cheap when there is nothing to do (it is the resolver's job to throttle). Errors are logged and ignored."""
        ...


class StaticResolver:
    """A resolver for one known sandboxd (tests, and the manager's read-only views of a mapped workspace)."""

    def __init__(
        self,
        endpoint: Endpoint,
        workspace_id: str,
        *,
        alive: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._id = workspace_id
        self._alive = alive

    async def acquire(self, ref: WorkspaceRef | None, on_ref: Callable[[WorkspaceRef], None]) -> Attachment:
        if ref is not None and ref != WorkspaceRef(provider=PROVIDER, id=self._id):
            raise WorkspaceUnavailableError(f"unknown sandboxd workspace {ref.id!r}")
        if self._alive is not None and not await self._alive():
            raise WorkspaceUnavailableError(f"sandboxd workspace {self._id!r} is gone")
        on_ref(WorkspaceRef(provider=PROVIDER, id=self._id))
        return Attachment(self._endpoint)

    async def refresh(self, ref: WorkspaceRef) -> Endpoint | None:
        if self._alive is not None and not await self._alive():
            return None
        return self._endpoint

    async def touch(self, ref: WorkspaceRef) -> None:
        return None


@dataclass(frozen=True)
class CommandLimits:
    """Per-invocation command budget, enforced by the backend."""

    max_commands: int
    invocation_s: float
    command_max_s: float
    default_timeout_s: float

    @classmethod
    def from_settings(cls, cfg: ToolSettings) -> CommandLimits:
        return cls(
            max_commands=cfg.sandbox_max_commands,
            invocation_s=float(cfg.sandbox_invocation_s),
            command_max_s=float(cfg.sandbox_command_max_s),
            default_timeout_s=float(cfg.sandbox_command_timeout_s),
        )


@dataclass
class _Budget:
    limits: CommandLimits | None
    commands: int = 0
    seconds: float = 0.0

    def admit(self, timeout: float | None) -> tuple[float | None, str | None]:
        """Count one command and return its effective timeout plus the note to append if that timeout
        was cut short; raise ``SandboxToolError`` when the invocation's budget is spent."""
        lim = self.limits
        if lim is None:
            return timeout, None
        if self.commands >= lim.max_commands:
            raise SandboxToolError(
                f"本次请求的工作区命令次数已达上限（{lim.max_commands} 条），不能再执行命令；请根据已有结果直接回答。"
            )
        remaining = lim.invocation_s - self.seconds
        if remaining < 1:
            raise SandboxToolError(
                f"本次请求的工作区命令总时长已用完（{lim.invocation_s:g} 秒），不能再执行命令；请根据已有结果直接回答。"
            )
        self.commands += 1
        wanted = lim.default_timeout_s if timeout is None else timeout
        note = None
        if wanted > lim.command_max_s:
            wanted = lim.command_max_s
            note = f"[工作区] 单条命令最长只能运行 {lim.command_max_s:g} 秒，超时按这个算。"
        if wanted > remaining:
            wanted = remaining
            note = f"[工作区] 本次请求的命令总时长只剩 {remaining:.0f} 秒，超时按剩余时间算。"
        return wanted, note


class _Exec(NamedTuple):
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass
class _Conn:
    endpoint: Endpoint
    channel: grpc.aio.Channel
    stub: process_pb2_grpc.ProcessServiceStub
    http: httpx.AsyncClient
    closed: bool = field(default=False)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.http.aclose()
        await self.channel.close()


class _Transient(Exception):
    """sandboxd did not answer; the operation never reached it (safe to retry)."""


_READY_WAIT = 5.0
"""How long a run-once command waits for the gRPC connection to be ready before each (re)try."""
UNKNOWN_OUTCOME = "命令发出后沙箱服务断开了，结果未知（可能已经执行）；先检查一下再决定要不要重试。"


def shell_argv(command: WorkspaceCommand, shell: bool) -> list[str]:
    """The argv sandboxd runs: string commands under ``bash --norc --noprofile -c``, argv as given, both wrapped
    so stdin is ``/dev/null``. Applies the protocol's ``shell`` rules (mismatch is a ``TypeError``)."""
    if isinstance(command, str):
        if not shell:
            raise TypeError("a string command requires shell=True; pass an argv sequence otherwise")
        if "\x00" in command:
            raise ValueError("command must not contain NUL bytes")
        inner = [*_SHELL, command]
    else:
        if isinstance(command, bytes):
            raise TypeError("a bytes command is not supported; pass a string or argv sequence")
        if shell:
            raise TypeError("an argv sequence cannot be combined with shell=True; pass a single command string")
        if not command:
            raise TypeError("an argv sequence needs at least the program to run")
        for argument in command:
            if type(argument) is not str:
                raise TypeError("argv elements must be strings")
            if "\x00" in argument:
                raise ValueError("argv elements must not contain NUL bytes")
        inner = list(command)
    return [*_STDIN_EOF_WRAPPER, *inner]


def rest_path(root: str, path: str) -> str | None:
    """The sandboxd REST path (relative to its root, percent-encoded) for absolute ``path``, or ``None`` when
    ``path`` lies outside ``root`` (sandboxd would re-root it instead of reaching it)."""
    norm = posixpath.normpath(path)
    if norm == root:
        rel = ""
    elif norm.startswith(root.rstrip("/") + "/"):
        rel = norm[len(root.rstrip("/")) + 1 :]
    else:
        return None
    return urllib.parse.quote(rel.encode("utf-8", "surrogateescape"), safe="/")


def _absolute(path: str) -> str:
    if not isinstance(path, str) or not posixpath.isabs(path):
        raise ValueError(f"workspace paths must be absolute POSIX paths, got {path!r}")
    if "\x00" in path:
        raise ValueError("paths must not contain NUL bytes")
    return path


def _command_text(command: WorkspaceCommand) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def _validate_env(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("env keys and values must be strings")
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError(f"invalid environment variable {key!r}")


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace")


class SandboxdWorkspace:
    """pydantic-ai ``WorkspaceBackend`` + ``SupportsCommands`` + ``SupportsFilesystem`` + ``SupportsRealpath``.

    Lazy: the constructor does no I/O; the first operation asks the resolver for the sandbox. Commands are
    audit-logged and charged against ``limits``; file operations are not.
    """

    def __init__(
        self,
        *,
        resolver: WorkspaceResolver,
        token: str,
        ref: WorkspaceRef | None = None,
        root: str = DEFAULT_ROOT,
        env: Mapping[str, str] | None = None,
        limits: CommandLimits | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError(f"not a sandboxd workspace ref: {ref!r}")
        if not posixpath.isabs(root) or posixpath.normpath(root) != root:
            raise ValueError(f"root must be a normalized absolute path, got {root!r}")
        self._resolver = resolver
        self._metadata = (("authorization", f"Bearer {token}"),)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._ref = ref
        self._root = root
        self._env = dict(env or {})
        _validate_env(self._env)
        self._budget = _Budget(limits)
        self._audit = dict(audit or {})
        self._conn: _Conn | None = None
        self._acquiring: asyncio.Task[_Conn] | None = None
        self._notice: str | None = None
        self._gone = False

    # --- lifecycle -------------------------------------------------------------------------------------

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    @property
    def attached(self) -> bool:
        """Whether the first operation has reached a sandbox (no I/O)."""
        return self._conn is not None

    async def admit(self, timeout: float | None) -> tuple[float | None, str | None]:  # noqa: ASYNC109 - a budget
        """Attach, then count one model-issued command (``sh`` / ``py``) against the invocation budget: its
        effective timeout and the note to show when that was cut short. Run it with ``exec(..., audit=...)``."""
        await self._ensure()
        await self._touch()
        return self._budget.admit(timeout)

    def take_notice(self) -> str | None:
        """The one-off attach notice (e.g. the previous workspace expired), handed out once."""
        notice, self._notice = self._notice, None
        return notice

    async def working_dir(self) -> str:
        await self._ensure()
        return self._root

    def _record_ref(self, ref: WorkspaceRef) -> None:
        if self._ref is None:
            self._ref = ref

    async def _ensure(self) -> _Conn:
        if self._conn is not None:
            return self._conn
        if self._gone:
            raise WorkspaceUnavailableError("工作区已失效（沙箱已不存在），里面的文件都没了。")
        task = self._acquiring
        if task is None:
            # Detached, so a caller cancelled mid-create cannot abandon a half-made workspace: the ref is still
            # recorded and later callers share the same acquisition.
            task = asyncio.create_task(self._acquire(), name="sandboxd-acquire")
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._acquiring = task
        await asyncio.wait([task])
        if task.cancelled() or task.exception() is not None:
            if self._acquiring is task:
                self._acquiring = None
        return task.result()

    async def _acquire(self) -> _Conn:
        attachment = await self._resolver.acquire(self._ref, self._record_ref)
        if self._ref is None:
            raise WorkspaceError("sandbox resolver returned without a workspace ref")
        self._notice = attachment.notice
        self._conn = self._connect(attachment.endpoint)
        return self._conn

    def _connect(self, endpoint: Endpoint) -> _Conn:
        channel = grpc.aio.insecure_channel(
            endpoint.grpc_target,
            options=[
                ("grpc.max_receive_message_length", GRPC_MAX_MESSAGE),
                ("grpc.max_send_message_length", GRPC_MAX_MESSAGE),
            ],
        )
        http = httpx.AsyncClient(
            base_url=endpoint.rest_url, headers=self._headers, timeout=httpx.Timeout(120.0, connect=5.0)
        )
        return _Conn(endpoint, channel, process_pb2_grpc.ProcessServiceStub(channel), http)

    async def _recover(self, conn: _Conn, cause: BaseException) -> _Conn:
        """After a connection failure: raise ``WorkspaceUnavailableError`` if the sandbox is gone, else return
        a connection to its (possibly new) endpoint."""
        assert self._ref is not None
        try:
            endpoint = await self._resolver.refresh(self._ref)
        except Exception:
            LOGGER.opt(exception=True).warning("sandbox refresh failed for {}", self._ref.id)
            return conn
        if endpoint is None:
            self._gone = True
            await self._drop_conn()
            raise WorkspaceUnavailableError("工作区已失效（沙箱已不存在），里面的文件都没了。") from cause
        if endpoint == conn.endpoint and not conn.closed:
            return conn
        await self._drop_conn()
        self._conn = self._connect(endpoint)
        return self._conn

    async def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    async def detach(self) -> None:
        """Forget the sandbox (after the manager destroyed it): the next operation acquires a new one."""
        await self._drop_conn()
        self._acquiring = None
        self._ref = None
        self._gone = False
        self._notice = None

    async def settle(self) -> None:
        """Wait for an acquisition still in flight (it outlives cancelled callers), without raising its error."""
        task = self._acquiring
        if task is not None and not task.done():
            await asyncio.wait([task])

    async def aclose(self) -> None:
        """Close connections; the sandbox itself is never destroyed here."""
        await self.settle()
        await self._drop_conn()

    async def _touch(self) -> None:
        if self._ref is None:
            return
        try:
            await self._resolver.touch(self._ref)
        except Exception:
            LOGGER.opt(exception=True).warning("sandbox touch failed for {}", self._ref.id)

    async def _with_retry[T](self, op: Callable[[_Conn], Awaitable[T]], *, retry: bool = True) -> T:
        conn = await self._ensure()
        await self._touch()
        for delay in (*_RETRY_DELAYS, None) if retry else (None,):
            try:
                return await op(conn)
            except _Transient as e:
                conn = await self._recover(conn, e)
                if delay is None:
                    raise SandboxToolError("工作区暂时连不上（沙箱服务可能在重启），稍后再试。") from e
                LOGGER.warning("sandboxd unreachable ({}), retrying in {}s", e, delay)
                await anyio.sleep(delay)
        raise AssertionError("unreachable")

    # --- commands --------------------------------------------------------------------------------------

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - SupportsCommands protocol signature
    ) -> CommandResult:
        argv = shell_argv(command, shell)
        validate_timeout(timeout)
        merged = {**self._env, **(env or {})}
        _validate_env(merged)
        conn = await self._ensure()
        await self._touch()
        effective, note = self._budget.admit(timeout)
        started = time.monotonic()
        outcome = "error"
        try:
            result = await self._run_with_retry(conn, argv, merged, effective, note)
            outcome = str(result.exit_code)
            return result
        except WorkspaceTimeoutError:
            outcome = "timeout"
            raise
        except BaseException as e:
            outcome = type(e).__name__
            raise
        finally:
            elapsed = time.monotonic() - started
            self._budget.seconds += elapsed
            self._log_command(command, outcome, elapsed)

    async def _run_with_retry(
        self, conn: _Conn, argv: list[str], env: dict[str, str], seconds: float | None, note: str | None
    ) -> CommandResult:
        for delay in (*_RETRY_DELAYS, None):
            try:
                result = await self._start(conn, argv, env, seconds, note)
            except _Transient as e:
                conn = await self._recover(conn, e)
                if delay is None:
                    raise SandboxToolError("工作区暂时连不上（沙箱服务可能在重启），稍后再试。") from e
                await anyio.sleep(delay)
                continue
            if notice := self.take_notice():
                result = CommandResult(
                    exit_code=result.exit_code, stdout=result.stdout, stderr=f"{notice}\n{result.stderr}"
                )
            return result
        raise AssertionError("unreachable")

    async def _start(
        self, conn: _Conn, argv: list[str], env: dict[str, str], seconds: float | None, note: str | None
    ) -> CommandResult:
        request = process_pb2.StartRequest(config=process_pb2.ProcessConfig(command=argv, env_vars=env))
        stdout, stderr = bytearray(), bytearray()
        reached = False
        exit_code: int | None = None
        # The gRPC deadline travels to sandboxd, which kills the process group when it passes.
        started = time.monotonic()
        call = conn.stub.Start(request, metadata=self._metadata, timeout=seconds)
        try:
            async for event in call:
                reached = True
                kind = event.WhichOneof("event")
                if kind == "stdout":
                    stdout += event.stdout
                elif kind == "stderr":
                    stderr += event.stderr
                elif kind == "exit":
                    exit_code = event.exit.exit_code
                if len(stdout) + len(stderr) > MAX_COMMAND_OUTPUT:
                    raise WorkspaceOutputLimitError(
                        f"命令输出超过 {MAX_COMMAND_OUTPUT // (1024 * 1024)} MiB，已终止；"
                        "请把输出重定向到文件再分段查看。",
                        limit=MAX_COMMAND_OUTPUT,
                        stdout=_decode(stdout[: MAX_COMMAND_OUTPUT // 2]),
                        stderr=_decode(stderr[: MAX_COMMAND_OUTPUT // 2]),
                    )
        except grpc.aio.AioRpcError as e:
            code = e.code()
            # At the deadline the stream sometimes ends as CANCELLED (RST_STREAM from the server killing the
            # command) instead of DEADLINE_EXCEEDED; both mean the timeout fired.
            timed_out = code == grpc.StatusCode.DEADLINE_EXCEEDED or (
                code == grpc.StatusCode.CANCELLED
                and seconds is not None
                and time.monotonic() - started >= seconds - 0.25  # server-side deadline may fire marginally early
            )
            if timed_out:
                tail = f"\n{note}" if note else ""
                raise WorkspaceTimeoutError(
                    f"Command timed out after {seconds or 0:g} seconds",
                    stdout=_decode(stdout),
                    stderr=_decode(stderr) + tail,
                ) from e
            if code == grpc.StatusCode.UNAVAILABLE:
                if not reached:
                    raise _Transient(e.details() or "unavailable") from e
                # The command started and its daemon vanished: gone, or restarted with the result lost.
                await self._recover(conn, e)
                raise SandboxToolError("命令执行中沙箱服务断开了，结果未知；可以检查一下再重试。") from e
            raise self._rpc_error(e) from e
        finally:
            call.cancel()
        if exit_code is None:
            raise WorkspaceError("sandboxd ended the command stream without an exit status")
        if exit_code < 0:
            raise WorkspaceError("sandboxd could not wait for the command")
        return CommandResult(exit_code=exit_code, stdout=_decode(stdout), stderr=_decode(stderr))

    @staticmethod
    def _rpc_error(e: grpc.aio.AioRpcError) -> Exception:
        code = e.code()
        if code == grpc.StatusCode.UNAUTHENTICATED:
            return SandboxToolError("沙箱拒绝了 bot 的访问令牌（配置错误），工作区暂时不能用。")
        if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
            return WorkspaceOutputLimitError(f"sandboxd response too large: {e.details()}", limit=GRPC_MAX_MESSAGE)
        return WorkspaceError(f"sandboxd {code.name}: {e.details()}")

    def _log_command(self, command: WorkspaceCommand, outcome: str, elapsed: float) -> None:
        text = _command_text(command)
        LOGGER.bind(
            audit="sandbox",
            action="run",
            workspace=self._ref.id if self._ref else None,
            exit_code=outcome,
            duration_s=round(elapsed, 3),
            command=text[:500],
            **self._audit,
        ).info("sandbox run exit={} in {:.1f}s: {}", outcome, elapsed, text[:200])

    async def exec(
        self,
        argv: Sequence[str],
        *,
        seconds: float = _INTERNAL_TIMEOUT,
        retry: bool = True,
        once: bool = False,
        audit: str | None = None,
    ) -> _Exec:
        """Run a helper command and return raw bytes. ``retry=False`` fails fast while sandboxd is unreachable
        instead of backing off. ``once``: the command is not idempotent (starts a process, writes into a pipe), so
        it is only retried while the connection is not even established; once sent, a lost connection raises
        ``SandboxToolError`` (``UNKNOWN_OUTCOME``) instead of running it again. Not audited or charged to the
        budget unless ``audit`` (the command text to log) is given: model-issued commands admitted with
        ``admit`` pass it."""
        args = list(argv)

        async def op(conn: _Conn) -> _Exec:
            request = process_pb2.ExecuteRequest(config=process_pb2.ProcessConfig(command=args))
            if once:
                try:
                    await asyncio.wait_for(conn.channel.channel_ready(), _READY_WAIT)
                except TimeoutError as e:
                    raise _Transient("sandboxd not reachable") from e
            try:
                response = await conn.stub.Execute(request, metadata=self._metadata, timeout=seconds)
            except grpc.aio.AioRpcError as e:
                code = e.code()
                if code == grpc.StatusCode.UNAVAILABLE:
                    if once:
                        await self._recover(conn, e)  # WorkspaceUnavailableError when the sandbox is gone
                        raise SandboxToolError(UNKNOWN_OUTCOME) from e
                    raise _Transient(e.details() or "unavailable") from e
                if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise WorkspaceTimeoutError(f"workspace helper command timed out after {seconds:g}s") from e
                if code == grpc.StatusCode.NOT_FOUND:
                    return _Exec(127, b"", (e.details() or "not found").encode())
                if code == grpc.StatusCode.PERMISSION_DENIED:
                    return _Exec(126, b"", (e.details() or "permission denied").encode())
                raise self._rpc_error(e) from e
            return _Exec(response.exit_code, response.stdout, response.stderr)

        if audit is None:
            return await self._with_retry(op, retry=retry)
        started = time.monotonic()
        outcome = "error"
        try:
            result = await self._with_retry(op, retry=retry)
            outcome = str(result.exit_code)
            return result
        except WorkspaceTimeoutError:
            outcome = "timeout"
            raise
        except BaseException as e:
            outcome = type(e).__name__
            raise
        finally:
            elapsed = time.monotonic() - started
            self._budget.seconds += elapsed
            self._log_command(audit, outcome, elapsed)

    async def _script(self, script: str, *args: str) -> _Exec:
        return await self.exec(["/bin/sh", "-c", script, "sh", *args])

    @staticmethod
    def _raise_for(result: _Exec, path: str, what: str) -> None:
        code = result.exit_code
        if code == 0:
            return
        if code == _EXIT_NOT_FOUND:
            raise FileNotFoundError(2, "No such file or directory", path)
        if code == _EXIT_NOT_DIRECTORY:
            raise NotADirectoryError(20, "Not a directory", path)
        if code == _EXIT_IS_DIRECTORY:
            raise IsADirectoryError(21, "Is a directory", path)
        if code == _EXIT_EXISTS:
            raise FileExistsError(17, "File exists", path)
        if code == _EXIT_PERMISSION:
            raise PermissionError(13, "Permission denied", path)
        if code == _EXIT_NOT_REGULAR:
            raise OSError(f"not a regular file: {path!r}")
        detail = _decode(result.stderr).strip()[:300] or f"exit code {code}"
        raise WorkspaceError(f"could not {what} {path!r}: {detail}")

    # --- filesystem ------------------------------------------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        path = _absolute(path)
        rel = rest_path(self._root, path)
        result = await self._script(_SCRIPT_READ, path, "cat" if rel is None else "")
        if result.exit_code == 0:
            return result.stdout
        if result.exit_code != _EXIT_LARGE or rel is None:
            self._raise_for(result, path, "read")

        async def op(conn: _Conn) -> bytes:
            try:
                response = await conn.http.get(f"/v1/files/{rel}")
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise _Transient(str(e)) from e
            except httpx.TransportError as e:
                raise WorkspaceError(f"reading {path!r} from sandboxd failed: {e!r}") from e
            self._raise_http(response, path, "read")
            return response.content

        return await self._with_retry(op)

    async def write_bytes(self, path: str, data: bytes) -> None:
        path = _absolute(path)
        rel = rest_path(self._root, path)
        if rel is None:
            raise PermissionError(13, f"只能写 {self._root} 下的文件", path)
        check = await self._script(_SCRIPT_WRITE_CHECK, path)
        self._raise_for(check, path, "write")
        mode = _decode(check.stdout).strip()
        params = {"mode": f"0{mode.zfill(3)[-3:]}"} if mode.isdigit() else None
        payload = bytes(data)

        async def op(conn: _Conn) -> None:
            try:
                response = await conn.http.put(
                    f"/v1/files/{rel}",
                    params=params,
                    content=payload,
                    headers={"Content-Type": "application/octet-stream"},
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                raise _Transient(str(e)) from e
            except httpx.TransportError as e:
                raise WorkspaceError(f"writing {path!r} to sandboxd failed: {e!r}") from e
            self._raise_http(response, path, "write")

        await self._with_retry(op)

    @staticmethod
    def _raise_http(response: httpx.Response, path: str, what: str) -> None:
        status = response.status_code
        if status < 300:
            return
        if status == 404:
            raise FileNotFoundError(2, "No such file or directory", path)
        if status == 403:
            raise PermissionError(13, "Permission denied", path)
        if status == 401:
            raise SandboxToolError("沙箱拒绝了 bot 的访问令牌（配置错误），工作区暂时不能用。")
        if status == 400 and what == "write":
            raise IsADirectoryError(21, "Is a directory", path)
        raise WorkspaceError(f"could not {what} {path!r}: sandboxd HTTP {status} {response.text[:200]}")

    async def stat(self, path: str) -> FileEntry:
        path = _absolute(path)
        result = await self._script(_SCRIPT_STAT, path)
        self._raise_for(result, path, "stat")
        out = _decode(result.stdout).strip()
        name = posixpath.basename(posixpath.normpath(path))
        if out == "d":
            return FileEntry(name=name, path=path, is_dir=True, size=None)
        return FileEntry(name=name, path=path, is_dir=False, size=int(out) if out.isdigit() else None)

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        path = _absolute(path)
        result = await self._script(_SCRIPT_LIST, path)
        self._raise_for(result, path, "list")
        return parse_listing(path, result.stdout)

    async def make_dir(self, path: str) -> None:
        path = _absolute(path)
        self._raise_for(await self._script(_SCRIPT_MKDIR, path), path, "create directory")

    async def remove(self, path: str) -> None:
        path = _absolute(path)
        parent = await self.realpath(posixpath.dirname(path))
        target = posixpath.join(parent, posixpath.basename(path))
        if is_protected(self._root, target):
            raise ValueError("cannot remove the workspace root or its ancestor")
        self._raise_for(await self._script(_SCRIPT_REMOVE, path), path, "remove")

    async def exists(self, path: str) -> bool:
        path = _absolute(path)
        result = await self._script(_SCRIPT_EXISTS, path)
        if result.exit_code in (0, 1):
            return result.exit_code == 0
        raise WorkspaceError(f"could not check whether {path!r} exists (exit code {result.exit_code})")

    async def realpath(self, path: str) -> str:
        path = _absolute(path)
        result = await self._script(_SCRIPT_REALPATH, path)
        self._raise_for(result, path, "resolve")
        return result.stdout.decode("utf-8", errors="surrogateescape")


def is_protected(root: str, target: str) -> bool:
    """Whether removing ``target`` would remove the working directory ``root`` (itself or an ancestor)."""
    target = posixpath.normpath(target)
    return target == root or root.startswith(target.rstrip("/") + "/")


def parse_listing(path: str, raw: bytes) -> tuple[FileEntry, ...]:
    """Parse ``find -printf '%Y\\t%y\\t%s\\t%f\\0'`` output into entries sorted by name. ``%Y`` follows symlinks
    (``d`` = directory; ``L``/``N`` = loop/broken, reported as non-directories)."""
    entries: list[FileEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 3)
        if len(parts) != 4:
            raise WorkspaceError(f"could not parse the listing of {path!r}")
        followed, own, size, name_bytes = parts
        name = name_bytes.decode("utf-8", errors="surrogateescape")
        is_dir = followed == b"d"
        file_size = int(size) if followed == b"f" and own == b"f" and size.isdigit() else None
        entries.append(FileEntry(name=name, path=posixpath.join(path, name), is_dir=is_dir, size=file_size))
    entries.sort(key=lambda e: e.name)
    return tuple(entries)

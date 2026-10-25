"""Background jobs in group workspaces: long shell commands and omp agent tasks that outlive the invocation
that started them.

Everything lives in files and Redis, nothing in long-lived connections, so a restarted bot carries on:

- the job's process runs detached in the workspace under ``/opt/ygg/ygg sh start`` (commands) or
  ``ygg rpc start`` (omp), writing into ``/workspace/.ygg/jobs/<id>/`` and, when it ends, an ``exit`` file;
- ``ygg:sandbox:job:<id>`` (a hash) holds the record, ``ygg:sandbox:jobs:running:<gid>`` the group's running
  jobs (the concurrency limit), ``ygg:sandbox:jobs:group:<gid>`` the group's history and
  ``ygg:sandbox:jobs:active`` every job the poller still has to look at (running, or its report pending);
- the poller (``JobRunner``, in the bot process) visits active jobs every few seconds under a per-job Redis
  lock (safe with more than one bot replica, e.g. during a rolling update): it renews the workspace, reads the
  job's files incrementally over sandboxd, drives omp, enforces deadlines and cancellation, and when a job ends
  starts a synthetic chat invocation that reports the result back to the requester.

While a job runs its supervisor pid sits in its workspace's keep hash (``job:<id>``), so the end-of-invocation
reap spares it and the workspace is never evicted; starting a job pushes the workspace's hard cap out so the
job fits. A workspace that is destroyed or replaced meanwhile fails the job on its next poll.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic_ai.workspaces import WorkspaceError

from yggdrasil.core.log import LOGGER
from yggdrasil.kv import redis_client
from yggdrasil.sandbox import omp
from yggdrasil.sandbox.backend import DEFAULT_ROOT
from yggdrasil.sandbox.errors import SandboxError
from yggdrasil.sandbox.state import keep_key, random_id

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from yggdrasil.sandbox.state import Link
    from yggdrasil.settings import Settings, ToolSettings

YGG = "/opt/ygg/ygg"
JOBS_ROOT = f"{DEFAULT_ROOT}/.ygg/jobs"

JOB_KEY_PREFIX = "ygg:sandbox:job:"
ACTIVE_KEY = "ygg:sandbox:jobs:active"
RUNNING_PREFIX = "ygg:sandbox:jobs:running:"
GROUP_PREFIX = "ygg:sandbox:jobs:group:"
LOCK_PREFIX = "ygg:sandbox:joblock:"

RETENTION_S = 7 * 86400
"""How long a settled job's Redis record stays (the chat ``jobs`` tool reads it); the archive keeps it for good."""
POLL_S = 3.0
LOCK_TTL = 120
"""Seconds a poller holds a job; longer than any single visit (helper commands time out well before)."""
START_GRACE_S = 120.0
"""A job still ``starting`` after this long was abandoned mid-start (bot restarted): it is failed."""
START_MARGIN_S = 180.0
"""Extra room a new job needs before the workspace's max age, on top of its run time (stopping it, polling)."""
REPORT_RETRY_S = 30.0
REPORT_GIVE_UP_S = 3600.0
"""A report that could not even start (no bot connection, queue full) for this long is dropped."""
REPORT_STALE_S = 1200.0
"""A report marked running this long ago died with its bot process; it is retried."""
REPORT_ATTEMPTS = 3
EVENTS_KEEP = 1000  # the whole log of any realistic job; it is archived in full when the job settles
HEAD_CHARS = 2000
TAIL_CHARS = 6000
MAX_SHOW_BYTES = 10 * 1024 * 1024
MAX_SHOW = 4  # images a job can hand to its report (the reply's forward message carries 4)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_SEND_INLINE = 32 * 1024  # frames larger than this go through a file (``ygg rpc send --file``)

JobKind = Literal["command", "agent"]
JobStatus = Literal["starting", "running", "succeeded", "failed", "timeout", "cancelled"]
ReportState = Literal["", "pending", "running", "done", "skipped", "failed"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "timeout", "cancelled"})
STATUS_TEXT = {
    "starting": "启动中",
    "running": "运行中",
    "succeeded": "已完成",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已取消",
}
KIND_TEXT = {"command": "后台命令", "agent": "后台大任务"}
_GONE = "工作区已被回收或重置，任务中断，里面的文件都没了。"

# Reserve a running slot and create the record: 0 ok, 1 group at its limit, 2 an agent job already runs.
_RESERVE = """
for _, id in ipairs(redis.call('HKEYS', KEYS[1])) do
  if redis.call('EXISTS', ARGV[5] .. id) == 0 then redis.call('HDEL', KEYS[1], id) end
end
if redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[1]) then return 1 end
if ARGV[3] == 'agent' then
  for _, kind in ipairs(redis.call('HVALS', KEYS[1])) do if kind == 'agent' then return 2 end end
end
redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
redis.call('HSET', KEYS[2], unpack(ARGV, 6))
redis.call('ZADD', KEYS[3], ARGV[4], ARGV[2])
redis.call('SADD', KEYS[4], ARGV[2])
return 0
"""
_UNLOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
# Set fields of a job record only while its status is still ARGV[1]: 1 = done, 0 = the status moved on.
_CAS_STATUS = (
    "if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then return 0 end "
    "redis.call('HSET', KEYS[1], unpack(ARGV, 2)) return 1"
)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# $1 job dir, $2 events file name ("" for none), $3 byte offset, $4 max bytes. Exit 3: the dir is gone.
# The exit file is read first, so events read after it are complete when the job has ended. A supervisor that
# is gone without an exit file was SIGKILLed (no chance to write it): reported as signal 9.
_POLL_SCRIPT = (
    '[ -d "$1" ] || exit 3; '
    'if [ -f "$1/exit" ]; then cat -- "$1/exit"; '
    'else p=$(cat -- "$1/pid" 2>/dev/null); '
    'if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; then '
    'if [ -f "$1/exit" ]; then cat -- "$1/exit"; else printf \'{"exit":null,"signal":9}\'; fi; fi; fi; '
    'printf "\\0"; '
    '[ -n "$2" ] || exit 0; '
    'tail -c +"$(($3 + 1))" -- "$1/$2" 2>/dev/null | head -c "$4"; exit 0'
)
# $1 file, $2 head bytes, $3 tail bytes: "<size>\n" + head + tail (the tail never overlaps the head).
_OUTPUT_SCRIPT = (
    '[ -f "$1" ] || { printf "0\\n"; exit 0; }; s=$(stat -c %s -- "$1"); printf "%s\\n" "$s"; '
    'head -c "$2" -- "$1"; r=$((s - $2)); [ "$r" -gt "$3" ] && r=$3; '
    '[ "$r" -gt 0 ] && tail -c "$r" -- "$1"; exit 0'
)
_SIZE_SCRIPT = '[ -f "$1" ] || exit 3; stat -L -c %s -- "$1"'


def job_dir(job_id: str) -> str:
    return f"{JOBS_ROOT}/{job_id}"


def job_key(job_id: str) -> str:
    return f"{JOB_KEY_PREFIX}{job_id}"


def events_key(job_id: str) -> str:
    return f"{JOB_KEY_PREFIX}{job_id}:events"


def running_key(group_id: int) -> str:
    return f"{RUNNING_PREFIX}{group_id}"


def group_key(group_id: int) -> str:
    return f"{GROUP_PREFIX}{group_id}"


def keep_field(job_id: str) -> str:
    return f"job:{job_id}"


def clean_output(raw: bytes) -> str:
    """Terminal output as plain text: UTF-8 with replacement, ANSI escapes stripped, ``\\r`` progress bars
    collapsed to their last state."""
    text = _ANSI.sub("", raw.decode("utf-8", errors="replace"))
    return "\n".join(line.rsplit("\r", 1)[-1] if "\r" in line else line for line in text.split("\n"))


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


# --- records ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Requester:
    """Who started a job, from where: the report replies to ``trigger_message_id``."""

    group_id: int
    user_id: int
    trigger_message_id: int | None
    invocation_id: str | None
    request_text: str


@dataclass
class Job:
    id: str
    kind: JobKind
    group_id: int
    user_id: int
    trigger_message_id: int | None
    invocation_id: str | None
    request_text: str
    title: str
    spec: str  # the script (command) or the task (agent)
    workspace: str  # the workspace's name in its group
    claim: str
    created_at: float
    deadline: float
    status: JobStatus = "starting"
    pid: int | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    result: str = ""
    error: str = ""
    progress: str = ""
    model: str = ""
    driver: str = ""  # omp.DriverState as JSON (agent jobs)
    images: list[dict[str, str]] = field(default_factory=list)  # {"path", "caption"} from omp's show
    links: list[dict[str, Any]] = field(default_factory=list)  # {"port", "url"} from omp's publish
    usage: dict[str, int] = field(default_factory=dict)  # relay usage (agent jobs)
    renewed_at: float = 0.0
    cancel: str = ""  # who asked to cancel: "chat:<qq>" / "admin:<email>"; written only by ``cancel``
    report: ReportState = ""
    report_at: float = 0.0
    report_attempts: int = 0
    report_invocation: str = ""

    @property
    def dir(self) -> str:
        return job_dir(self.id)

    @property
    def running(self) -> bool:
        return self.status in ("starting", "running")

    def to_hash(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in asdict(self).items():
            if isinstance(value, (list, dict)):
                out[key] = json.dumps(value, ensure_ascii=False)
            elif value is None:
                out[key] = ""
            else:
                out[key] = str(value) if not isinstance(value, float) else repr(value)
        return out

    @classmethod
    def from_hash(cls, data: dict[str, str]) -> Job | None:
        if not data.get("id"):
            return None

        def opt_int(key: str) -> int | None:
            raw = data.get(key, "")
            return int(raw) if raw not in ("", "None") else None

        def opt_float(key: str) -> float | None:
            raw = data.get(key, "")
            return float(raw) if raw not in ("", "None") else None

        try:
            return cls(
                id=data["id"],
                kind=cast(JobKind, data.get("kind", "command")),
                group_id=int(data["group_id"]),
                user_id=int(data.get("user_id") or 0),
                trigger_message_id=opt_int("trigger_message_id"),
                invocation_id=data.get("invocation_id") or None,
                request_text=data.get("request_text", ""),
                title=data.get("title", ""),
                spec=data.get("spec", ""),
                workspace=data.get("workspace", ""),
                claim=data.get("claim", ""),
                created_at=float(data.get("created_at") or 0),
                deadline=float(data.get("deadline") or 0),
                status=cast(JobStatus, data.get("status", "starting")),
                pid=opt_int("pid"),
                ended_at=opt_float("ended_at"),
                exit_code=opt_int("exit_code"),
                result=data.get("result", ""),
                error=data.get("error", ""),
                progress=data.get("progress", ""),
                model=data.get("model", ""),
                driver=data.get("driver", ""),
                images=json.loads(data.get("images") or "[]"),
                links=json.loads(data.get("links") or "[]"),
                usage=json.loads(data.get("usage") or "{}"),
                renewed_at=float(data.get("renewed_at") or 0),
                cancel=data.get("cancel", ""),
                report=cast(ReportState, data.get("report", "")),
                report_at=float(data.get("report_at") or 0),
                report_attempts=int(data.get("report_attempts") or 0),
                report_invocation=data.get("report_invocation", ""),
            )
        except (ValueError, KeyError, TypeError):
            LOGGER.warning("unreadable job record {}", data.get("id"))
            return None


@dataclass(frozen=True)
class JobEvent:
    at: float
    text: str


# --- in-workspace operations --------------------------------------------------------------------------------


class Exec(Protocol):
    exit_code: int
    stdout: bytes
    stderr: bytes


class JobBackend(Protocol):
    """The part of ``SandboxdWorkspace`` jobs use."""

    async def exec(self, argv: Sequence[str], *, seconds: float = ..., retry: bool = ..., once: bool = ...) -> Exec: ...
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Output:
    text: str
    size: int
    truncated: bool


class JobOps:
    """Job files and processes in one workspace, through ``/opt/ygg/ygg`` and small shell helpers."""

    def __init__(self, backend: JobBackend) -> None:
        self._b = backend

    async def _ygg(self, *args: str, seconds: float = 30, once: bool = False) -> dict[str, Any]:
        """Run ``/opt/ygg/ygg``; ``once`` for commands that must not run twice (they start a process or write
        into omp's stdin): a connection lost after sending raises instead of re-running them."""
        result = await self._b.exec([YGG, *args], seconds=seconds, once=once)
        out = result.stdout.decode("utf-8", errors="replace").strip()
        if result.exit_code != 0 or not out:
            err = result.stderr.decode("utf-8", errors="replace").strip() or out or f"exit {result.exit_code}"
            raise SandboxError(f"ygg {' '.join(args[:2])} 失败：{err[:500]}")
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError as e:
            raise SandboxError(f"ygg {' '.join(args[:2])} 的输出看不懂：{out[:200]}") from e

    async def write(self, path: str, data: bytes) -> None:
        await self._b.write_bytes(path, data)

    async def start_sh(self, directory: str) -> int:
        return int((await self._ygg("sh", "start", directory, once=True))["pid"])

    async def start_rpc(self, directory: str, argv: list[str], env: dict[str, str], *, cwd: str) -> int:
        args = ["rpc", "start", directory, "--cwd", cwd]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        return int((await self._ygg(*args, "--", *argv, once=True))["pid"])

    async def kill(self, directory: str) -> None:
        await self._ygg("proc", "kill", directory, seconds=20)

    async def send(self, directory: str, frame: dict[str, Any]) -> None:
        text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) <= _SEND_INLINE:
            await self._ygg("rpc", "send", directory, "--json", text, once=True)
            return
        path = f"{directory}/send-{random_id(6)}.json"
        await self.write(path, text.encode())
        await self._ygg("rpc", "send", directory, "--file", path, once=True)

    async def close_stdin(self, directory: str) -> None:
        await self._ygg("rpc", "close", directory)

    async def poll(self, directory: str, events: str = "", offset: int = 0, limit: int = 0) -> omp.Poll:
        result = await self._b.exec(
            ["/bin/sh", "-c", _POLL_SCRIPT, "sh", directory, events, str(offset), str(limit)], seconds=30
        )
        if result.exit_code == 3:
            return omp.Poll(gone=True, exit=None, data=b"")
        if result.exit_code != 0:
            raise SandboxError(f"读任务状态失败：{result.stderr.decode('utf-8', 'replace')[:300]}")
        raw_exit, _, data = result.stdout.partition(b"\0")
        return omp.Poll(gone=False, exit=parse_exit(raw_exit), data=data)

    async def output(self, path: str, *, head: int, tail: int) -> Output:
        result = await self._b.exec(["/bin/sh", "-c", _OUTPUT_SCRIPT, "sh", path, str(head), str(tail)], seconds=30)
        first, _, rest = result.stdout.partition(b"\n")
        size = int(first) if first.strip().isdigit() else 0
        head_bytes = rest[: min(head, size)]
        tail_bytes = rest[min(head, size) :]
        truncated = size > head + tail
        text = clean_output(head_bytes)
        if tail_bytes:
            gap = size - len(head_bytes) - len(tail_bytes)
            text += f"\n…（中间省略 {gap} 字节）…\n" if truncated else ""
            text += clean_output(tail_bytes)
        return Output(text=text, size=size, truncated=truncated)

    async def tail_text(self, path: str, chars: int) -> str:
        return (await self.output(path, head=0, tail=chars * 3)).text[-chars:]

    async def file_size(self, path: str) -> int | None:
        result = await self._b.exec(["/bin/sh", "-c", _SIZE_SCRIPT, "sh", path], seconds=20)
        out = result.stdout.decode().strip()
        return int(out) if result.exit_code == 0 and out.isdigit() else None


def parse_exit(raw: bytes) -> omp.ExitInfo | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return omp.ExitInfo(exit=None, signal=None)
    code, sig = data.get("exit"), data.get("signal")
    return omp.ExitInfo(exit=code if isinstance(code, int) else None, signal=sig if isinstance(sig, int) else None)


class _Channel:
    """``omp.RpcChannel`` over one job directory."""

    def __init__(self, ops: JobOps, directory: str) -> None:
        self._ops = ops
        self._dir = directory

    async def send(self, frame: dict[str, Any]) -> None:
        await self._ops.send(self._dir, frame)

    async def close_stdin(self) -> None:
        await self._ops.close_stdin(self._dir)

    async def kill(self) -> None:
        await self._ops.kill(self._dir)

    async def poll(self, offset: int, limit: int) -> omp.Poll:
        return await self._ops.poll(self._dir, "events.jsonl", offset, limit)

    async def stderr_tail(self, limit: int) -> str:
        return await self._ops.tail_text(f"{self._dir}/stderr.log", limit)


# --- relay (agent jobs) -------------------------------------------------------------------------------------


def relay_enabled() -> bool:
    from yggdrasil import relay

    return relay.relay_url() is not None


# --- the runner ---------------------------------------------------------------------------------------------


class Workspaces(Protocol):
    """The ``WorkspaceManager`` methods jobs use."""

    async def backend_for(self, group_id: int, name: str) -> tuple[Any, str] | None: ...
    async def renew(self, group_id: int, name: str, cfg: ToolSettings) -> bool: ...
    async def extend(self, group_id: int, name: str, claim: str, until: float, cfg: ToolSettings) -> float | None: ...
    async def publish(
        self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]
    ) -> Link: ...
    async def unpublish(
        self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]
    ) -> bool: ...


class ReportDeferred(Exception):
    """The report cannot start now (no bot connection, the requester's queue is full); retry later."""


Reporter = Callable[[Job], Awaitable[str | None]]
"""Runs the report invocation of a finished job; returns its invocation id."""
Archiver = Callable[[Job, list[JobEvent]], Awaitable[None]]
"""Stores a settled job for good with its event log (oldest first)."""


def _default_workspaces() -> Workspaces:
    from yggdrasil.sandbox.manager import manager

    return manager()


def _default_settings(group_id: int) -> Settings:
    from yggdrasil.runtime import snapshot

    return snapshot().settings(group_id)


async def _default_report(job: Job) -> str | None:
    from yggdrasil.pipeline.report import run_report

    return await run_report(job)


async def _default_archive(job: Job, events: list[JobEvent]) -> None:
    from yggdrasil.sandbox.archive import archive_job

    await archive_job(job, events)


class JobRunner:
    """Starts, lists, cancels and polls background jobs. One per process (``runner()``)."""

    def __init__(
        self,
        *,
        workspaces: Callable[[], Workspaces] = _default_workspaces,
        redis: Callable[[], Redis] = redis_client,
        ops: Callable[[JobBackend], JobOps] = JobOps,
        report: Reporter = _default_report,
        archive: Archiver = _default_archive,
        settings: Callable[[int], Settings] = _default_settings,
        link_domain: Callable[[], str | None] | None = None,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._workspaces = workspaces
        self._redis = redis
        self._ops = ops
        self._report = report
        self._archive = archive
        self._settings = settings
        self._link_domain = link_domain or _link_domain
        self._poll_s = poll_s
        self._clock = clock
        self._wake = asyncio.Event()
        self._inflight: dict[str, asyncio.Task[None]] = {}

    def _r(self) -> Redis:
        return self._redis()

    # --- records --------------------------------------------------------------------------------------

    async def get(self, job_id: str) -> Job | None:
        data = cast(dict[str, str], await self._r().hgetall(job_key(job_id)))  # pyright: ignore[reportGeneralTypeIssues]
        return Job.from_hash(data) if data else None

    async def _save(self, job: Job, *fields: str) -> None:
        data = job.to_hash()
        keys = fields or tuple(k for k in data if k != "cancel")
        await self._r().hset(job_key(job.id), mapping={k: data[k] for k in keys})  # pyright: ignore[reportGeneralTypeIssues]

    async def for_group(self, group_id: int, *, limit: int = 10) -> list[Job]:
        """The group's jobs, newest first (running and recently finished)."""
        r = self._r()
        await r.zremrangebyscore(group_key(group_id), 0, self._clock() - RETENTION_S)
        ids = cast(list[str], await r.zrevrange(group_key(group_id), 0, limit - 1))
        jobs = [j for j in [await self.get(i) for i in ids] if j is not None]
        return jobs

    async def active(self) -> list[Job]:
        """Jobs not yet archived (running, or finished with the report still pending), newest first."""
        ids = cast(set[str], await self._r().smembers(ACTIVE_KEY))  # pyright: ignore[reportGeneralTypeIssues]
        jobs = [j for j in [await self.get(i) for i in ids] if j is not None]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    async def events(self, job_id: str, *, limit: int = EVENTS_KEEP) -> list[JobEvent]:
        raw = cast(list[str], await self._r().lrange(events_key(job_id), 0, limit - 1))
        out: list[JobEvent] = []
        for item in raw:
            at, _, text = item.partition("\t")
            try:
                out.append(JobEvent(at=float(at), text=text))
            except ValueError:
                continue
        return out

    async def _event(self, job: Job, text: str) -> None:
        r = self._r()
        await r.lpush(events_key(job.id), f"{self._clock()!r}\t{text[:300]}")  # pyright: ignore[reportGeneralTypeIssues]
        await r.ltrim(events_key(job.id), 0, EVENTS_KEEP - 1)  # pyright: ignore[reportGeneralTypeIssues]

    # --- starting -------------------------------------------------------------------------------------

    async def _admit(
        self, req: Requester, backend: Any, workspace: str, kind: JobKind, cfg: ToolSettings
    ) -> tuple[str, float]:
        """Attach the workspace, check what does not need the reservation and push the workspace's hard cap out
        so the job fits: (claim, seconds allowed)."""
        if cfg.sandbox_max_jobs <= 0:
            raise SandboxError("后台任务已关闭")
        await backend.working_dir()
        ref = backend.ref
        if ref is None:
            raise SandboxError("工作区不可用")
        limit_s = (cfg.sandbox_job_command_min if kind == "command" else cfg.sandbox_job_agent_min) * 60
        # A later invocation retires a workspace with less than sandbox_invocation_s left before its cap (the
        # manager's max-age rule), which would end the job: the cap must lie past the job's deadline by that much,
        # plus time to stop it.
        until = self._clock() + limit_s + cfg.sandbox_invocation_s + START_MARGIN_S
        if await self._workspaces().extend(req.group_id, workspace, ref.id, until, cfg) is None:
            raise SandboxError("工作区刚被重置或回收，稍后再试")
        return ref.id, float(limit_s)

    async def _reserve(self, job: Job, cfg: ToolSettings) -> None:
        data = job.to_hash()
        flat: list[str] = [x for kv in data.items() for x in kv]
        code = int(
            cast(
                int,
                await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
                    _RESERVE,
                    4,
                    running_key(job.group_id),
                    job_key(job.id),
                    group_key(job.group_id),
                    ACTIVE_KEY,
                    str(cfg.sandbox_max_jobs),
                    job.id,
                    job.kind,
                    repr(job.created_at),
                    JOB_KEY_PREFIX,
                    *flat,
                ),
            )
        )
        if code == 1:
            raise SandboxError(f"本群同时最多 {cfg.sandbox_max_jobs} 个后台任务，先等一个结束或用 cancel_job 取消")
        if code == 2:
            raise SandboxError("本群已经有一个后台大任务在跑，同时只能有一个；等它结束或用 cancel_job 取消")

    async def _cas(self, job_id: str, expect: str, **values: str) -> bool:
        """Set ``values`` on the job record only while its status is still ``expect``."""
        flat = [x for kv in values.items() for x in kv]
        done = await self._r().eval(_CAS_STATUS, 1, job_key(job_id), expect, *flat)  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, done)) == 1

    async def _abort_start(self, job: Job, ops: JobOps, error: str, *, kill: bool) -> None:
        """Undo a start that failed, was cancelled, took too long or has an unknown outcome: stop whatever may
        have started (the supervisor writes ``<dir>/pid`` before it answers), revoke the relay token and, unless
        the poller already failed the job, release its slot. Never raises."""
        if kill:
            try:
                await ops.kill(job.dir)
            except Exception:
                LOGGER.opt(exception=True).info("stopping half-started sandbox job {} failed", job.id)
        r = self._r()
        try:
            await r.hdel(keep_key(job.group_id, job.workspace), keep_field(job.id))  # pyright: ignore[reportGeneralTypeIssues]
            if job.kind == "agent":
                from yggdrasil import relay

                await relay.revoke_token(job.id)
            now = self._clock()
            if await self._cas(
                job.id, "starting", status="failed", error=error[:2000], ended_at=repr(now), report="skipped"
            ):
                job.status, job.error, job.ended_at, job.report = "failed", error[:2000], now, "skipped"
                await r.hdel(running_key(job.group_id), job.id)  # pyright: ignore[reportGeneralTypeIssues]
                await self._settle(job)
        except Exception:
            LOGGER.exception("cleaning up the failed start of sandbox job {} failed", job.id)

    def _new(
        self, req: Requester, kind: JobKind, workspace: str, claim: str, title: str, spec: str, limit_s: float
    ) -> Job:
        now = self._clock()
        return Job(
            id=random_id(8),
            kind=kind,
            group_id=req.group_id,
            user_id=req.user_id,
            trigger_message_id=req.trigger_message_id,
            invocation_id=req.invocation_id,
            request_text=req.request_text[:2000],
            title=" ".join(title.split())[:40] or KIND_TEXT[kind],
            spec=spec,
            workspace=workspace,
            claim=claim,
            created_at=now,
            deadline=now + limit_s,
            renewed_at=now,
        )

    async def _started(self, job: Job, pid: int, ops: JobOps) -> None:
        """Mark the job running, unless the poller has meanwhile given up on it (``START_GRACE_S``): then the
        process just started is stopped again and the start fails."""
        r = self._r()
        # The keep entry first: the moment the record says running, the reap must already spare the process.
        await r.hset(keep_key(job.group_id, job.workspace), keep_field(job.id), str(pid))  # pyright: ignore[reportGeneralTypeIssues]
        if not await self._cas(job.id, "starting", status="running", pid=str(pid), model=job.model, driver=job.driver):
            await asyncio.shield(self._abort_start(job, ops, "启动太慢", kill=True))
            raise SandboxError("后台任务启动太慢，已被判定失败，刚启动的进程也已停掉；可以重试")
        job.pid, job.status = pid, "running"
        await self._event(job, "已启动")
        self._wake.set()

    async def _start_failed(self, job: Job, ops: JobOps, e: BaseException, *, kill: bool) -> SandboxError | None:
        """Clean up after a start that raised ``e``; the error for the model (``None``: re-raise ``e``)."""
        await asyncio.shield(self._abort_start(job, ops, f"启动失败：{e}", kill=kill))
        if not isinstance(e, Exception):
            return None
        return SandboxError(f"后台任务没能启动（{e}）；可能已启动的部分已经停掉，这个任务不会再回报")

    async def start_command(self, req: Requester, backend: Any, *, workspace: str, script: str, title: str) -> Job:
        """Start ``script`` detached in the group's workspace ``workspace`` (``backend``: the invocation's session
        backend, attached to it)."""
        cfg = self._settings(req.group_id).tools
        claim, limit_s = await self._admit(req, backend, workspace, "command", cfg)
        job = self._new(req, "command", workspace, claim, title, script, limit_s)
        await self._reserve(job, cfg)
        ops = self._ops(backend)
        sent = False
        try:
            await ops.write(f"{job.dir}/script.sh", script.encode())
            sent = True
            pid = await ops.start_sh(job.dir)
        except BaseException as e:
            if (err := await self._start_failed(job, ops, e, kill=sent)) is None:
                raise
            raise err from e
        await self._started(job, pid, ops)
        LOGGER.bind(audit="sandbox", action="job_start", group=job.group_id, job=job.id).info(
            "sandbox job {} (command) started in group {} pid={}", job.id, job.group_id, pid
        )
        return job

    async def start_agent(self, req: Requester, backend: Any, *, workspace: str, task: str, title: str) -> Job:
        """Start omp on ``task`` in the group's workspace ``workspace``, talking to the model through the relay."""
        from yggdrasil import relay

        base_url = relay.relay_url()
        if base_url is None:
            raise SandboxError("没有配置模型中转，不能委托后台大任务")
        settings = self._settings(req.group_id)
        cfg = settings.tools
        claim, limit_s = await self._admit(req, backend, workspace, "agent", cfg)
        models = await relay.relay_models()
        if not models:
            raise SandboxError(
                "没有给后台大任务配置模型（控制台「模型路由」里的 agent 路由，需要 Anthropic 格式的 provider）"
            )
        model = models[0]
        job = self._new(req, "agent", workspace, claim, title, task, limit_s)
        job.model = model.id
        job.driver = omp.DriverState(since=self._clock()).to_json()
        await self._reserve(job, cfg)
        ops = self._ops(backend)
        sent = False
        try:
            token = await relay.issue_token(
                group_id=req.group_id,
                job_id=job.id,
                models=[model.id],
                max_input_tokens=cfg.sandbox_job_max_input_tokens,
                max_output_tokens=cfg.sandbox_job_max_output_tokens,
                ttl_s=int(limit_s) + 600,
            )
            specs = [
                omp.ModelSpec(
                    id=m.id,
                    name=m.name,
                    context_window=m.context_window,
                    max_tokens=m.max_tokens,
                    reasoning=m.reasoning,
                    vision=m.vision,
                )
                for m in models
                if m.id == model.id
            ]
            await ops.write(omp.MODELS_PATH, omp.models_yaml(base_url, specs).encode())
            rules = f"{job.dir}/rules.md"
            await ops.write(rules, render_rules(settings, job, links=bool(self._link_domain())).encode())
            argv, env = omp.launch(
                group_id=req.group_id, model=model.id, rules_path=rules, max_time_s=int(limit_s) + 120, token=token
            )
            sent = True
            pid = await ops.start_rpc(job.dir, argv, env, cwd=omp.OMP_HOME)
        except BaseException as e:
            if (err := await self._start_failed(job, ops, e, kill=sent)) is None:
                raise
            raise err from e
        await self._started(job, pid, ops)
        LOGGER.bind(audit="sandbox", action="job_start", group=job.group_id, job=job.id).info(
            "sandbox job {} (agent, {}) started in group {} pid={}", job.id, model.id, job.group_id, pid
        )
        return job

    # --- cancelling -----------------------------------------------------------------------------------

    async def cancel(self, job_id: str, *, by: str, group_id: int | None = None) -> Job | None:
        """Ask the poller to stop a running job (it does so within a poll interval). ``group_id`` restricts
        the lookup to one group; returns the job, or ``None`` when there is no such job."""
        job = await self.get(job_id)
        if job is None or (group_id is not None and job.group_id != group_id):
            return None
        if job.running and not job.cancel:
            await self._r().hset(job_key(job_id), "cancel", by)  # pyright: ignore[reportGeneralTypeIssues]
            job.cancel = by
            await self._event(job, f"请求取消（{by.split(':', 1)[0]}）")
            self._wake.set()
        return job

    async def wait_finished(self, job_id: str, *, within: float) -> Job | None:
        """Poll the record until the job is no longer running (or ``within`` seconds pass)."""
        deadline = time.monotonic() + within
        while True:
            job = await self.get(job_id)
            if job is None or not job.running or time.monotonic() >= deadline:
                return job
            await asyncio.sleep(0.5)

    # --- polling --------------------------------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        try:
            await self.archive_settled()
        except Exception:
            LOGGER.exception("archiving settled sandbox jobs failed")
        while True:
            try:
                await self.tick()
            except Exception:
                LOGGER.exception("sandbox job poll failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self._poll_s)
            except TimeoutError:
                pass

    async def tick(self, *, wait: bool = False) -> None:
        """Visit every active job not already being visited by this process; ``wait`` (tests) awaits them."""
        ids = cast(set[str], await self._r().smembers(ACTIVE_KEY))  # pyright: ignore[reportGeneralTypeIssues]
        for job_id in sorted(ids):
            task = self._inflight.get(job_id)
            if task is not None and not task.done():
                continue
            task = asyncio.create_task(self._visit(job_id), name=f"sandbox-job-{job_id}")
            self._inflight[job_id] = task
            task.add_done_callback(lambda t, i=job_id: self._inflight.get(i) is t and self._inflight.pop(i))
        if wait:
            pending = [t for t in self._inflight.values() if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop in-flight visits (shutdown); the jobs themselves keep running and are resumed later."""
        tasks = [t for t in self._inflight.values() if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _visit(self, job_id: str) -> None:
        token = random_id(12)
        lock = f"{LOCK_PREFIX}{job_id}"
        if not await self._r().set(lock, token, nx=True, ex=LOCK_TTL):
            return  # another replica (or a stale lock) has it
        report: Job | None = None
        try:
            job = await self.get(job_id)
            if job is None:
                await self._r().srem(ACTIVE_KEY, job_id)  # pyright: ignore[reportGeneralTypeIssues]
                return
            if job.status == "starting":
                # Only while still starting: a start finishing right now wins, and this visit leaves it alone.
                if self._clock() - job.created_at > START_GRACE_S and await self._cas(
                    job.id, "starting", status="failed"
                ):
                    await self._finish(job, "failed", error="启动过程中断（bot 重启或启动太慢）")
            elif job.status == "running":
                await self._step(job)
            if job.status in TERMINAL:
                report = await self._claim_report(job)
        except Exception:
            LOGGER.exception("sandbox job {} poll failed", job_id)
        finally:
            try:
                await asyncio.shield(self._r().eval(_UNLOCK, 1, lock, token))  # pyright: ignore[reportGeneralTypeIssues, reportArgumentType]
            except Exception:
                LOGGER.exception("releasing job lock {} failed", job_id)
        if report is not None:
            # Tracked like an event-triggered reply, so a rolling update's drain waits for it; shielded so the
            # poller's own cancellation at shutdown does not cut a reply half sent.
            from yggdrasil.bot.inflight import track

            await asyncio.shield(track(self._run_report(report)))

    async def _step(self, job: Job) -> None:
        now = self._clock()
        found = await self._workspaces().backend_for(job.group_id, job.workspace)
        if found is None or found[1] != job.claim:
            if found is not None:
                await found[0].aclose()
            await self._finish(job, "failed", error=_GONE)
            return
        backend = found[0]
        try:
            ops = self._ops(backend)
            if job.cancel:
                await self._stop(job, ops, "cancelled")
                return
            if now >= job.deadline:
                await self._stop(job, ops, "timeout")
                return
            await self._renew(job)
            if job.kind == "command":
                await self._step_command(job, ops)
            else:
                await self._step_agent(job, ops)
        except WorkspaceError as e:
            # Unreachable or gone: a gone sandbox shows up as "claim changed / no workspace" on a later poll,
            # a restarting one recovers; the deadline bounds both.
            LOGGER.warning("sandbox job {} poll: workspace error {}", job.id, e)
        finally:
            await backend.aclose()

    async def _renew(self, job: Job) -> None:
        cfg = self._settings(job.group_id).tools
        if self._clock() - job.renewed_at < cfg.sandbox_idle_min * 60 / 4:
            return
        job.renewed_at = self._clock()
        await self._save(job, "renewed_at")
        if not await self._workspaces().renew(job.group_id, job.workspace, cfg):
            LOGGER.warning("sandbox job {}: renewing the workspace failed", job.id)

    async def _step_command(self, job: Job, ops: JobOps) -> None:
        poll = await ops.poll(job.dir)
        if poll.gone:
            await self._finish(job, "failed", error=_GONE)
            return
        if poll.exit is None:
            return
        out = await ops.output(f"{job.dir}/out", head=HEAD_CHARS, tail=TAIL_CHARS)
        code, sig = poll.exit.exit, poll.exit.signal
        job.exit_code = code
        how = f"退出码 {code}" if code is not None else f"被信号 {sig} 终止"
        result = f"{how}，输出 {out.size} 字节（完整输出在 {job.dir}/out）\n{out.text}".rstrip()
        await self._finish(job, "succeeded" if code == 0 else "failed", result=result)

    def _driver(self, job: Job, ops: JobOps) -> omp.OmpDriver:
        async def on_event(text: str) -> None:
            await self._event(job, text)

        async def host(name: str, args: dict[str, Any]) -> tuple[str, bool]:
            return await self._host_tool(job, ops, name, args)

        return omp.OmpDriver(
            omp.DriverState.from_json(job.driver),
            _Channel(ops, job.dir),
            host,
            session_dir=omp.session_dir(job.group_id),
            model=job.model,
            task=job.spec,
            tools=omp.host_tools(links=bool(self._link_domain())),
            on_event=on_event,
            clock=self._clock,
        )

    async def _step_agent(self, job: Job, ops: JobOps) -> None:
        driver = self._driver(job, ops)
        try:
            finished = await driver.step()
        finally:
            job.driver = driver.state.to_json()
            await self._save(job, "driver")
        if finished is not None:
            status: JobStatus = "succeeded" if finished.ok else "failed"
            await self._finish(job, status, result=finished.text or "", error=finished.error)

    async def _stop(self, job: Job, ops: JobOps, status: JobStatus) -> None:
        result, error = "", ""
        if job.kind == "agent":
            driver = self._driver(job, ops)
            finished = await driver.stop(wait=asyncio.sleep)
            job.driver = driver.state.to_json()
            await self._save(job, "driver")
            result = finished.text or ""
        else:
            try:
                await ops.kill(job.dir)
            except Exception:
                LOGGER.exception("killing sandbox job {} failed", job.id)
            try:
                out = await ops.output(f"{job.dir}/out", head=HEAD_CHARS, tail=TAIL_CHARS)
                result = f"输出 {out.size} 字节（完整输出在 {job.dir}/out）\n{out.text}".rstrip()
            except Exception:
                LOGGER.exception("reading the output of sandbox job {} failed", job.id)
        if status == "timeout":
            error = f"超过最长运行时间（{fmt_duration(job.deadline - job.created_at)}），已停止"
        else:
            error = "已按要求取消"
        await self._finish(job, status, result=result, error=error)

    async def _finish(self, job: Job, status: JobStatus, *, result: str = "", error: str = "") -> None:
        """Record the outcome and release what the running job held (keep entry, slot, relay token). Leftover
        processes are left to the next end-of-invocation reap, which spares the services behind public links."""
        job.status, job.ended_at = status, self._clock()
        job.result, job.error = result[:20000], error[:2000]
        if job.kind == "agent":
            from yggdrasil import relay

            try:
                await relay.revoke_token(job.id)
                usage = await relay.relay_usage(job.id)
                job.usage = {
                    "requests": usage.requests,
                    "input_tokens": usage.input_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "output_tokens": usage.output_tokens,
                }
            except Exception:
                LOGGER.exception("relay cleanup for sandbox job {} failed", job.id)
        job.report = "skipped" if job.cancel.startswith("chat:") else "pending"
        job.report_at = self._clock()
        r = self._r()
        await r.hdel(keep_key(job.group_id, job.workspace), keep_field(job.id))  # pyright: ignore[reportGeneralTypeIssues]
        await r.hdel(running_key(job.group_id), job.id)  # pyright: ignore[reportGeneralTypeIssues]
        await self._save(job, "status", "ended_at", "result", "error", "exit_code", "usage", "report", "report_at")
        await self._event(job, f"{STATUS_TEXT[status]}" + (f"：{error[:100]}" if error else ""))
        LOGGER.bind(audit="sandbox", action="job_end", group=job.group_id, job=job.id).info(
            "sandbox job {} ended: {} {}", job.id, status, error[:200]
        )
        if job.report == "skipped":
            await self._bill(job)

    async def _bill(self, job: Job) -> None:
        """Charge the relay tokens to the group's daily budget when no report invocation carries them."""
        tokens = job.usage.get("input_tokens", 0) + job.usage.get("output_tokens", 0)
        if tokens:
            from yggdrasil.pipeline import ratelimit

            try:
                await ratelimit.add_tokens(job.group_id, tokens)
            except Exception:
                LOGGER.exception("billing sandbox job {} failed", job.id)

    # --- host tools (omp) -----------------------------------------------------------------------------

    async def _host_tool(self, job: Job, ops: JobOps, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        cfg = self._settings(job.group_id).tools
        if name == "progress":
            text = " ".join(str(args.get("text", "")).split())[:300]
            job.progress = text
            await self._save(job, "progress")
            await self._event(job, f"进度：{text}")
            return "已更新进度", False
        if name == "show":
            path = posixpath.normpath(posixpath.join(DEFAULT_ROOT, str(args.get("path", ""))))
            if not path.startswith(DEFAULT_ROOT + "/"):
                return f"路径必须在 {DEFAULT_ROOT} 里面", True
            if not path.lower().endswith(_IMAGE_EXTS):
                return "只能发 PNG/JPEG/GIF/WebP 图片", True
            if any(x.get("path") == path for x in job.images):
                return "已记下这张图，任务结束后会随结果一起发到群里（发之前还要过审核）。", False
            if len(job.images) >= MAX_SHOW:
                return f"这个任务最多发 {MAX_SHOW} 张图，已经够了", True
            size = await ops.file_size(path)
            if size is None:
                return f"文件不存在：{path}", True
            if size > MAX_SHOW_BYTES:
                return "图片太大（上限 10MB）", True
            caption = " ".join(str(args.get("caption", "")).split())[:200]
            job.images = [*job.images, {"path": path, "caption": caption}]
            await self._save(job, "images")
            await self._event(job, f"show {path}")
            return "已记下这张图，任务结束后会随结果一起发到群里（发之前还要过审核）。", False
        if name in ("publish", "unpublish"):
            port = args.get("port")
            if not isinstance(port, int):
                return "port 必须是整数", True
            audit = {"job": job.id}
            try:
                if name == "publish":
                    link = await self._workspaces().publish(job.group_id, job.workspace, port, cfg, audit=audit)
                    job.links = [*(x for x in job.links if x.get("port") != port), {"port": port, "url": link.url}]
                    await self._save(job, "links")
                    await self._event(job, f"publish {port} → {link.url}")
                    return f"已公开端口 {port}：{link.url}\n这个链接任何人都能打开；在最终回答里写出它。", False
                closed = await self._workspaces().unpublish(job.group_id, job.workspace, port, cfg, audit=audit)
            except SandboxError as e:
                return str(e), True
            job.links = [x for x in job.links if x.get("port") != port]
            await self._save(job, "links")
            await self._event(job, f"unpublish {port}")
            return (f"端口 {port} 的链接已撤销" if closed else f"端口 {port} 没有公开的链接"), False
        return f"没有这个工具：{name}", True

    # --- reporting ------------------------------------------------------------------------------------

    async def _claim_report(self, job: Job) -> Job | None:
        """Under the job lock: decide whether this visit runs the report; marks it running when so."""
        now = self._clock()
        if job.report == "running" and now - job.report_at > REPORT_STALE_S:
            job.report, job.report_attempts = "pending", job.report_attempts + 1
            await self._save(job, "report", "report_attempts")
        if job.report == "pending" and job.report_attempts >= REPORT_ATTEMPTS:
            job.report = "failed"
            await self._save(job, "report")
            await self._bill(job)
        if job.report in ("done", "skipped", "failed", ""):
            await self._settle(job)
            return None
        if job.report != "pending" or now < job.report_at:
            return None
        job.report, job.report_at = "running", now
        await self._save(job, "report", "report_at")
        return job

    async def _run_report(self, job: Job) -> None:
        try:
            invocation = await self._report(job)
        except ReportDeferred as e:
            now = self._clock()
            if job.ended_at is not None and now - job.ended_at > REPORT_GIVE_UP_S:
                LOGGER.warning("sandbox job {}: giving up on its report ({})", job.id, e)
                job.report = "failed"
                await self._save(job, "report")
                await self._bill(job)
            else:
                job.report, job.report_at = "pending", now + REPORT_RETRY_S
                await self._save(job, "report", "report_at")
            return
        except Exception:
            LOGGER.exception("report of sandbox job {} failed", job.id)
            job.report, job.report_attempts = "pending", job.report_attempts + 1
            job.report_at = self._clock() + REPORT_RETRY_S
            await self._save(job, "report", "report_attempts", "report_at")
            return
        job.report, job.report_invocation = "done", invocation or ""
        await self._save(job, "report", "report_invocation")
        await self._settle(job)

    async def _settle(self, job: Job) -> None:
        """The report is settled: archive the job for good, then stop polling it; the Redis record stays for
        ``RETENTION_S``. If archiving fails the job stays active and the next visit tries again."""
        try:
            await self._archive(job, list(reversed(await self.events(job.id))))
        except Exception:
            LOGGER.exception("archiving sandbox job {} failed", job.id)
            return
        r = self._r()
        await r.srem(ACTIVE_KEY, job.id)  # pyright: ignore[reportGeneralTypeIssues]
        await r.expire(job_key(job.id), RETENTION_S)
        await r.expire(events_key(job.id), RETENTION_S)

    async def archive_settled(self) -> int:
        """Archive settled jobs still only in Redis (those finished before the archive existed); idempotent."""
        r = self._r()
        active = cast(set[str], await r.smembers(ACTIVE_KEY))  # pyright: ignore[reportGeneralTypeIssues]
        n = 0
        async for key in r.scan_iter(match=f"{GROUP_PREFIX}*", count=100):
            for job_id in cast(list[str], await r.zrange(key, 0, -1)):
                job = await self.get(job_id)
                if job is None or job.id in active or job.status not in TERMINAL:
                    continue
                await self._archive(job, list(reversed(await self.events(job.id))))
                n += 1
        return n


def _link_domain() -> str | None:
    from yggdrasil.config import APP_CONFIG

    return APP_CONFIG.sandbox_link_domain


def render_rules(settings: Settings, job: Job, *, links: bool) -> str:
    from yggdrasil.prompts import render

    return (
        render(
            "job_rules",
            persona=settings.persona,
            job_id=job.id,
            job_dir=job.dir,
            minutes=int((job.deadline - job.created_at) // 60),
            links=links,
        )
        + "\n"
    )


@cache
def runner() -> JobRunner:
    return JobRunner()

"""Background job records: ``Job``, where it lives in Redis, and the status texts chat, reports and the console show.

``ygg:sandbox:job:<id>`` (a hash) holds the record, ``ygg:sandbox:jobs:running:<gid>`` the group's running jobs
(the concurrency limit), ``ygg:sandbox:jobs:group:<gid>`` the group's history and ``ygg:sandbox:jobs:active`` every
job the poller (``yggdrasil.sandbox.jobs``) still has to look at (running, or its report pending). While a job runs,
its supervisor pid sits in its workspace's keep hash under ``job:<id>``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

from yggdrasil.core.log import LOGGER
from yggdrasil.sandbox.backend import DEFAULT_ROOT

JOBS_ROOT = f"{DEFAULT_ROOT}/.ygg/jobs"

JOB_KEY_PREFIX = "ygg:sandbox:job:"
ACTIVE_KEY = "ygg:sandbox:jobs:active"
RUNNING_PREFIX = "ygg:sandbox:jobs:running:"
GROUP_PREFIX = "ygg:sandbox:jobs:group:"
LOCK_PREFIX = "ygg:sandbox:joblock:"

RETENTION_S = 7 * 86400
"""How long a settled job's Redis record stays (the chat ``jobs`` tool reads it); the archive keeps it for good."""

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


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


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


class ReportDeferred(Exception):
    """The report cannot start now (no bot connection, the requester's queue is full); retry later."""

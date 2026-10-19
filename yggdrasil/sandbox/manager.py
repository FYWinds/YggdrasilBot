"""Per-topic workspaces of a group: SandboxClaims from the warm pool, their Redis records, the per-group and
global caps, renewal, links, sessions.

A workspace is ``(group_id, name)`` with a one-line description; a group has at most
``sandbox_max_workspaces_per_group`` of them and the deployment at most ``sandbox_max_workspaces`` pods. An
invocation selects one (``Session.open``), attaches lazily on first use and may switch to another.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import shlex
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, cast

import httpx
from pydantic_ai.workspaces import WorkspaceError, WorkspaceRef, WorkspaceUnavailableError

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.kv import redis_client
from yggdrasil.sandbox.backend import (
    DEFAULT_ROOT,
    PROVIDER,
    Attachment,
    CommandLimits,
    Endpoint,
    SandboxdWorkspace,
    StaticResolver,
)
from yggdrasil.sandbox.errors import SandboxError, SandboxNotConfigured, SandboxToolError
from yggdrasil.sandbox.k8s import KubeClient, KubeError
from yggdrasil.sandbox.state import (
    INDEX_KEY_PREFIX,
    WS_KEY_PREFIX,
    Link,
    LinkRecord,
    Mapping,
    WorkspaceInfo,
    claim_name,
    clean_description,
    group_lock_key,
    index_key,
    keep_key,
    link_id,
    link_name,
    lock_key,
    name_error,
    parse_ws_key,
    shutdown_at,
    validate_port,
    ws_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Sequence
    from contextlib import AbstractAsyncContextManager

    from redis.asyncio import Redis

    from yggdrasil.settings import ToolSettings

_LABELS = {"app.kubernetes.io/part-of": "yggdrasil", "app.kubernetes.io/component": "sandbox"}
_LABEL_SELECTOR = ",".join(f"{k}={v}" for k, v in _LABELS.items())
_GROUP_LABEL = "yggdrasil/group"
_NAME_LABEL = "yggdrasil/workspace"
_WORKSPACE_LABEL = "sandbox.users.io/workspace"
"""Pod label carrying the claim name; link Services select on it."""

READY_TIMEOUT = 60.0
_LOCK_TTL = 150
"""Seconds a workspace's Redis lock lives; covers the longest holder (creating its pod, ~READY_TIMEOUT)."""
_ATTACH_LOCK_WAIT = 120.0
"""How long attaching waits for the workspace lock (another invocation creating or releasing it)."""
_RELEASE_LOCK_WAIT = 20.0
"""How long the last release waits for the lock before it gives up reaping (it still decrements)."""
_CLOSE_LOCK_WAIT = 5.0
"""How long dropping an unused reservation at the end of an invocation waits for the workspace lock."""
_GROUP_LOCK_TTL = 60
"""Seconds the per-group lock lives; reserving only reads records and deletes evicted claims."""
_GROUP_LOCK_WAIT = 30.0
_PENDING_TTL = 1800
"""Seconds a pending record (no pod yet) lives unused; refreshed when its pod is being created."""
_LOCK_POLL = 0.2
_REAP = "/opt/ygg/reap"
_YGG = "/opt/ygg/ygg"
_PUBLIC_ADDRS = frozenset({"0.0.0.0", "::"})
"""Listener addresses a public link can reach (all interfaces)."""

_GONE_NOTICE = (
    "[工作区] 这个工作区之前的沙箱已过期或出故障被回收，里面的文件都没了；这是同名新建的空工作区，记得更新它的说明。"
)
_AGED_NOTICE = (
    "[工作区] 这个工作区到了最长存活时间，已回收，里面的文件都没了；这是同名新建的空工作区，记得更新它的说明。"
)
_RESET_NOTICE = "[工作区] 工作区已按要求重置；这是新建的空工作区。"
_RESTARTED_NOTICE = "[工作区] 工作区的沙箱重启过，之前的文件都没了。"
_CHOOSE_HINT = (
    '同一话题的后续请求（接着做、改一改、再看看）用 open_workspace(name="名字") 继续用原来的；和现有话题都无关的'
    '新任务用 open_workspace(name="新名字", new=True, description="一句话：放什么、哪个话题") 新建。'
)
_CREATE_HINT = (
    '用 open_workspace(name="新名字", new=True, description="一句话：放什么、哪个话题") 新建一个'
    "（名字用小写字母、数字和 -，例如 pelican-game）。"
)

# Every script takes KEYS[1] = the record, KEYS[2] = its keep hash, KEYS[3] = the group's index set where used.
# Remove a record (and its keep hash and index entry) only if it still names this claim ("" = pending).
_DROP_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return 0 end "
    "redis.call('DEL', KEYS[1], KEYS[2]) redis.call('SREM', KEYS[3], ARGV[2]) return 1"
)
# Change `active` only while the record still names this (non-empty) claim; returns the new value or -1.
_INCR_IF_CLAIM = (
    "if ARGV[1] ~= '' and redis.call('HGET', KEYS[1], 'claim') == ARGV[1] then "
    "return redis.call('HINCRBY', KEYS[1], 'active', ARGV[2]) end return -1"
)
# Write fields (ARGV[2..]) only while the record still names claim ARGV[1]: 1 = written, 0 = it moved on.
_SAVE_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return 0 end "
    "redis.call('HSET', KEYS[1], unpack(ARGV, 2)) return 1"
)
# Write fields (ARGV) only while the record exists.
_SAVE_IF_EXISTS = (
    "if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end redis.call('HSET', KEYS[1], unpack(ARGV)) return 1"
)
_PENDING_FIELDS = (
    "'claim', '', 'sandbox', '', 'pod_ip', '', 'active', '0', 'recreate', '0', 'links', '[]', "
    "'expires_at', '0', 'max_until', '0'"
)
# Destroy the pod of a record only if it still names claim ARGV[1] and at most ARGV[2] invocations use it:
# 1 = done (the record removed, or kept as pending with TTL ARGV[6] when ARGV[5] is 1), 0 = busy (and `recreate`
# marked when ARGV[3] is 1), 2 = background jobs run in it (checked only when ARGV[4] is 1; nothing marked),
# -1 = the record names another claim or is gone.
_DESTROY_IF_IDLE = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return -1 end "
    "if ARGV[4] == '1' then for _, f in ipairs(redis.call('HKEYS', KEYS[2])) do "
    "if string.sub(f, 1, 4) == 'job:' then return 2 end end end "
    "if tonumber(redis.call('HGET', KEYS[1], 'active') or '0') <= tonumber(ARGV[2]) then "
    "redis.call('DEL', KEYS[2]) "
    f"if ARGV[5] == '1' then redis.call('HSET', KEYS[1], {_PENDING_FIELDS}) "
    "redis.call('EXPIRE', KEYS[1], ARGV[6]) "
    "else redis.call('DEL', KEYS[1]) redis.call('SREM', KEYS[3], ARGV[7]) end return 1 end "
    "if ARGV[3] == '1' then redis.call('HSET', KEYS[1], 'recreate', '1') end return 0"
)
_RETIRE_ACTIVE = "1000000000"
"""``max_active`` that makes ``_DESTROY_IF_IDLE`` ignore users (retiring a record whose pod others still use)."""
# Refresh a pending record before its pod is created (TTL, LRU position); 0 when it is gone or not pending.
_TOUCH_PENDING = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= '' then return 0 end "
    "redis.call('EXPIRE', KEYS[1], ARGV[1]) redis.call('HSET', KEYS[1], 'last_used', ARGV[2]) return 1"
)
# Give a pending record its new pod (fields ARGV): 1 = done, 0 = the record was removed or claimed meanwhile.
_SET_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= '' then return 0 end "
    "redis.call('DEL', KEYS[2]) redis.call('HSET', KEYS[1], unpack(ARGV)) redis.call('PERSIST', KEYS[1]) return 1"
)
# Push max_until (ARGV[2]) out while the record names claim ARGV[1]; returns the resulting value or nil.
_EXTEND = (
    "if ARGV[1] == '' or redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return false end "
    "if tonumber(ARGV[2]) > (tonumber(redis.call('HGET', KEYS[1], 'max_until') or '0') or 0) then "
    "redis.call('HSET', KEYS[1], 'max_until', ARGV[2]) end return redis.call('HGET', KEYS[1], 'max_until')"
)
# Drop an index entry whose record is gone (KEYS[1] = record, KEYS[2] = index).
_PRUNE_INDEX = "if redis.call('EXISTS', KEYS[1]) == 0 then redis.call('SREM', KEYS[2], ARGV[1]) return 1 end return 0"
_UNLOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
_BUSY_JOBS = 2

_TREE_SKIP = (
    ".local",
    "node_modules",
    ".git",
    ".ygg",
    ".pydantic-ai-harness",
    "__pycache__",
    ".cache",
    ".npm",
    ".npm-global",
    ".config",
    ".ipython",
    ".jupyter",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".pydantic-ai-conformance-*",
)
_TREE_LINES = 50
_NOTES_CHARS = 1500
_TREE_SCRIPT = (
    'cd -- "$1" || exit 1; find . -mindepth 1 \\( '
    + " -o ".join(f"-name {shlex.quote(n)}" for n in _TREE_SKIP)
    + " \\) -prune -o -printf '%y\\t%s\\t%T@\\t%P\\n' | head -n 2000"
)


class _ClaimLost(SandboxToolError):
    """The claim vanished or its pod failed for good while waiting for it to become ready."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim_ready(claim: dict[str, Any]) -> tuple[bool, str | None, str | None, str | None]:
    """(ready, pod ip, sandbox name, terminal failure reason) from a SandboxClaim object."""
    status = claim.get("status") or {}
    sandbox = status.get("sandbox") or {}
    ips = [ip for ip in sandbox.get("podIPs") or [] if ip]
    name = sandbox.get("name") or None
    ready = False
    failure = None
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready":
            ready = cond.get("status") == "True"
            if not ready and cond.get("reason") in {"PodFailed", "PodSucceeded", "TemplateNotFound", "SandboxExpired"}:
                failure = str(cond.get("reason"))
    return ready and bool(ips), (ips[0] if ips else None), name, failure


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def format_tree(raw: str, *, limit: int = _TREE_LINES) -> str:
    """Render `find -printf '%y\\t%s\\t%T@\\t%P\\n'` output as at most ``limit`` sorted lines."""
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4 or not parts[3]:
            continue
        kind, size, mtime, path = parts
        try:
            stamp = datetime.fromtimestamp(float(mtime), UTC).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            stamp = "?"
        if kind == "d":
            rows.append((path, f"{path}/"))
        else:
            size_text = _fmt_size(int(size)) if size.isdigit() else "?"
            rows.append((path, f"{path}  {size_text}  {stamp}"))
    rows.sort()
    lines = [text for _, text in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"…（还有 {len(rows) - limit} 项没列出）")
    return "\n".join(lines)


def reap_argv(keep: dict[str, str], links: Iterable[LinkRecord]) -> list[str]:
    """The end-of-invocation reap: spare every process tree in the keep hash (the kernel, background jobs) and
    each link's listener (its process group and its tree)."""
    argv = [_REAP]
    for _, pid in sorted(keep.items()):
        if pid.isdigit():
            argv += ["--keep-tree", pid]
    for link in links:
        if link.pgid is not None:
            argv += ["--keep-pgid", str(link.pgid)]
        if link.pid is not None:
            argv += ["--keep-tree", str(link.pid)]
    return argv


@dataclass(frozen=True)
class Listener:
    """What ``ygg port`` found listening on a port."""

    pid: int | None
    pgid: int | None
    addr: str
    cmdline: str


def parse_listener(port: int, exit_code: int, stdout: bytes, stderr: bytes) -> Listener:
    """Decode ``ygg port`` output; ``SandboxError`` (Chinese, for the model) when the port cannot be published."""
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        if exit_code != 0 or not lines:
            raise ValueError(stderr.decode("utf-8", "replace").strip()[:300] or f"exit code {exit_code}")
        data = json.loads(lines[-1])
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError as e:
        raise SandboxError(f"查不到端口 {port} 的监听进程：{e}") from e
    if not data.get("listening"):
        raise SandboxError(
            f"端口 {port} 上没有程序在监听。先把服务起起来（监听 0.0.0.0:{port}，例如 start(...) 后 "
            f"wait_port({port})），再 publish。"
        )
    addr = str(data.get("addr") or "")
    if addr not in _PUBLIC_ADDRS:
        raise SandboxError(
            f"端口 {port} 上的服务只监听了 {addr or '本机地址'}，外面访问不到；改成监听 0.0.0.0:{port} 再 publish。"
        )
    pid, pgid = data.get("pid"), data.get("pgid")
    return Listener(
        pid=pid if type(pid) is int else None,
        pgid=pgid if type(pgid) is int else None,
        addr=addr,
        cmdline=str(data.get("cmdline") or ""),
    )


class WorkspaceMissing(SandboxToolError):
    """The selected workspace's record is gone (expired, evicted or destroyed): the model has to choose again."""


@dataclass(frozen=True)
class Opened:
    """What ``Session.open`` did: the selected workspace, whether it is new, the workspaces evicted to make room
    and the one this invocation switched away from."""

    info: WorkspaceInfo
    created: bool
    evicted: tuple[WorkspaceInfo, ...]
    left: str | None
    updated: bool = False


def _span(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def format_workspaces(
    infos: Sequence[WorkspaceInfo], *, now: float, titles: dict[str, str], current: str | None = None
) -> str:
    """The group's workspaces for the model: name, description, age, idle time, time left, links, jobs."""
    lines: list[str] = []
    for w in infos:
        mark = "（当前选中）" if w.name == current else ""
        lines.append(f"- {w.name}{mark}：{w.description or '（没有说明）'}")
        if w.pending:
            lines.append("  空的，还没启动（第一次用时创建）")
            continue
        facts = [f"已存在 {_span(now - w.created_at)}", "正在使用" if w.active else f"闲置 {_span(now - w.last_used)}"]
        if w.links or w.jobs:
            facts.append(f"最多还能保留 {_span(w.max_until - now)}")
        else:
            facts.append(f"约 {_span(w.expires_at - now)}后闲置回收")
        if w.links:
            facts.append("公开链接 " + "、".join(f"{x.url}（端口 {x.port}）" for x in w.links))
        if w.jobs:
            facts.append("后台任务 " + "、".join(f"{j}「{titles.get(j, '')}」" for j in w.jobs))
        lines.append("  " + "；".join(facts))
    return "\n".join(lines)


class WorkspaceManager:
    """Process-wide owner of the groups' workspace records. Build with ``manager()``."""

    def __init__(
        self,
        *,
        token: str | None,
        namespace: str,
        warmpool: str,
        link_domain: str | None = None,
        kube: KubeClient | None = None,
        redis: Callable[[], Redis] = redis_client,
        endpoint: Callable[[str], Endpoint] = Endpoint.for_pod,
    ) -> None:
        self._token = token
        self._ns = namespace
        self._warmpool = warmpool
        self._link_domain = link_domain
        self._kube_client = kube
        self._redis = redis
        # Pod IP -> sandboxd endpoint; tests map it onto a local sandboxd with published ports.
        self._endpoint = endpoint
        self._local_locks: defaultdict[tuple[int, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def configured(self) -> bool:
        return bool(self._token)

    @property
    def token(self) -> str:
        if not self._token:
            raise SandboxNotConfigured("工作区未配置（没有设置 YGG_SANDBOX_TOKEN）")
        return self._token

    def _kube(self) -> KubeClient:
        if self._kube_client is None:
            self._kube_client = KubeClient()
        return self._kube_client

    # --- paths ------------------------------------------------------------------------------------------

    def _claim_path(self, name: str = "") -> str:
        base = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{self._ns}/sandboxclaims"
        return f"{base}/{name}" if name else base

    def _sandbox_path(self, name: str) -> str:
        return f"/apis/agents.x-k8s.io/v1beta1/namespaces/{self._ns}/sandboxes/{name}"

    def _service_path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self._ns}/services"
        return f"{base}/{name}" if name else base

    def _route_path(self, name: str = "") -> str:
        base = f"/apis/traefik.io/v1alpha1/namespaces/{self._ns}/ingressroutes"
        return f"{base}/{name}" if name else base

    # --- public API ---------------------------------------------------------------------------------------

    def session(
        self, group_id: int, *, invocation_id: str | None, cfg: ToolSettings, workspace: str | None = None
    ) -> Session:
        """A lazy per-invocation handle (no I/O until the model first uses a workspace). ``workspace``
        pre-selects one (a job's report); otherwise the model selects with ``open_workspace``."""
        if not self.configured:
            raise SandboxNotConfigured("工作区未配置（没有设置 YGG_SANDBOX_TOKEN）")
        return Session(self, group_id, invocation_id=invocation_id, cfg=cfg, workspace=workspace)

    async def get(self, group_id: int, name: str) -> WorkspaceInfo | None:
        """One workspace; ``None`` when there is none by that name (a record whose claim is gone is dropped)."""
        m = await self._load(group_id, name)
        if m is None:
            return None
        if not m.pending and await self._kube().get(self._claim_path(m.claim)) is None:
            await self._drop(m, reason="claim gone")
            return None
        return m.info(await self._job_ids(m))

    async def workspaces(self, group_id: int) -> list[WorkspaceInfo]:
        """The group's workspaces (pending ones included), most recently used first; records whose claim is gone
        are dropped."""
        mappings = await self._group_mappings(group_id)
        live = await self._live_claims(group_id) if any(not m.pending for m in mappings) else set()
        out: list[WorkspaceInfo] = []
        for m in mappings:
            if not m.pending and m.claim not in live:
                await self._drop(m, reason="claim gone")
                continue
            out.append(m.info(await self._job_ids(m)))
        return sorted(out, key=lambda w: w.last_used, reverse=True)

    async def list(self) -> list[WorkspaceInfo]:
        """Every group's workspaces (console, metrics); pending ones included."""
        mappings = await self._all_mappings()
        live = await self._live_claims()
        return [m.info(await self._job_ids(m)) for m in mappings if m.pending or m.claim in live]

    async def listing(self, group_id: int, *, current: str | None = None) -> str:
        """The group's workspaces as text for the model ("" when it has none)."""
        infos = await self.workspaces(group_id)
        return format_workspaces(
            infos, now=time.time(), titles=await self._job_titles([j for w in infos for j in w.jobs]), current=current
        )

    async def status(self, info: WorkspaceInfo, *, current: str | None = None) -> str:
        """One workspace as text for the model, like a ``listing`` entry."""
        return format_workspaces([info], now=time.time(), titles=await self._job_titles(info.jobs), current=current)

    async def describe(self, group_id: int, name: str) -> str | None:
        """Files and the head of NOTES.md of a live workspace; ``None`` when pending, gone or unreadable."""
        try:
            return await self._describe(group_id, name)
        except Exception:
            LOGGER.exception("sandbox describe failed for group {} workspace {}", group_id, name)
            return None

    async def reserve(
        self,
        group_id: int,
        name: str,
        description: str,
        cfg: ToolSettings,
        *,
        mine: tuple[str, str] | None = None,
    ) -> list[WorkspaceInfo]:
        """Add the pending record of a new workspace, evicting the group's least recently used ones that no
        invocation uses and no background job runs in while the group is at its cap (``mine``: the caller's own
        attached workspace as (name, claim), which only the caller uses; a newer pod under the same name is not
        the caller's). A workspace whose lock is held (its pod being created, attached or released) is never
        evicted. Returns the evicted workspaces. Runs under the per-group lock: the cap holds across invocations
        and bot replicas."""
        if (why := name_error(name)) is not None:
            raise SandboxError(why)
        if not description:
            raise SandboxError("新建工作区要写 description：一句话说明放什么、属于哪个话题")
        cap = cfg.sandbox_max_workspaces_per_group
        async with self._redis_lock(group_lock_key(group_id), wait=_GROUP_LOCK_WAIT, ttl=_GROUP_LOCK_TTL) as got:
            if not got:
                raise SandboxToolError("本群正有别的请求在新建工作区，等太久了，稍后再试。")
            current = await self.workspaces(group_id)
            if existing := next((w for w in current if w.name == name), None):
                raise SandboxError(
                    f"已经有叫「{name}」的工作区了（{existing.description}）：要继续用它就不要传 new=True，"
                    "要新建就换个名字。"
                )
            evicted: list[WorkspaceInfo] = []
            excess = len(current) - cap + 1
            # Snapshot `active` / jobs only pick candidates: the destroy re-checks both atomically.
            for w in sorted(current, key=lambda w: w.last_used):
                if excess <= 0:
                    break
                if w.jobs or w.active > (1 if mine is not None and w.name == mine[0] else 0):
                    continue
                # Holding the workspace's lock keeps a create / attach / release from running while it goes.
                async with self._ws_lock(group_id, w.name, wait=0) as got:
                    if not got:
                        continue
                    m = await self._load(group_id, w.name)
                    if m is None:
                        excess -= 1
                        continue
                    allowed = 1 if mine == (m.name, m.claim) and not m.pending else 0
                    reason = f"evicted (group LRU) for {name}"
                    if await self._destroy_if_idle(m, reason=reason, max_active=allowed, check_jobs=True) == 1:
                        evicted.append(w)
                        excess -= 1
            if excess > 0:
                left = [w for w in current if w not in evicted]
                listing = format_workspaces(
                    left, now=time.time(), titles=await self._job_titles([j for w in left for j in w.jobs])
                )
                made = "、".join(f"「{w.name}」" for w in evicted)
                raise SandboxError(
                    f"本群已经有 {len(left)} 个工作区（上限 {cap} 个），而且都在使用中或有后台任务，没法回收"
                    + (f"（已经回收了 {made}，还不够）" if made else "")
                    + f"：\n{listing}\n"
                    '这次先用 open_workspace(name="名字") 继续用其中话题最接近的一个，或者等它们空闲后再新建。'
                )
            now = time.time()
            record = Mapping(
                group_id=group_id,
                name=name,
                description=description,
                claim="",
                sandbox=None,
                pod_ip="",
                created_at=now,
                last_used=now,
                expires_at=0.0,
                max_until=0.0,
            )
            async with self._r().pipeline(transaction=True) as pipe:
                pipe.hset(ws_key(group_id, name), mapping=record.to_hash())  # pyright: ignore[reportArgumentType]
                pipe.expire(ws_key(group_id, name), _PENDING_TTL)
                pipe.sadd(index_key(group_id), name)
                await pipe.execute()
        self._audit(group_id, "reserve", workspace=name, evicted=[w.name for w in evicted])
        return evicted

    async def set_description(self, group_id: int, name: str, description: str) -> bool:
        done = await self._r().eval(_SAVE_IF_EXISTS, 1, ws_key(group_id, name), "description", description)  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, done)) == 1

    async def destroy(self, group_id: int, name: str, *, reason: str) -> bool:
        """Destroy one workspace now (its pod and record), even while invocations or jobs use it."""
        m = await self._load(group_id, name)
        if m is None:
            return False
        await self._destroy_mapping(m, reason=reason)
        return True

    async def destroy_all(self, *, reason: str) -> int:
        mappings = await self._all_mappings()
        claims = {m.claim for m in mappings if not m.pending}
        try:
            claims |= await self._live_claims()
        except KubeError:
            LOGGER.exception("listing sandbox claims failed")
        destroyed = 0
        for claim in claims:
            try:
                if await self._kube().delete(self._claim_path(claim)):
                    destroyed += 1
            except Exception:
                LOGGER.exception("deleting sandbox claim {} failed", claim)
        for m in mappings:
            await self._r().delete(ws_key(m.group_id, m.name), keep_key(m.group_id, m.name))
        async for key in self._r().scan_iter(match=f"{INDEX_KEY_PREFIX}*", count=100):
            await self._r().delete(key)
        self._audit(None, "destroy_all", reason=reason, destroyed=destroyed)
        return destroyed

    async def reconcile(self) -> None:
        try:
            mappings = [m for m in await self._all_mappings() if not m.pending]
            if not mappings:
                return
            live = await self._live_claims()
            for m in mappings:
                if m.claim not in live:
                    await self._drop(m, reason="claim gone (reconcile)")
        except Exception:
            LOGGER.exception("sandbox reconcile failed")

    async def backend_for(self, group_id: int, name: str) -> tuple[SandboxdWorkspace, str] | None:
        """A backend bound to the workspace's current pod plus its claim name, for work outside an invocation
        (background jobs): no ``active`` counting, never creates a pod. ``None`` when the workspace has no live
        pod. The caller closes it with ``aclose()``."""
        m = await self._load(group_id, name)
        if m is None or m.pending:
            return None
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._drop(m, reason="claim gone")
            return None
        _, ip, _, failure = _claim_ready(claim)
        if failure:
            return None
        claim_id = m.claim

        async def alive() -> bool:
            return await self._kube().get(self._claim_path(claim_id)) is not None

        backend = SandboxdWorkspace(
            resolver=StaticResolver(self._endpoint(ip or m.pod_ip), claim_id, alive=alive),
            token=self.token,
            ref=WorkspaceRef(provider=PROVIDER, id=claim_id),
            audit={"group": group_id, "workspace": name},
        )
        return backend, claim_id

    async def renew(self, group_id: int, name: str, cfg: ToolSettings) -> bool:
        """Renew the workspace's claim (as an invocation's use would); False when it has none or it is gone."""
        m = await self._load(group_id, name)
        if m is None or m.pending:
            return False
        if await self._renew(m, cfg):
            return True
        await self._drop(m, reason="claim gone")
        return False

    async def extend(self, group_id: int, name: str, claim: str, until: float, cfg: ToolSettings) -> float | None:
        """Push the workspace's hard cap (``max_until``) out to at least ``until`` so a background job fits, and
        renew its claim; the resulting cap, or ``None`` when the workspace no longer runs ``claim``."""
        raw = await self._r().eval(_EXTEND, 1, ws_key(group_id, name), claim, repr(until))  # pyright: ignore[reportGeneralTypeIssues]
        if raw is None:
            return None
        m = await self._load(group_id, name)
        if m is None or m.claim != claim or not await self._renew(m, cfg):
            return None
        self._audit(group_id, "extend", workspace=name, claim=claim, max_until=round(m.max_until))
        return m.max_until

    async def publish(self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]) -> Link:
        """Give ``port`` of the workspace a public link. The listener must already run on all interfaces; it and
        its process tree are spared by the reap while the link lives, and the workspace then lives to its hard
        cap. Publishing a port that already has a link returns that link."""
        domain = self._link_domain
        if not domain:
            raise SandboxError("没有配置公开链接域名，不能公开端口")
        if (reason := validate_port(port)) is not None:
            raise SandboxError(reason)
        async with self._local_locks[group_id, name], _errors("公开端口"):
            m = await self._load(group_id, name)
            if m is None or m.pending:
                raise SandboxError("工作区已经被重置或回收，没法公开端口。")
            if existing := next((x for x in m.links if x.port == port), None):
                return existing.public()
            if len(m.links) >= cfg.sandbox_max_links:
                raise SandboxError(f"每个工作区最多 {cfg.sandbox_max_links} 个公开链接，先 unpublish 一个")
            listener = await self._listener(group_id, name, port)
            rid = link_id()
            await self._create_link_objects(m, rid, port, domain)
            link = LinkRecord(
                id=rid,
                port=port,
                url=f"https://{link_name(rid)}.{domain}",
                command=listener.cmdline,
                created_at=time.time(),
                pgid=listener.pgid,
                pid=listener.pid,
            )
            m.links.append(link)
            await self._save(m, "links")
            await self._renew(m, cfg)
        self._audit(
            group_id,
            "publish",
            workspace=name,
            claim=m.claim,
            port=port,
            url=link.url,
            pid=link.pid,
            pgid=link.pgid,
            command=link.command[:500],
            **audit,
        )
        return link.public()

    async def unpublish(self, group_id: int, name: str, port: int, cfg: ToolSettings, *, audit: dict[str, Any]) -> bool:
        """Remove the public link of ``port`` (the process keeps running); False when it has none."""
        async with self._local_locks[group_id, name], _errors("撤销链接"):
            m = await self._load(group_id, name)
            link = next((x for x in m.links if x.port == port), None) if m else None
            if m is None or link is None:
                return False
            await self._delete_link_objects(link.id)
            m.links = [x for x in m.links if x.port != port]
            await self._save(m, "links")
            await self._renew(m, cfg)
        self._audit(group_id, "unpublish", workspace=name, claim=m.claim, port=port, url=link.url, **audit)
        return True

    async def _listener(self, group_id: int, name: str, port: int) -> Listener:
        found = await self.backend_for(group_id, name)
        if found is None:
            raise SandboxError("工作区已经被回收，没法公开端口。")
        backend, _ = found
        try:
            result = await backend.exec([_YGG, "port", str(port)], seconds=20)
        finally:
            await backend.aclose()
        return parse_listener(port, result.exit_code, result.stdout, result.stderr)

    # --- redis ----------------------------------------------------------------------------------------------

    def _r(self) -> Redis:
        return self._redis()

    async def _load(self, group_id: int, name: str) -> Mapping | None:
        data = cast(dict[str, str], await self._r().hgetall(ws_key(group_id, name)))  # pyright: ignore[reportGeneralTypeIssues]
        return Mapping.from_hash(group_id, data) if data else None

    async def _group_mappings(self, group_id: int) -> list[Mapping]:
        names = cast(set[str], await self._r().smembers(index_key(group_id)))  # pyright: ignore[reportGeneralTypeIssues]
        out: list[Mapping] = []
        for name in sorted(names):
            m = await self._load(group_id, name)
            if m is not None:
                out.append(m)
            else:  # expired (an unused reservation) or removed: only while the record is really gone
                await self._r().eval(_PRUNE_INDEX, 2, ws_key(group_id, name), index_key(group_id), name)  # pyright: ignore[reportGeneralTypeIssues]
        return out

    async def _all_mappings(self) -> list[Mapping]:
        out: list[Mapping] = []
        async for key in self._r().scan_iter(match=f"{WS_KEY_PREFIX}*", count=100):
            parsed = parse_ws_key(str(key))
            if parsed is not None and (m := await self._load(*parsed)) is not None:
                out.append(m)
        return out

    async def _job_ids(self, m: Mapping) -> tuple[str, ...]:
        fields = cast(list[str], await self._r().hkeys(keep_key(m.group_id, m.name)))  # pyright: ignore[reportGeneralTypeIssues]
        return tuple(sorted(f.removeprefix("job:") for f in fields if f.startswith("job:")))

    async def _job_titles(self, ids: Iterable[str]) -> dict[str, str]:
        from yggdrasil.sandbox.jobs import job_key

        return {i: str(await self._r().hget(job_key(i), "title") or "") for i in ids}  # pyright: ignore[reportGeneralTypeIssues]

    async def _save(self, m: Mapping, *fields: str) -> bool:
        """Write ``fields`` while the record still names ``m.claim``; False when it moved on."""
        data = m.to_hash()
        flat = [x for k in fields for x in (k, data[k])]
        done = await self._r().eval(_SAVE_IF_CLAIM, 1, ws_key(m.group_id, m.name), m.claim, *flat)  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, done)) == 1

    async def _incr_active(self, m: Mapping, delta: int) -> int:
        value = await self._r().eval(_INCR_IF_CLAIM, 1, ws_key(m.group_id, m.name), m.claim, str(delta))  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, value))

    async def _drop(self, m: Mapping, *, reason: str) -> None:
        """Remove the record (expired or destroyed) if it still names ``m.claim``."""
        dropped = await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
            _DROP_IF_CLAIM,
            3,
            ws_key(m.group_id, m.name),
            keep_key(m.group_id, m.name),
            index_key(m.group_id),
            m.claim,
            m.name,
        )
        if int(cast(int, dropped)):
            self._audit(m.group_id, "drop_mapping", workspace=m.name, claim=m.claim, reason=reason)

    async def _drop_unused(self, group_id: int, name: str) -> None:
        """Remove a reservation nobody created a pod for (the invocation that made it ended without using it)."""
        async with self._redis_lock(lock_key(group_id, name), wait=_CLOSE_LOCK_WAIT, ttl=_LOCK_TTL) as got:
            if not got:
                return  # someone is creating its pod right now
            m = await self._load(group_id, name)
            if m is not None and m.pending:
                await self._drop(m, reason="reservation unused")

    @asynccontextmanager
    async def _redis_lock(self, key: str, *, wait: float, ttl: int) -> AsyncIterator[bool]:
        """Hold a lock shared by all bot replicas, polling up to ``wait`` seconds; yields whether it was
        acquired."""
        token = link_id()
        deadline = time.monotonic() + wait
        while not (got := bool(await self._r().set(key, token, nx=True, ex=ttl))):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_LOCK_POLL)
        try:
            yield got
        finally:
            if got:
                try:
                    await asyncio.shield(self._r().eval(_UNLOCK, 1, key, token))  # pyright: ignore[reportGeneralTypeIssues, reportArgumentType]
                except Exception:
                    LOGGER.exception("releasing sandbox lock {} failed", key)

    def _ws_lock(self, group_id: int, name: str, *, wait: float) -> AbstractAsyncContextManager[bool]:
        """``ygg:sandbox:lock:<gid>:<name>``: attaching and the last release run under it, so a release never
        reaps or recreates a workspace another invocation is just attaching to."""
        return self._redis_lock(lock_key(group_id, name), wait=wait, ttl=_LOCK_TTL)

    # --- kubernetes -----------------------------------------------------------------------------------------

    async def _live_claims(self, group_id: int | None = None) -> set[str]:
        selector = _LABEL_SELECTOR if group_id is None else f"{_LABEL_SELECTOR},{_GROUP_LABEL}={group_id}"
        items = await self._kube().list(self._claim_path(), label_selector=selector)
        return {str(i.get("metadata", {}).get("name")) for i in items}

    async def _destroy_mapping(self, m: Mapping, *, reason: str) -> None:
        try:
            if not m.pending:
                await self._kube().delete(self._claim_path(m.claim))
        finally:
            await self._drop(m, reason=reason)
        self._audit(m.group_id, "destroy", workspace=m.name, claim=m.claim, reason=reason)

    async def _destroy_if_idle(
        self,
        m: Mapping,
        *,
        reason: str,
        max_active: int = 0,
        mark_recreate: bool = False,
        check_jobs: bool = False,
        keep_record: bool = False,
    ) -> int:
        """Atomically take the pod from the record if it still names ``m.claim`` and at most ``max_active``
        invocations use it, then delete the claim. ``keep_record`` leaves the record pending (same name and
        description; a fresh pod on next use) instead of removing it. Returns 1 (destroyed), 0 (in use;
        ``recreate`` marked if asked), 2 (background jobs run in it, with ``check_jobs``) or -1 (the record
        already names another claim or is gone)."""
        result = int(
            cast(
                int,
                await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
                    _DESTROY_IF_IDLE,
                    3,
                    ws_key(m.group_id, m.name),
                    keep_key(m.group_id, m.name),
                    index_key(m.group_id),
                    m.claim,
                    str(max_active),
                    "1" if mark_recreate else "0",
                    "1" if check_jobs else "0",
                    "1" if keep_record else "0",
                    str(_PENDING_TTL),
                    m.name,
                ),
            )
        )
        if result == 1:
            self._audit(m.group_id, "destroy", workspace=m.name, claim=m.claim, reason=reason, kept=keep_record)
            if not m.pending:
                await self._kube().delete(self._claim_path(m.claim))
        return result

    async def _retire(self, m: Mapping, *, reason: str) -> None:
        """Make the record pending (a fresh pod on next use) whatever uses its current pod; the pod itself is left
        alone (gone already, or expiring on its own under the invocations still using it)."""
        await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
            _DESTROY_IF_IDLE,
            3,
            ws_key(m.group_id, m.name),
            keep_key(m.group_id, m.name),
            index_key(m.group_id),
            m.claim,
            _RETIRE_ACTIVE,
            "0",
            "0",
            "1",
            str(_PENDING_TTL),
            m.name,
        )
        self._audit(m.group_id, "retire", workspace=m.name, claim=m.claim, reason=reason)

    async def _replace(self, m: Mapping, *, reason: str) -> None:
        """Delete a broken pod and make the record pending."""
        try:
            await self._kube().delete(self._claim_path(m.claim))
        finally:
            await self._retire(m, reason=reason)

    async def _renew(self, m: Mapping, cfg: ToolSettings) -> bool:
        """Push the claim's shutdownTime out; False when the claim is gone."""
        now = time.time()
        expires = shutdown_at(now=now, max_until=m.max_until, idle_s=cfg.sandbox_idle_min * 60, has_links=bool(m.links))
        patched = await self._kube().merge_patch(
            self._claim_path(m.claim), {"spec": {"lifecycle": {"shutdownTime": _rfc3339(expires)}}}
        )
        if patched is None:
            return False
        m.expires_at, m.last_used = expires, now
        await self._save(m, "expires_at", "last_used")
        return True

    async def _healthy(self, http: httpx.AsyncClient, ip: str) -> bool:
        try:
            return (await http.get(f"{self._endpoint(ip).rest_url}/v1/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def _wait_ready(self, name: str, deadline: float) -> tuple[str, str | None]:
        """Wait until the claim is Ready with a pod IP and sandboxd answers /v1/health; (pod ip, sandbox name).
        Raises ``_ClaimLost`` when the claim disappears or its pod fails for good."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as http:
            while True:
                claim = await self._kube().get(self._claim_path(name))
                if claim is None:
                    raise _ClaimLost("工作区被回收了，稍后再试。", reason="claim gone")
                ready, ip, sandbox, failure = _claim_ready(claim)
                if failure:
                    raise _ClaimLost(f"工作区启动失败（{failure}），稍后再试。", reason=f"pod failed ({failure})")
                if ready and ip and await self._healthy(http, ip):
                    return ip, sandbox
                if time.monotonic() > deadline:
                    raise SandboxToolError(f"工作区没能在 {READY_TIMEOUT:.0f} 秒内就绪，稍后再试。")
                await asyncio.sleep(0.5)

    async def _evict_for(self, m: Mapping, cfg: ToolSettings) -> None:
        """Make room under the deployment-wide cap of pods for ``m``'s new one: evict the least recently used
        idle workspaces of any group (their records go too)."""
        others = [
            x for x in await self._all_mappings() if not x.pending and (x.group_id, x.name) != (m.group_id, m.name)
        ]
        excess = len(others) - cfg.sandbox_max_workspaces + 1
        # `active` in this snapshot may be stale: the destroy re-checks it atomically and skips a workspace an
        # invocation attached to meanwhile.
        for x in sorted((x for x in others if x.active <= 0), key=lambda x: x.last_used):
            if excess <= 0:
                break
            reason = f"evicted (LRU) for group {m.group_id} workspace {m.name}"
            if await self._destroy_if_idle(x, reason=reason, check_jobs=True) == 1:
                excess -= 1
        if excess > 0:
            raise SandboxToolError(
                f"同时存在的工作区已达上限（{cfg.sandbox_max_workspaces} 个），而且都在使用中或有后台任务，稍后再试。"
            )

    async def _create(self, m: Mapping, cfg: ToolSettings) -> Mapping:
        """Create the pod of pending record ``m`` (under its workspace lock) and record it, already counting the
        caller in ``active`` (atomically with the claim, so no eviction sees it unused in between)."""
        await self._evict_for(m, cfg)
        key = ws_key(m.group_id, m.name)
        now = time.time()
        if not int(cast(int, await self._r().eval(_TOUCH_PENDING, 1, key, str(_PENDING_TTL), repr(now)))):  # pyright: ignore[reportGeneralTypeIssues]
            raise WorkspaceMissing(f"工作区「{m.name}」已经不存在了。")
        claim = claim_name(m.group_id, m.name)
        max_until = now + cfg.sandbox_max_age_min * 60
        expires = shutdown_at(now=now, max_until=max_until, idle_s=cfg.sandbox_idle_min * 60, has_links=False)
        labels = {**_LABELS, _GROUP_LABEL: str(m.group_id), _NAME_LABEL: m.name}
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {"name": claim, "labels": labels},
            "spec": {
                "warmPoolRef": {"name": self._warmpool},
                "lifecycle": {"shutdownTime": _rfc3339(expires), "shutdownPolicy": "Delete"},
                "additionalPodMetadata": {"labels": {_WORKSPACE_LABEL: claim}},
            },
        }
        started = time.monotonic()
        try:
            await self._kube().create(self._claim_path(), body)
        except KubeError as e:
            LOGGER.error("creating sandbox claim for group {} workspace {} failed: {}", m.group_id, m.name, e)
            raise SandboxToolError("创建工作区失败（集群拒绝了请求），稍后再试。") from e
        try:
            ip, sandbox = await self._wait_ready(claim, started + READY_TIMEOUT)
            new = Mapping(
                group_id=m.group_id,
                name=m.name,
                description=m.description,
                claim=claim,
                sandbox=sandbox,
                pod_ip=ip,
                created_at=now,
                last_used=now,
                expires_at=expires,
                max_until=max_until,
                active=1,
            )
            data = new.to_hash()
            flat = [x for k, v in data.items() if k not in ("name", "description") for x in (k, v)]
            if not int(cast(int, await self._r().eval(_SET_CLAIM, 2, key, keep_key(m.group_id, m.name), *flat))):  # pyright: ignore[reportGeneralTypeIssues]
                raise WorkspaceMissing(f"工作区「{m.name}」在创建过程中被删除了。")
        except BaseException:
            try:
                await asyncio.shield(self._kube().delete(self._claim_path(claim)))
            except Exception:
                LOGGER.exception("deleting failed sandbox claim {} failed", claim)
            raise
        self._audit(
            m.group_id, "create", workspace=m.name, claim=claim, pod_ip=ip, ready_s=round(time.monotonic() - started, 2)
        )
        return new

    async def _usable(self, m: Mapping, cfg: ToolSettings) -> tuple[Mapping | None, str | None]:
        """Check a record with a pod (under its workspace lock): (record to use, notice or None), or (None,
        notice) after making it pending because its pod must not be used any more."""
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._retire(m, reason="claim gone")
            return None, _GONE_NOTICE
        ready, ip, sandbox, failure = _claim_ready(claim)
        if failure:
            # A failed pod (e.g. evicted for filling /workspace) never comes back: replace it now rather than
            # failing every invocation until the claim expires.
            await self._replace(m, reason=f"pod failed ({failure})")
            return None, _GONE_NOTICE
        if m.recreate and (
            await self._destroy_if_idle(m, reason="pending recreate", check_jobs=True, keep_record=True) == 1
        ):
            return None, _RESET_NOTICE
        if m.max_until - time.time() < cfg.sandbox_invocation_s:
            # Too close to its hard cap for a whole invocation. If others still use it, only retire the record:
            # their claim's shutdownTime is already capped, so the pod expires on its own.
            if await self._destroy_if_idle(m, reason="max age", keep_record=True) != 1:
                await self._retire(m, reason="max age (still in use, expires on its own)")
            return None, _AGED_NOTICE
        if not ready:
            try:
                ip, sandbox = await self._wait_ready(m.claim, time.monotonic() + READY_TIMEOUT)
            except _ClaimLost as e:
                await self._replace(m, reason=e.reason)
                return None, _GONE_NOTICE
        notice = None
        if ip and ip != m.pod_ip:
            if m.pod_ip:
                notice = _RESTARTED_NOTICE
            m.pod_ip = ip
            m.sandbox = sandbox or m.sandbox
            await self._save(m, "pod_ip", "sandbox")
        return m, notice

    async def _provide(self, group_id: int, name: str, cfg: ToolSettings) -> tuple[Mapping, str | None]:
        """The workspace's pod, attached or created, with ``active`` already incremented for the caller. Raises
        ``WorkspaceMissing`` when the record is gone.

        Runs under the workspace lock, so it never interleaves with another invocation's last release (reap and
        pending recreate) or with a concurrent create."""
        notice: str | None = None
        deadline = time.monotonic() + _ATTACH_LOCK_WAIT
        while True:
            async with self._ws_lock(group_id, name, wait=max(0.0, deadline - time.monotonic())) as got:
                if not got:
                    raise SandboxToolError("工作区正忙（另一个请求正在创建或收尾），等太久了，稍后再试。")
                m = await self._load(group_id, name)
                if m is None:
                    raise WorkspaceMissing(f"工作区「{name}」已经不存在了（过期、被回收或被删除），里面的文件都没了。")
                if m.pending:
                    m = await self._create(m, cfg)
                    return m, notice
                usable, why = await self._usable(m, cfg)
                if usable is None:
                    notice = why
                    continue
                if await self._incr_active(usable, 1) < 0:
                    continue  # replaced meanwhile (lock expired under a slow holder): look again
                usable.active += 1
                try:
                    alive = await self._renew(usable, cfg)
                except BaseException:
                    await asyncio.shield(self._incr_active(usable, -1))
                    raise
                if alive:
                    return usable, why or notice
                await self._incr_active(usable, -1)
                await self._retire(usable, reason="claim gone")
                notice = _GONE_NOTICE

    # --- describe -------------------------------------------------------------------------------------------

    async def _describe(self, group_id: int, name: str) -> str | None:
        m = await self._load(group_id, name)
        if m is None or m.pending:
            return None
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._drop(m, reason="claim gone (describe)")
            return None
        _, ip, _, _ = _claim_ready(claim)
        backend = SandboxdWorkspace(
            resolver=StaticResolver(self._endpoint(ip or m.pod_ip), m.claim),
            token=self.token,
            ref=WorkspaceRef(provider=PROVIDER, id=m.claim),
        )
        try:
            tree = await backend.exec(["/bin/sh", "-c", _TREE_SCRIPT, "sh", DEFAULT_ROOT], seconds=10, retry=False)
            notes = await backend.exec(
                [
                    "/bin/sh",
                    "-c",
                    f'[ -f "$1" ] && head -c {_NOTES_CHARS * 4} -- "$1"',
                    "sh",
                    f"{DEFAULT_ROOT}/NOTES.md",
                ],
                seconds=10,
                retry=False,
            )
        finally:
            await backend.aclose()
        parts: list[str] = []
        listing = format_tree(tree.stdout.decode("utf-8", "replace")) if tree.exit_code == 0 else ""
        parts.append(f"文件（{DEFAULT_ROOT}）：\n{listing}" if listing else f"文件（{DEFAULT_ROOT}）：（空）")
        if notes.exit_code == 0 and notes.stdout.strip():
            text = notes.stdout.decode("utf-8", "replace")[:_NOTES_CHARS]
            parts.append(f"NOTES.md 开头：\n{text.rstrip()}")
        return "\n\n".join(parts)

    # --- links ----------------------------------------------------------------------------------------------

    def _owner(self, sandbox: dict[str, Any]) -> list[dict[str, Any]]:
        meta = sandbox.get("metadata") or {}
        return [
            {
                "apiVersion": "agents.x-k8s.io/v1beta1",
                "kind": "Sandbox",
                "name": meta.get("name"),
                "uid": meta.get("uid"),
                "blockOwnerDeletion": False,
            }
        ]

    async def _create_link_objects(self, m: Mapping, rid: str, port: int, domain: str) -> None:
        sandbox_name = m.sandbox
        if not sandbox_name:
            claim = await self._kube().get(self._claim_path(m.claim))
            sandbox_name = _claim_ready(claim)[2] if claim else None
        sandbox = await self._kube().get(self._sandbox_path(sandbox_name)) if sandbox_name else None
        if sandbox is None:
            raise SandboxError("找不到工作区对应的沙箱对象，没法建立公开链接。")
        owners = self._owner(sandbox)
        name = link_name(rid)
        labels = {**_LABELS, _GROUP_LABEL: str(m.group_id), _NAME_LABEL: m.name}
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": labels, "ownerReferences": owners},
            "spec": {
                "selector": {_WORKSPACE_LABEL: m.claim},
                "ports": [{"name": "http", "port": 80, "targetPort": port, "protocol": "TCP"}],
            },
        }
        route = {
            "apiVersion": "traefik.io/v1alpha1",
            "kind": "IngressRoute",
            "metadata": {"name": name, "labels": labels, "ownerReferences": owners},
            "spec": {
                "entryPoints": ["websecure"],
                "routes": [
                    {"kind": "Rule", "match": f"Host(`{name}.{domain}`)", "services": [{"name": name, "port": 80}]}
                ],
            },
        }
        await self._kube().create(self._service_path(), service)
        try:
            await self._kube().create(self._route_path(), route)
        except BaseException:
            await asyncio.shield(self._delete_link_objects(rid))
            raise

    async def _delete_link_objects(self, rid: str) -> None:
        name = link_name(rid)
        for path in (self._route_path(name), self._service_path(name)):
            try:
                await self._kube().delete(path)
            except Exception:
                LOGGER.exception("deleting {} failed", path)

    @staticmethod
    def _audit(group_id: int | None, action: str, **fields: Any) -> None:
        LOGGER.bind(audit="sandbox", action=action, group=group_id, **fields).info(
            "sandbox {} group={} {}", action, group_id, fields
        )


class Session:
    """One invocation's use of its group's workspaces: the selected one (``open``), attached lazily on first use.
    ``backend`` goes to ``agent.run(workspace=...)``; switching workspaces re-points it."""

    def __init__(
        self,
        mgr: WorkspaceManager,
        group_id: int,
        *,
        invocation_id: str | None,
        cfg: ToolSettings,
        workspace: str | None = None,
    ) -> None:
        self._mgr = mgr
        self._group_id = group_id
        self._invocation_id = invocation_id
        self._cfg = cfg
        self._name = workspace
        self._mapping: Mapping | None = None
        self._counted = False
        self._renewed_at = 0.0  # monotonic time of this session's last claim renewal
        self._reserved: set[str] = set()  # created by this invocation and not used yet
        self.backend = SandboxdWorkspace(
            resolver=self,
            token=mgr.token,
            limits=CommandLimits.from_settings(cfg),
            audit={"group": group_id, "invocation": invocation_id},
        )

    @property
    def name(self) -> str | None:
        """The selected workspace."""
        return self._name

    @property
    def used(self) -> bool:
        """Whether this invocation is attached to (and counted in) the selected workspace."""
        return self._counted

    # --- selecting --------------------------------------------------------------------------------------------

    async def listing(self) -> str:
        """The group's workspaces and how to choose, for ``open_workspace()`` without a name."""
        text = await self._mgr.listing(self._group_id, current=self._name)
        if not text:
            return "本群还没有工作区。" + _CREATE_HINT
        cap = self._cfg.sandbox_max_workspaces_per_group
        return (
            f"本群的工作区（每群最多 {cap} 个）：\n{text}\n{_CHOOSE_HINT}"
            "满了再新建时，会回收最久没用、没有后台任务的那个。"
        )

    async def require(self) -> str:
        """The selected workspace's name; ``SandboxToolError`` listing the group's workspaces when none is."""
        if self._name is None:
            raise SandboxToolError(await self._unselected())
        return self._name

    async def _unselected(self) -> str:
        try:
            text = await self._mgr.listing(self._group_id)
        except Exception:
            LOGGER.exception("listing workspaces failed for group {}", self._group_id)
            text = ""
        if not text:
            return "还没有选工作区，本群也还没有工作区。先" + _CREATE_HINT + "然后再调用这个工具。"
        return f"还没有选工作区：先调用 open_workspace 选一个，再调用这个工具。本群的工作区：\n{text}\n{_CHOOSE_HINT}"

    async def open(self, name: str, *, new: bool = False, description: str = "") -> Opened:
        """Select workspace ``name`` for the rest of the invocation: an existing one (``description`` updates its
        description) or, with ``new``, a new one (created on first use; the group's least recently used idle one
        is evicted when the group is at its cap). Switching releases the previous one as the invocation's end
        would (reap included)."""
        if (why := name_error(name)) is not None:
            raise SandboxError(why)
        description = clean_description(description)
        evicted: list[WorkspaceInfo] = []
        updated = False
        if new:
            mine = (self._mapping.name, self._mapping.claim) if self._counted and self._mapping is not None else None
            evicted = await self._mgr.reserve(self._group_id, name, description, self._cfg, mine=mine)
            self._reserved.add(name)
            if mine is not None and any((w.name, w.claim) == mine for w in evicted):
                # This invocation's own workspace made room: there is nothing left to release or reap.
                self._counted, self._mapping = False, None
                await self.backend.detach()
        else:
            info = await self._mgr.get(self._group_id, name)
            if info is None:
                raise SandboxError(f"本群没有叫「{name}」的工作区（可能已经过期回收了）。\n{await self.listing()}")
            if description and description != info.description:
                updated = await self._mgr.set_description(self._group_id, name, description)
        left = await self._switch(name)
        if left is not None and any(w.name == left for w in evicted):
            left = None  # it made room for the new one; nothing of it is left
        info = await self._mgr.get(self._group_id, name)
        if info is None:
            self._name = None
            raise SandboxError(f"工作区「{name}」刚被回收了。\n{await self.listing()}")
        return Opened(info=info, created=new, evicted=tuple(evicted), left=left, updated=updated)

    async def summary(self, opened: Opened, *, files: bool = True) -> str:
        """What ``open`` did, for the model: the switch, evictions (their links die with them), and the selected
        workspace's state, plus (``files``) its file listing and the head of NOTES.md."""
        w = opened.info
        lines: list[str] = []
        if opened.left:
            lines.append(
                f"已离开工作区「{opened.left}」：它里面用 & 起的临时进程已清理，文件、kernel、start() 起的服务和"
                "公开链接都还在。"
            )
        for e in opened.evicted:
            gone = f"本群工作区满了，回收了最久没用的「{e.name}」（{e.description}），里面的文件都没了"
            if e.links:
                gone += "，它的公开链接也随之失效：" + "、".join(x.url for x in e.links)
            lines.append(gone + "。")
        if opened.created:
            lines.append(
                f"已新建工作区「{w.name}」：{w.description}\n"
                "它现在是空的，第一次运行命令时启动；之后的 sh、py 等都在这个工作区里。"
            )
            return "\n".join(lines)
        updated = "，说明已更新" if opened.updated else ""
        lines += [
            f"已选中工作区「{w.name}」{updated}；之后的 sh、py 等都在这个工作区里。",
            await self._mgr.status(w, current=w.name),
        ]
        if files and (described := await self._mgr.describe(self._group_id, w.name)):
            lines.append(described)
        return "\n".join(lines)

    async def _switch(self, name: str) -> str | None:
        """Make ``name`` the selected workspace; returns the one left behind, if any."""
        previous = self._name
        if previous == name:
            return None
        await self.backend.settle()
        try:
            if self._counted and self._mapping is not None:
                await self._release(self._mapping)
        finally:
            self._counted, self._mapping = False, None
            await self.backend.detach()
            self._name = name
        return previous

    # --- WorkspaceResolver ----------------------------------------------------------------------------------

    async def acquire(self, ref: WorkspaceRef | None, on_ref: Callable[[WorkspaceRef], None]) -> Attachment:
        name = self._name
        if name is None:
            raise SandboxToolError(await self._unselected())
        if ref is not None and (self._mapping is None or ref.id != self._mapping.claim):
            raise WorkspaceUnavailableError("工作区已失效（沙箱已不存在），里面的文件都没了。")
        try:
            m, notice = await self._mgr._provide(self._group_id, name, self._cfg)
        except WorkspaceMissing as e:
            if self._name == name:
                self._name = None
            raise SandboxToolError(f"{e}\n{await self.listing()}") from e
        except (SandboxError, WorkspaceError):
            raise
        except Exception as e:
            LOGGER.exception("sandbox acquisition failed for group {} workspace {}", self._group_id, name)
            raise SandboxToolError("工作区暂时不可用（内部错误），稍后再试。") from e
        self._mapping = m
        self._counted = True
        self._reserved.discard(name)
        self._renewed_at = time.monotonic()  # _provide renewed it
        # Recorded only once the workspace is Ready: a claim that never became ready was deleted again, and the
        # next operation must create a fresh one rather than attach to it.
        on_ref(WorkspaceRef(provider=PROVIDER, id=m.claim))
        return Attachment(self._mgr._endpoint(m.pod_ip), notice)

    async def refresh(self, ref: WorkspaceRef) -> Endpoint | None:
        claim = await self._mgr._kube().get(self._mgr._claim_path(ref.id))
        if claim is None:
            if self._mapping is not None and self._mapping.claim == ref.id:
                await self._mgr._drop(self._mapping, reason="claim gone mid-invocation")
                self._counted = False
            return None
        _, ip, _, _ = _claim_ready(claim)
        if ip:
            return self._mgr._endpoint(ip)
        return self._mgr._endpoint(self._mapping.pod_ip) if self._mapping else None

    async def touch(self, ref: WorkspaceRef) -> None:
        """Renew the claim during a long run (model turns between commands count as idle time to the
        controller): at most every quarter of the idle timeout, from the freshly loaded record so live links
        keep their longer lifetime."""
        now = time.monotonic()
        if not self._counted or self._mapping is None or now - self._renewed_at < self._cfg.sandbox_idle_min * 60 / 4:
            return
        self._renewed_at = now
        m = await self._mgr._load(self._group_id, self._mapping.name)
        if m is None or m.claim != ref.id:
            return
        await self._mgr._renew(m, self._cfg)

    # --- bot tools ------------------------------------------------------------------------------------------

    def _path(self, rel_path: str) -> str:
        path = posixpath.normpath(posixpath.join(DEFAULT_ROOT, rel_path))
        if path != DEFAULT_ROOT and not path.startswith(DEFAULT_ROOT + "/"):
            raise SandboxError(f"路径必须在 {DEFAULT_ROOT} 里面：{rel_path}")
        return path

    async def put_bytes(self, rel_path: str, data: bytes) -> str:
        path = self._path(rel_path)
        async with _errors("写入"):
            await self.backend.write_bytes(path, data)
        return path

    async def get_bytes(self, rel_path: str, *, max_bytes: int) -> bytes:
        path = self._path(rel_path)
        async with _errors("读取"):
            entry = await self.backend.stat(path)
            if entry.is_dir:
                raise SandboxError(f"{path} 是目录，不是文件")
            if entry.size is not None and entry.size > max_bytes:
                raise SandboxError(f"文件太大（{_fmt_size(entry.size)}，上限 {_fmt_size(max_bytes)}）：{path}")
            data = await self.backend.read_bytes(path)
        if len(data) > max_bytes:
            raise SandboxError(f"文件太大（上限 {_fmt_size(max_bytes)}）：{path}")
        return data

    async def publish(self, port: int) -> Link:
        name = await self.require()
        audit = {"invocation": self._invocation_id}
        return await self._mgr.publish(self._group_id, name, port, self._cfg, audit=audit)

    async def unpublish(self, port: int) -> bool:
        name = await self.require()
        audit = {"invocation": self._invocation_id}
        return await self._mgr.unpublish(self._group_id, name, port, self._cfg, audit=audit)

    async def keep_kernel(self, pid: int | None) -> None:
        """Record the workspace's IPython kernel from a ``py`` result so the end-of-invocation reap spares it;
        ``None`` (the kernel died) forgets it."""
        if self._mapping is None:
            return
        key = keep_key(self._group_id, self._mapping.name)
        if pid is None:
            await self._mgr._r().hdel(key, "kernel")  # pyright: ignore[reportGeneralTypeIssues]
        else:
            await self._mgr._r().hset(key, "kernel", str(pid))  # pyright: ignore[reportGeneralTypeIssues]

    async def reset(self) -> str:
        """Empty the selected workspace: its pod is destroyed and a fresh one is created on next use, under the
        same name and description."""
        name = await self.require()
        async with self._mgr._local_locks[self._group_id, name]:
            m = await self._mgr._load(self._group_id, name)
            if m is None:
                self._name = None
                raise SandboxError(f"工作区「{name}」已经不存在了。\n{await self.listing()}")
            if m.pending:
                await self.backend.detach()
                self._counted, self._mapping = False, None
                return f"工作区「{name}」现在是空的（还没启动），不用重置。"
            mine = self._counted and self._mapping is not None and self._mapping.claim == m.claim
            reason = f"reset_workspace ({self._invocation_id})"
            destroyed = await self._mgr._destroy_if_idle(
                m, reason=reason, max_active=int(mine), mark_recreate=True, check_jobs=True, keep_record=True
            )
            if destroyed == _BUSY_JOBS:
                raise SandboxError("有后台任务在运行，先用 cancel_job 取消")
            if destroyed == 0:
                self._mgr._audit(
                    self._group_id, "reset_deferred", workspace=name, invocation=self._invocation_id, claim=m.claim
                )
                return f"有其他请求正在使用工作区「{name}」，等它们结束后会重建；这次请求里文件还在。"
            await self.backend.detach()
            self._counted, self._mapping = False, None
            return (
                f"工作区「{name}」已清空：之前的文件、装的依赖和公开链接都没了，下次用到时会新建一个空的"
                "（名字和说明不变，用完记得更新说明）。"
            )

    async def close(self) -> None:
        try:
            # An acquisition still in flight (a tool call cancelled mid-attach) may yet count this session in
            # `active`; wait for it so the count is released, not leaked.
            await self.backend.settle()
            if self._counted and self._mapping is not None:
                await self._release(self._mapping)
            for name in sorted(self._reserved):
                await self._mgr._drop_unused(self._group_id, name)
        except Exception:
            LOGGER.exception("sandbox session close failed for group {}", self._group_id)
        finally:
            self._counted = False
            self._reserved.clear()
            try:
                await self.backend.aclose()
            except Exception:
                LOGGER.exception("closing sandbox connections failed")

    async def _release(self, mine: Mapping) -> None:
        async with self._mgr._ws_lock(self._group_id, mine.name, wait=_RELEASE_LOCK_WAIT) as locked:
            remaining = await self._mgr._incr_active(mine, -1)
            if remaining < 0:
                return  # record gone or given a new pod meanwhile
            m = await self._mgr._load(self._group_id, mine.name)
            if m is None or m.claim != mine.claim:
                return
            if remaining == 0 and not locked:
                # Reaping without the lock could kill an invocation attaching right now; leave the leftovers.
                LOGGER.warning("sandbox workspace {}/{} lock busy; skipping reap on release", self._group_id, m.name)
            elif remaining == 0:
                await self._reap(m)
                if m.recreate and (
                    await self._mgr._destroy_if_idle(m, reason="pending recreate", check_jobs=True, keep_record=True)
                    == 1
                ):
                    return
            await self._mgr._renew(m, self._cfg)

    async def _reap(self, m: Mapping) -> None:
        keep = cast(dict[str, str], await self._mgr._r().hgetall(keep_key(self._group_id, m.name)))  # pyright: ignore[reportGeneralTypeIssues]
        try:
            result = await self.backend.exec(reap_argv(keep, m.links), seconds=30)
            if result.exit_code != 0:
                LOGGER.warning("sandbox reap exited {}: {}", result.exit_code, result.stderr[:300])
        except WorkspaceUnavailableError:
            pass  # the sandbox is gone; nothing left to reap
        except Exception:
            LOGGER.exception("sandbox reap failed for group {} workspace {}", self._group_id, m.name)


@asynccontextmanager
async def _errors(action: str) -> AsyncIterator[None]:
    """Turn workspace / filesystem failures in the bot's own sandbox tools into ``SandboxError`` text."""
    try:
        yield
    except SandboxError:
        raise
    except FileNotFoundError as e:
        raise SandboxError(f"{action}失败：文件不存在 {e.filename or ''}".rstrip()) from e
    except IsADirectoryError as e:
        raise SandboxError(f"{action}失败：{e.filename or ''} 是目录") from e
    except PermissionError as e:
        raise SandboxError(f"{action}失败：没有权限 {e.filename or ''}".rstrip()) from e
    except (WorkspaceError, OSError) as e:
        raise SandboxError(f"{action}失败：{e}") from e
    except KubeError as e:
        LOGGER.error("sandbox {} failed: {}", action, e)
        raise SandboxError(f"{action}失败（集群拒绝了请求）") from e


@cache
def manager() -> WorkspaceManager:
    return WorkspaceManager(
        token=APP_CONFIG.sandbox_token,
        namespace=APP_CONFIG.sandbox_namespace,
        warmpool=APP_CONFIG.sandbox_warmpool,
        link_domain=APP_CONFIG.sandbox_link_domain,
    )

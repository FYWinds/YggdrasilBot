"""Per-group workspaces: SandboxClaims from the warm pool, the Redis mapping, renewal, links, sessions."""

from __future__ import annotations

import asyncio
import posixpath
import shlex
import time
from collections import defaultdict
from contextlib import asynccontextmanager
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
    WS_KEY_PREFIX,
    Link,
    LinkRecord,
    Mapping,
    WorkspaceInfo,
    claim_name,
    link_id,
    link_name,
    links_json,
    lock_key,
    shutdown_at,
    validate_port,
    ws_key,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from redis.asyncio import Redis

    from yggdrasil.settings import ToolSettings

_LABELS = {"app.kubernetes.io/part-of": "yggdrasil", "app.kubernetes.io/component": "sandbox"}
_LABEL_SELECTOR = ",".join(f"{k}={v}" for k, v in _LABELS.items())
_GROUP_LABEL = "yggdrasil/group"
_WORKSPACE_LABEL = "sandbox.users.io/workspace"

READY_TIMEOUT = 60.0
_LOCK_TTL = 150
"""Seconds the per-group Redis lock lives; covers the longest holder (creating a workspace, ~READY_TIMEOUT)."""
_ATTACH_LOCK_WAIT = 120.0
"""How long attaching waits for the group lock (another invocation creating or releasing the workspace)."""
_RELEASE_LOCK_WAIT = 20.0
"""How long the last release waits for the lock before it gives up reaping (it still decrements)."""
_LOCK_POLL = 0.2
_SERVE_WAIT_STEPS = 60  # x 0.5s
_REAP = "/opt/ygg/reap"
_SERVE_DIR = f"{DEFAULT_ROOT}/.ygg/serve"

_GONE_NOTICE = "[工作区] 之前的工作区已过期或出故障被回收，里面的文件都没了；这是新建的空工作区。"
_AGED_NOTICE = "[工作区] 之前的工作区到了最长存活时间，已回收，里面的文件都没了；这是新建的空工作区。"
_RESET_NOTICE = "[工作区] 工作区已按要求重置；这是新建的空工作区。"
_RESTARTED_NOTICE = "[工作区] 工作区的沙箱重启过，之前的文件都没了。"

# Remove the mapping only if it still names this claim (a newer workspace may have replaced it).
_DROP_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
)
# Change `active` only while the mapping still names this claim; returns the new value or -1.
_INCR_IF_CLAIM = (
    "if redis.call('HGET', KEYS[1], 'claim') == ARGV[1] then "
    "return redis.call('HINCRBY', KEYS[1], 'active', ARGV[2]) end return -1"
)
_UNLOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
# Destroy the mapping only if it still names this claim and at most ARGV[2] invocations use it: 1 = deleted,
# 0 = busy (and `recreate` marked when ARGV[3] is 1), -1 = the mapping names another claim or is gone.
_DESTROY_IF_IDLE = (
    "if redis.call('HGET', KEYS[1], 'claim') ~= ARGV[1] then return -1 end "
    "if tonumber(redis.call('HGET', KEYS[1], 'active') or '0') <= tonumber(ARGV[2]) then "
    "redis.call('DEL', KEYS[1]) return 1 end "
    "if ARGV[3] == '1' then redis.call('HSET', KEYS[1], 'recreate', '1') end return 0"
)

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


class WorkspaceManager:
    """Process-wide owner of the group -> workspace mapping. Build with ``manager()``."""

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
        self._group_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    def session(self, group_id: int, *, invocation_id: str | None, cfg: ToolSettings) -> Session:
        """A lazy per-invocation handle (no I/O until the model first uses the workspace)."""
        if not self.configured:
            raise SandboxNotConfigured("工作区未配置（没有设置 YGG_SANDBOX_TOKEN）")
        return Session(self, group_id, invocation_id=invocation_id, cfg=cfg)

    async def get(self, group_id: int) -> WorkspaceInfo | None:
        m = await self._load(group_id)
        if m is None:
            return None
        if await self._kube().get(self._claim_path(m.claim)) is None:
            await self._drop(m, reason="claim gone")
            return None
        return m.info()

    async def list(self) -> list[WorkspaceInfo]:
        mappings = await self._all_mappings()
        live = await self._live_claims()
        return [m.info() for m in mappings if m.claim in live]

    async def describe(self, group_id: int) -> str | None:
        try:
            return await self._describe(group_id)
        except Exception:
            LOGGER.exception("sandbox describe failed for group {}", group_id)
            return None

    async def destroy(self, group_id: int, *, reason: str, force: bool = False) -> bool:
        m = await self._load(group_id)
        if m is None:
            return False
        if force:
            await self._destroy_mapping(m, reason=reason)
            return True
        if await self._destroy_if_idle(m, reason=reason, mark_recreate=True) == 1:
            return True
        self._audit(group_id, "destroy_deferred", claim=m.claim, reason=reason)
        return False

    async def destroy_all(self, *, reason: str) -> int:
        mappings = await self._all_mappings()
        claims = {m.claim for m in mappings}
        try:
            claims |= await self._live_claims()
        except KubeError:
            LOGGER.exception("listing sandbox claims failed")
        destroyed = 0
        for name in claims:
            try:
                if await self._kube().delete(self._claim_path(name)):
                    destroyed += 1
            except Exception:
                LOGGER.exception("deleting sandbox claim {} failed", name)
        for m in mappings:
            await self._r().delete(ws_key(m.group_id))
        self._audit(None, "destroy_all", reason=reason, destroyed=destroyed)
        return destroyed

    async def reconcile(self) -> None:
        try:
            mappings = await self._all_mappings()
            if not mappings:
                return
            live = await self._live_claims()
            for m in mappings:
                if m.claim not in live:
                    await self._drop(m, reason="claim gone (reconcile)")
        except Exception:
            LOGGER.exception("sandbox reconcile failed")

    # --- redis ----------------------------------------------------------------------------------------------

    def _r(self) -> Redis:
        return self._redis()

    async def _load(self, group_id: int) -> Mapping | None:
        data = cast(dict[str, str], await self._r().hgetall(ws_key(group_id)))  # pyright: ignore[reportGeneralTypeIssues]
        return Mapping.from_hash(group_id, data) if data else None

    async def _all_mappings(self) -> list[Mapping]:
        out: list[Mapping] = []
        async for key in self._r().scan_iter(match=f"{WS_KEY_PREFIX}*", count=100):
            try:
                group_id = int(str(key).removeprefix(WS_KEY_PREFIX))
            except ValueError:
                continue
            if (m := await self._load(group_id)) is not None:
                out.append(m)
        return out

    async def _save(self, m: Mapping, *fields: str) -> None:
        """Write ``fields`` (default: all but ``active``, which only HINCRBY touches)."""
        data = m.to_hash()
        keys = fields or tuple(k for k in data if k != "active")
        await self._r().hset(ws_key(m.group_id), mapping={k: data[k] for k in keys})  # pyright: ignore[reportGeneralTypeIssues]

    async def _incr_active(self, m: Mapping, delta: int) -> int:
        value = await self._r().eval(_INCR_IF_CLAIM, 1, ws_key(m.group_id), m.claim, str(delta))  # pyright: ignore[reportGeneralTypeIssues]
        return int(cast(int, value))

    async def _drop(self, m: Mapping, *, reason: str) -> None:
        await self._r().eval(_DROP_IF_CLAIM, 1, ws_key(m.group_id), m.claim)  # pyright: ignore[reportGeneralTypeIssues]
        self._audit(m.group_id, "drop_mapping", claim=m.claim, reason=reason)

    @asynccontextmanager
    async def _group_lock(self, group_id: int, *, wait: float) -> AsyncIterator[bool]:
        """Hold ``ygg:sandbox:lock:<gid>`` (shared by all bot replicas), polling up to ``wait`` seconds; yields
        whether it was acquired. Attaching and the last release run under it, so a release never reaps or
        recreates a workspace another invocation is just attaching to."""
        token = link_id()
        deadline = time.monotonic() + wait
        while not (got := bool(await self._r().set(lock_key(group_id), token, nx=True, ex=_LOCK_TTL))):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_LOCK_POLL)
        try:
            yield got
        finally:
            if got:
                try:
                    await asyncio.shield(self._r().eval(_UNLOCK, 1, lock_key(group_id), token))  # pyright: ignore[reportGeneralTypeIssues, reportArgumentType]
                except Exception:
                    LOGGER.exception("releasing sandbox lock for group {} failed", group_id)

    # --- kubernetes -----------------------------------------------------------------------------------------

    async def _live_claims(self) -> set[str]:
        items = await self._kube().list(self._claim_path(), label_selector=_LABEL_SELECTOR)
        return {str(i.get("metadata", {}).get("name")) for i in items}

    async def _destroy_mapping(self, m: Mapping, *, reason: str) -> None:
        try:
            await self._kube().delete(self._claim_path(m.claim))
        finally:
            await self._drop(m, reason=reason)
        self._audit(m.group_id, "destroy", claim=m.claim, reason=reason)

    async def _destroy_if_idle(
        self, m: Mapping, *, reason: str, max_active: int = 0, mark_recreate: bool = False
    ) -> int:
        """Atomically drop the mapping if it still names ``m.claim`` and at most ``max_active`` invocations use
        it, then delete the claim. Returns 1 (destroyed), 0 (in use; ``recreate`` marked if asked) or -1 (the
        mapping already names another claim or is gone)."""
        result = int(
            cast(
                int,
                await self._r().eval(  # pyright: ignore[reportGeneralTypeIssues]
                    _DESTROY_IF_IDLE, 1, ws_key(m.group_id), m.claim, str(max_active), "1" if mark_recreate else "0"
                ),
            )
        )
        if result == 1:
            self._audit(m.group_id, "destroy", claim=m.claim, reason=reason)
            await self._kube().delete(self._claim_path(m.claim))
        return result

    async def _renew(self, m: Mapping, cfg: ToolSettings) -> bool:
        """Push the claim's shutdownTime out; False when the claim is gone."""
        now = time.time()
        expires = shutdown_at(
            now=now,
            created_at=m.created_at,
            idle_s=cfg.sandbox_idle_min * 60,
            max_age_s=cfg.sandbox_max_age_min * 60,
            has_links=bool(m.links),
        )
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

    async def _evict_for(self, group_id: int, cfg: ToolSettings) -> None:
        others = [m for m in await self._all_mappings() if m.group_id != group_id]
        excess = len(others) - cfg.sandbox_max_workspaces + 1
        # `active` in this snapshot may be stale: the destroy re-checks it atomically and skips a workspace an
        # invocation attached to meanwhile.
        for m in sorted((m for m in others if m.active <= 0), key=lambda m: m.last_used):
            if excess <= 0:
                break
            if await self._destroy_if_idle(m, reason=f"evicted (LRU) for group {group_id}") == 1:
                excess -= 1
        if excess > 0:
            raise SandboxToolError(
                f"同时存在的工作区已达上限（{cfg.sandbox_max_workspaces} 个），而且都在使用中，稍后再试。"
            )

    async def _create(self, group_id: int, cfg: ToolSettings) -> Mapping:
        await self._evict_for(group_id, cfg)
        name = claim_name(group_id)
        now = time.time()
        expires = shutdown_at(
            now=now,
            created_at=now,
            idle_s=cfg.sandbox_idle_min * 60,
            max_age_s=cfg.sandbox_max_age_min * 60,
            has_links=False,
        )
        body = {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
            "kind": "SandboxClaim",
            "metadata": {"name": name, "labels": {**_LABELS, _GROUP_LABEL: str(group_id)}},
            "spec": {
                "warmPoolRef": {"name": self._warmpool},
                "lifecycle": {"shutdownTime": _rfc3339(expires), "shutdownPolicy": "Delete"},
                "additionalPodMetadata": {"labels": {_WORKSPACE_LABEL: name}},
            },
        }
        started = time.monotonic()
        try:
            await self._kube().create(self._claim_path(), body)
        except KubeError as e:
            LOGGER.error("creating sandbox claim for group {} failed: {}", group_id, e)
            raise SandboxToolError("创建工作区失败（集群拒绝了请求），稍后再试。") from e
        try:
            ip, sandbox = await self._wait_ready(name, started + READY_TIMEOUT)
        except BaseException:
            try:
                await asyncio.shield(self._kube().delete(self._claim_path(name)))
            except Exception:
                LOGGER.exception("deleting failed sandbox claim {} failed", name)
            raise
        m = Mapping(
            group_id=group_id,
            claim=name,
            sandbox=sandbox,
            pod_ip=ip,
            created_at=now,
            last_used=now,
            expires_at=expires,
            active=0,
        )
        await self._r().delete(ws_key(group_id))
        await self._r().hset(ws_key(group_id), mapping=m.to_hash())  # pyright: ignore[reportGeneralTypeIssues, reportArgumentType]
        self._audit(group_id, "create", claim=name, pod_ip=ip, ready_s=round(time.monotonic() - started, 2))
        return m

    async def _usable(self, m: Mapping, cfg: ToolSettings) -> tuple[Mapping | None, str | None]:
        """Check an existing mapping (under the group lock): (mapping to use, notice or None), or (None, notice)
        after dropping or destroying a workspace that must not be used any more."""
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._drop(m, reason="claim gone")
            return None, _GONE_NOTICE
        ready, ip, sandbox, failure = _claim_ready(claim)
        if failure:
            # A failed pod (e.g. evicted for filling /workspace) never comes back: replace it now rather than
            # failing every invocation until the claim expires.
            await self._destroy_mapping(m, reason=f"pod failed ({failure})")
            return None, _GONE_NOTICE
        if m.recreate and await self._destroy_if_idle(m, reason="pending recreate") == 1:
            return None, _RESET_NOTICE
        if m.created_at + cfg.sandbox_max_age_min * 60 - time.time() < cfg.sandbox_invocation_s:
            # Too close to its hard max age for a whole invocation. If others still use it, only retire the
            # mapping: their claim's shutdownTime is already capped at the max age, so it expires on its own.
            if await self._destroy_if_idle(m, reason="max age") != 1:
                await self._drop(m, reason="max age (still in use, expires on its own)")
            return None, _AGED_NOTICE
        if not ready:
            try:
                ip, sandbox = await self._wait_ready(m.claim, time.monotonic() + READY_TIMEOUT)
            except _ClaimLost as e:
                await self._destroy_mapping(m, reason=e.reason)
                return None, _GONE_NOTICE
        notice = None
        if ip and ip != m.pod_ip:
            if m.pod_ip:
                notice = _RESTARTED_NOTICE
            m.pod_ip = ip
            m.sandbox = sandbox or m.sandbox
            await self._save(m, "pod_ip", "sandbox")
        return m, notice

    async def _provide(self, group_id: int, cfg: ToolSettings) -> tuple[Mapping, str | None]:
        """The group's workspace, attached or created, with ``active`` already incremented for the caller.

        Runs under the group lock, so it never interleaves with another invocation's last release (reap and
        pending recreate) or with a concurrent create."""
        notice: str | None = None
        deadline = time.monotonic() + _ATTACH_LOCK_WAIT
        while True:
            async with self._group_lock(group_id, wait=max(0.0, deadline - time.monotonic())) as got:
                if not got:
                    raise SandboxToolError("工作区正忙（另一个请求正在创建或收尾），等太久了，稍后再试。")
                m = await self._load(group_id)
                if m is not None:
                    usable, why = await self._usable(m, cfg)
                    if usable is None:
                        notice = why
                    else:
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
                        await self._drop(usable, reason="claim gone")
                        notice = _GONE_NOTICE
                        continue
                m = await self._create(group_id, cfg)
                await self._incr_active(m, 1)
                m.active = 1
                return m, notice

    # --- describe -------------------------------------------------------------------------------------------

    async def _describe(self, group_id: int) -> str | None:
        m = await self._load(group_id)
        if m is None:
            return None
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._drop(m, reason="claim gone (describe)")
            return "之前的工作区已过期回收，里面的文件都没了；下次用到工作区会新建一个空的。"
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
        now = time.time()
        if m.links:
            minutes = max(0, int((m.expires_at - now) / 60))
            lines = [f"{x.url}（端口 {x.port}，约 {minutes} 分钟后随工作区回收）" for x in m.links]
            parts.append("公开链接：\n" + "\n".join(lines))
        remaining = max(0, int((m.expires_at - now) / 60))
        parts.append(f"工作区闲置约 {remaining} 分钟后回收（每次使用会续期）。")
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
        labels = {**_LABELS, _GROUP_LABEL: str(m.group_id)}
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
    """One invocation's use of its group's workspace. ``backend`` goes to ``agent.run(workspace=...)``."""

    def __init__(self, mgr: WorkspaceManager, group_id: int, *, invocation_id: str | None, cfg: ToolSettings) -> None:
        self._mgr = mgr
        self._group_id = group_id
        self._invocation_id = invocation_id
        self._cfg = cfg
        self._mapping: Mapping | None = None
        self._counted = False
        self._renewed_at = 0.0  # monotonic time of this session's last claim renewal
        self.backend = SandboxdWorkspace(
            resolver=self,
            token=mgr.token,
            limits=CommandLimits.from_settings(cfg),
            audit={"group": group_id, "invocation": invocation_id},
        )

    @property
    def used(self) -> bool:
        return self._counted

    # --- WorkspaceResolver ----------------------------------------------------------------------------------

    async def acquire(self, ref: WorkspaceRef | None, on_ref: Callable[[WorkspaceRef], None]) -> Attachment:
        if ref is not None and (self._mapping is None or ref.id != self._mapping.claim):
            raise WorkspaceUnavailableError("工作区已失效（沙箱已不存在），里面的文件都没了。")
        try:
            m, notice = await self._mgr._provide(self._group_id, self._cfg)
        except (SandboxError, WorkspaceError):
            raise
        except Exception as e:
            LOGGER.exception("sandbox acquisition failed for group {}", self._group_id)
            raise SandboxToolError("工作区暂时不可用（内部错误），稍后再试。") from e
        self._mapping = m
        self._counted = True
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
        controller): at most every quarter of the idle timeout, from the freshly loaded mapping so live links
        keep their longer lifetime."""
        now = time.monotonic()
        if not self._counted or now - self._renewed_at < self._cfg.sandbox_idle_min * 60 / 4:
            return
        self._renewed_at = now
        m = await self._mgr._load(self._group_id)
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

    async def _current(self) -> Mapping:
        await self.backend.working_dir()  # attach / create
        m = await self._mgr._load(self._group_id)
        if m is None or self._mapping is None or m.claim != self._mapping.claim:
            raise SandboxError("工作区已经被重置或回收，这次请求里没法再操作它。")
        return m

    async def serve(self, command: str, port: int) -> Link:
        domain = self._mgr._link_domain
        if not domain:
            raise SandboxError("没有配置公开链接域名，不能公开服务")
        if (reason := validate_port(port)) is not None:
            raise SandboxError(reason)
        if not command.strip():
            raise SandboxError("命令不能为空")
        async with self._mgr._group_locks[self._group_id]:
            async with _errors("暴露服务"):
                m = await self._current()
                if existing := next((x for x in m.links if x.port == port), None):
                    raise SandboxError(f"端口 {port} 已经公开了：{existing.url}；要换命令先 unserve 它")
                if len(m.links) >= self._cfg.sandbox_max_links:
                    raise SandboxError(f"每个工作区最多 {self._cfg.sandbox_max_links} 个公开链接，先 unserve 一个")
                log = f"{_SERVE_DIR}/{port}.log"
                # Only `setsid` is backgrounded (not an `a && b &` list, which bash runs in a subshell that keeps
                # the command's stdout open); its pid is the new session's and process group's id.
                launch = (
                    f"mkdir -p {_SERVE_DIR} || exit 1\n"
                    f"setsid sh -c {shlex.quote(command)} >{log} 2>&1 </dev/null & echo $!"
                )
                started = await self.backend.run(launch, shell=True, timeout=20)
                pid = started.stdout.strip()
                if started.exit_code != 0 or not pid.isdigit():
                    raise SandboxError(f"服务没启动起来：{(started.stderr or started.stdout).strip()[:500]}")
                pgid = int(pid)
                await self._wait_listening(m.pod_ip, port, pgid, log)
                rid = link_id()
                try:
                    await self._mgr._create_link_objects(m, rid, port, domain)
                except BaseException:
                    await asyncio.shield(self._kill_group(pgid))
                    raise
                link = LinkRecord(
                    id=rid,
                    port=port,
                    url=f"https://{link_name(rid)}.{domain}",
                    command=command,
                    created_at=time.time(),
                    pgid=pgid,
                )
                m.links.append(link)
                await self._mgr._r().hset(ws_key(m.group_id), "links", links_json(m.links))  # pyright: ignore[reportGeneralTypeIssues]
                await self._mgr._renew(m, self._cfg)
        self._mgr._audit(
            self._group_id,
            "serve",
            invocation=self._invocation_id,
            claim=m.claim,
            port=port,
            url=link.url,
            pgid=pgid,
            command=command[:500],
        )
        return link.public()

    async def _wait_listening(self, ip: str, port: int, pgid: int, log: str) -> None:
        script = (
            f"i=0; while [ $i -lt {_SERVE_WAIT_STEPS} ]; do "
            '(exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exit 0; '
            'kill -0 -- "-$3" 2>/dev/null || exit 3; sleep 0.5; i=$((i+1)); done; exit 4'
        )
        result = await self.backend.exec(
            ["/bin/bash", "--norc", "--noprofile", "-c", script, "bash", ip, str(port), str(pgid)],
            seconds=_SERVE_WAIT_STEPS * 0.5 + 15,
        )
        if result.exit_code == 0:
            return
        tail = await self.backend.exec(["tail", "-c", "2000", log], seconds=10)
        await self._kill_group(pgid)
        why = "进程已经退出" if result.exit_code == 3 else f"{_SERVE_WAIT_STEPS // 2} 秒内没有监听"
        output = tail.stdout.decode("utf-8", "replace").strip() or "（没有输出）"
        raise SandboxError(f"服务没起来（{why}）。要监听 0.0.0.0:{port}。日志末尾：\n{output}")

    async def _kill_group(self, pgid: int) -> None:
        try:
            await self.backend.exec(
                [
                    "/bin/sh",
                    "-c",
                    # dash's kill takes no `--`; a negative operand after the signal names the process group.
                    'kill -TERM "-$1" 2>/dev/null; sleep 1; kill -KILL "-$1" 2>/dev/null; true',
                    "sh",
                    str(pgid),
                ],
                seconds=15,
            )
        except Exception:
            LOGGER.exception("killing served process group {} failed", pgid)

    async def unserve(self, port: int) -> bool:
        async with self._mgr._group_locks[self._group_id]:
            async with _errors("撤销链接"):
                m = await self._mgr._load(self._group_id)
                link = next((x for x in m.links if x.port == port), None) if m else None
                if m is None or link is None:
                    return False
                await self._mgr._delete_link_objects(link.id)
                m.links = [x for x in m.links if x.port != port]
                await self._mgr._r().hset(ws_key(m.group_id), "links", links_json(m.links))  # pyright: ignore[reportGeneralTypeIssues]
                if link.pgid is not None:
                    await self.backend.working_dir()
                    await self._kill_group(link.pgid)
                await self._mgr._renew(m, self._cfg)
        self._mgr._audit(self._group_id, "unserve", invocation=self._invocation_id, claim=m.claim, port=port)
        return True

    async def reset(self) -> str:
        async with self._mgr._group_locks[self._group_id]:
            m = await self._mgr._load(self._group_id)
            if m is None:
                await self.backend.detach()
                self._counted = False
                return "当前没有工作区，不用重置；下次用到时会新建一个空工作区。"
            mine = self._counted and self._mapping is not None and self._mapping.claim == m.claim
            reason = f"reset_workspace ({self._invocation_id})"
            if await self._mgr._destroy_if_idle(m, reason=reason, max_active=int(mine), mark_recreate=True) == 0:
                self._mgr._audit(self._group_id, "reset_deferred", invocation=self._invocation_id, claim=m.claim)
                return "有其他请求正在使用这个工作区，等它们结束后会重建；这次请求里文件还在。"
            await self.backend.detach()
            self._counted = False
            self._mapping = None
            return "工作区已清空：之前的文件、装的依赖和公开链接都没了，下次用到时会新建一个空工作区。"

    async def close(self) -> None:
        try:
            # An acquisition still in flight (a tool call cancelled mid-attach) may yet count this session in
            # `active`; wait for it so the count is released, not leaked.
            await self.backend.settle()
            if self._counted and self._mapping is not None:
                await self._release(self._mapping)
        except Exception:
            LOGGER.exception("sandbox session close failed for group {}", self._group_id)
        finally:
            self._counted = False
            try:
                await self.backend.aclose()
            except Exception:
                LOGGER.exception("closing sandbox connections failed")

    async def _release(self, mine: Mapping) -> None:
        async with self._mgr._group_lock(self._group_id, wait=_RELEASE_LOCK_WAIT) as locked:
            remaining = await self._mgr._incr_active(mine, -1)
            if remaining < 0:
                return  # mapping gone or replaced meanwhile
            m = await self._mgr._load(self._group_id)
            if m is None or m.claim != mine.claim:
                return
            if remaining == 0 and not locked:
                # Reaping without the lock could kill an invocation attaching right now; leave the leftovers.
                LOGGER.warning("sandbox group {} lock busy; skipping reap on release", self._group_id)
            elif remaining == 0:
                await self._reap(m)
                if m.recreate and await self._mgr._destroy_if_idle(m, reason="pending recreate") == 1:
                    return
            await self._mgr._renew(m, self._cfg)

    async def _reap(self, m: Mapping) -> None:
        argv = [_REAP]
        for link in m.links:
            if link.pgid is not None:
                argv += ["--keep-pgid", str(link.pgid)]
        try:
            result = await self.backend.exec(argv, seconds=30)
            if result.exit_code != 0:
                LOGGER.warning("sandbox reap exited {}: {}", result.exit_code, result.stderr[:300])
        except WorkspaceUnavailableError:
            pass  # the sandbox is gone; nothing left to reap
        except Exception:
            LOGGER.exception("sandbox reap failed for group {}", self._group_id)


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

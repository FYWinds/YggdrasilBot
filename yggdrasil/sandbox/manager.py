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
_WAIT_FOR_CREATOR = 120.0
_SERVE_WAIT_STEPS = 60  # x 0.5s
_REAP = "/opt/ygg/reap"
_SERVE_DIR = f"{DEFAULT_ROOT}/.ygg/serve"

_GONE_NOTICE = "[工作区] 之前的工作区已过期回收，里面的文件都没了；这是新建的空工作区。"
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
        if m.active > 0 and not force:
            await self._r().hset(ws_key(group_id), "recreate", "1")  # pyright: ignore[reportGeneralTypeIssues]
            self._audit(group_id, "destroy_deferred", claim=m.claim, reason=reason, active=m.active)
            return False
        await self._destroy_mapping(m, reason=reason)
        return True

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
    async def _creator_lock(self, group_id: int) -> AsyncIterator[bool]:
        token = link_id()
        got = bool(await self._r().set(lock_key(group_id), token, nx=True, ex=_LOCK_TTL))
        try:
            yield got
        finally:
            if got:
                try:
                    await self._r().eval(_UNLOCK, 1, lock_key(group_id), token)  # pyright: ignore[reportGeneralTypeIssues]
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

    async def _wait_ready(self, name: str, deadline: float) -> tuple[str, str | None]:
        """Wait until the claim is Ready with a pod IP and sandboxd answers /v1/health; (pod ip, sandbox name)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as http:
            while True:
                claim = await self._kube().get(self._claim_path(name))
                if claim is None:
                    raise SandboxToolError("新建的工作区被回收了，稍后再试。")
                ready, ip, sandbox, failure = _claim_ready(claim)
                if failure:
                    raise SandboxToolError(f"工作区启动失败（{failure}），稍后再试。")
                if ready and ip:
                    try:
                        health = await http.get(f"{self._endpoint(ip).rest_url}/v1/health")
                        if health.status_code == 200:
                            return ip, sandbox
                    except httpx.HTTPError:
                        pass
                if time.monotonic() > deadline:
                    raise SandboxToolError(f"工作区没能在 {READY_TIMEOUT:.0f} 秒内就绪，稍后再试。")
                await asyncio.sleep(0.5)

    async def _evict_for(self, group_id: int, cfg: ToolSettings) -> None:
        others = [m for m in await self._all_mappings() if m.group_id != group_id]
        excess = len(others) - cfg.sandbox_max_workspaces + 1
        if excess <= 0:
            return
        idle = sorted((m for m in others if m.active <= 0), key=lambda m: m.last_used)
        if len(idle) < excess:
            raise SandboxToolError(
                f"同时存在的工作区已达上限（{cfg.sandbox_max_workspaces} 个），而且都在使用中，稍后再试。"
            )
        for m in idle[:excess]:
            await self._destroy_mapping(m, reason=f"evicted (LRU) for group {group_id}")

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
        """Check an existing mapping: (mapping to use, None) or (None, notice) after dropping a dead one."""
        claim = await self._kube().get(self._claim_path(m.claim))
        if claim is None:
            await self._drop(m, reason="claim gone")
            return None, _GONE_NOTICE
        if m.active <= 0 and m.recreate:
            await self._destroy_mapping(m, reason="pending recreate")
            return None, _RESET_NOTICE
        if m.active <= 0 and m.created_at + cfg.sandbox_max_age_min * 60 - time.time() < cfg.sandbox_invocation_s:
            await self._destroy_mapping(m, reason="max age")
            return None, _AGED_NOTICE
        ready, ip, sandbox, _ = _claim_ready(claim)
        if not ready:
            ip, sandbox = await self._wait_ready(m.claim, time.monotonic() + READY_TIMEOUT)
        notice = None
        if ip and ip != m.pod_ip:
            if m.pod_ip:
                notice = _RESTARTED_NOTICE
            m.pod_ip = ip
            m.sandbox = sandbox or m.sandbox
            await self._save(m, "pod_ip", "sandbox")
        return m, notice

    async def _provide(self, group_id: int, cfg: ToolSettings) -> tuple[Mapping, str | None]:
        """The group's workspace, attached or created, with ``active`` already incremented for the caller."""
        notice: str | None = None
        deadline = time.monotonic() + _WAIT_FOR_CREATOR
        while True:
            m = await self._load(group_id)
            if m is not None:
                usable, why = await self._usable(m, cfg)
                if usable is not None:
                    if await self._incr_active(usable, 1) >= 0:
                        usable.active += 1
                        await self._renew(usable, cfg)
                        return usable, notice
                    continue
                notice = why
            async with self._creator_lock(group_id) as got:
                if got:
                    if (await self._load(group_id)) is None:
                        m = await self._create(group_id, cfg)
                        await self._incr_active(m, 1)
                        m.active = 1
                        return m, notice
                    continue
            if time.monotonic() > deadline:
                raise SandboxToolError("工作区正在被另一个请求创建，等太久了，稍后再试。")
            await asyncio.sleep(0.5)

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
        name = f"sbx-{rid}"
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
                    {"kind": "Rule", "match": f"Host(`{rid}.{domain}`)", "services": [{"name": name, "port": 80}]}
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
        name = f"sbx-{rid}"
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
                    url=f"https://{rid}.{domain}",
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
            others = m.active - (1 if mine else 0)
            if others > 0:
                await self._mgr._r().hset(ws_key(m.group_id), "recreate", "1")  # pyright: ignore[reportGeneralTypeIssues]
                self._mgr._audit(self._group_id, "reset_deferred", invocation=self._invocation_id, claim=m.claim)
                return "有其他请求正在使用这个工作区，等它们结束后会重建；这次请求里文件还在。"
            await self._mgr._destroy_mapping(m, reason=f"reset_workspace ({self._invocation_id})")
            await self.backend.detach()
            self._counted = False
            self._mapping = None
            return "工作区已清空：之前的文件、装的依赖和公开链接都没了，下次用到时会新建一个空工作区。"

    async def close(self) -> None:
        try:
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
        remaining = await self._mgr._incr_active(mine, -1)
        if remaining < 0:
            return  # mapping gone or replaced meanwhile
        m = await self._mgr._load(self._group_id)
        if m is None or m.claim != mine.claim:
            return
        if remaining == 0:
            keep = [str(x.pgid) for x in m.links if x.pgid is not None]
            argv = [_REAP]
            for pgid in keep:
                argv += ["--keep-pgid", pgid]
            try:
                result = await self.backend.exec(argv, seconds=30)
                if result.exit_code != 0:
                    LOGGER.warning("sandbox reap exited {}: {}", result.exit_code, result.stderr[:300])
            except WorkspaceUnavailableError:
                return
            except Exception:
                LOGGER.exception("sandbox reap failed for group {}", self._group_id)
            if m.recreate:
                await self._mgr._destroy_mapping(m, reason="pending recreate")
                return
        await self._mgr._renew(m, self._cfg)


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

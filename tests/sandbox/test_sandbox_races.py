"""Concurrency rules of WorkspaceManager / Session, made deterministic with a fake Kubernetes API and an in-process
Redis (no sandboxd: the in-sandbox reap is replaced by a recorder that can be held open)."""

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import fakeredis
import httpx
import pytest
from pydantic_ai.workspaces import WorkspaceRef

from tests.sandbox.helpers import FakeKube
from yggdrasil.sandbox import SandboxError, Session, WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint, _Exec
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.manager import Listener
from yggdrasil.sandbox.state import keep_key, ws_key
from yggdrasil.settings import ToolSettings

GROUP = 10001
CFG = ToolSettings()


class Reaper:
    """Stands in for ``backend.exec`` (only the reap runs through it here); ``hold`` keeps a reap running."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.started = asyncio.Event()
        self.hold: asyncio.Event | None = None

    async def __call__(self, argv: Sequence[str], **_: Any) -> _Exec:
        self.calls.append(list(argv))
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        return _Exec(0, b"0\n", b"")


@pytest.fixture
def mgr(kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> WorkspaceManager:
    manager = WorkspaceManager(
        token="test",
        namespace="ns",
        warmpool="pool",
        kube=kube_client,
        redis=lambda: fake_redis,
        endpoint=lambda _ip: Endpoint("http://127.0.0.1:9", "127.0.0.1:9"),
    )

    async def healthy(_http: httpx.AsyncClient, _ip: str) -> bool:
        return True

    manager._healthy = healthy  # pyright: ignore[reportAttributeAccessIssue]
    return manager


def _session(mgr: WorkspaceManager, name: str, reaper: Reaper | None = None) -> Session:
    s = mgr.session(GROUP, invocation_id=name, cfg=CFG)
    s.backend.exec = reaper or Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    return s


async def _active(redis: fakeredis.FakeAsyncRedis, group: int = GROUP) -> int:
    return int(await redis.hget(ws_key(group), "active") or 0)  # pyright: ignore[reportGeneralTypeIssues]


async def _claim(redis: fakeredis.FakeAsyncRedis, group: int = GROUP) -> str | None:
    claim = await redis.hget(ws_key(group), "claim")  # pyright: ignore[reportGeneralTypeIssues]
    return None if claim is None else str(claim)


async def test_close_waits_for_an_acquisition_still_in_flight(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    reaper = Reaper()
    s = _session(mgr, "a", reaper)
    kube.claim_gate = asyncio.Event()
    tool_call = asyncio.create_task(s.backend.working_dir())
    await kube.claim_get_entered.wait()  # the claim exists; waiting for it to be ready
    tool_call.cancel()  # the run ends (e.g. moderation blocked it) while the tool is still attaching
    with pytest.raises(asyncio.CancelledError):
        await tool_call
    closing = asyncio.create_task(s.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    kube.claim_gate.set()
    await closing
    assert await _active(fake_redis) == 0
    assert reaper.calls == [["/opt/ygg/reap"]]


async def test_last_release_and_attach_are_serialized(
    mgr: WorkspaceManager, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    reaper = Reaper()
    a = _session(mgr, "a", reaper)
    await a.backend.working_dir()
    claim = await _claim(fake_redis)
    reaper.hold = asyncio.Event()
    closing = asyncio.create_task(a.close())
    await reaper.started.wait()
    b = _session(mgr, "b")
    attaching = asyncio.create_task(b.backend.working_dir())
    await asyncio.sleep(0.5)
    # B must not attach while A's reap (which kills everything but served processes) is running.
    assert not attaching.done()
    reaper.hold.set()
    await closing
    await attaching
    assert b.backend.ref == WorkspaceRef(provider="sandboxd", id=claim or "")
    assert await _active(fake_redis) == 1
    await b.close()


async def test_pending_recreate_does_not_pull_the_workspace_from_under_an_attacher(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    reaper = Reaper()
    a = _session(mgr, "a", reaper)
    await a.backend.working_dir()
    old = await _claim(fake_redis)
    assert not await mgr.destroy(GROUP, reason="admin")  # deferred: A is using it
    reaper.hold = asyncio.Event()
    closing = asyncio.create_task(a.close())
    await reaper.started.wait()
    b = _session(mgr, "b")
    attaching = asyncio.create_task(b.backend.working_dir())
    await asyncio.sleep(0.3)
    reaper.hold.set()
    await closing
    await attaching
    assert b.backend.ref is not None
    assert b.backend.ref.id != old
    assert kube.claim_names() == [b.backend.ref.id]
    assert await _active(fake_redis) == 1
    await b.close()


async def test_failed_renewal_does_not_leak_active(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    a = _session(mgr, "a")
    await a.backend.working_dir()
    await a.close()
    kube.fail_patch = True
    b = _session(mgr, "b")
    with pytest.raises(SandboxError):
        await b.backend.working_dir()
    assert await _active(fake_redis) == 0
    kube.fail_patch = False
    await b.backend.working_dir()  # the next tool call tries again
    assert await _active(fake_redis) == 1
    await b.close()
    assert await _active(fake_redis) == 0


async def test_failed_pod_is_replaced_with_a_notice(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    a = _session(mgr, "a")
    await a.backend.working_dir()
    await a.close()
    old = await _claim(fake_redis)
    assert old is not None
    kube.claim(old)["status"]["conditions"] = [{"type": "Ready", "status": "False", "reason": "PodFailed"}]
    b = _session(mgr, "b")
    attachment = await b.acquire(None, lambda _ref: None)
    assert attachment.notice is not None
    assert "文件都没了" in attachment.notice
    new = await _claim(fake_redis)
    assert new not in (None, old)
    assert kube.claim_names() == [new]
    await b.close()


async def test_aged_workspace_in_use_is_retired_not_destroyed(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    a_reaper = Reaper()
    a = _session(mgr, "a", a_reaper)
    await a.backend.working_dir()
    old = await _claim(fake_redis)
    assert old is not None
    # Fewer than sandbox_invocation_s seconds of lifetime left: a new run must not start on it.
    aged = time.time() - CFG.sandbox_max_age_min * 60 + CFG.sandbox_invocation_s / 2
    await fake_redis.hset(ws_key(GROUP), "created_at", repr(aged))  # pyright: ignore[reportGeneralTypeIssues]
    b = _session(mgr, "b")
    attachment = await b.acquire(None, lambda _ref: None)
    assert attachment.notice is not None
    assert "最长存活时间" in attachment.notice
    new = await _claim(fake_redis)
    assert new is not None
    assert new != old
    assert kube.claim_names() == sorted([old, new])  # A keeps using the old one until it expires
    await a.close()
    assert a_reaper.calls == []  # A's release no longer owns the group's workspace
    assert await _active(fake_redis) == 1
    await b.close()


async def test_eviction_rechecks_idleness_atomically(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces=1)
    a = mgr.session(GROUP, invocation_id="a", cfg=cfg)
    a.backend.exec = Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    await a.backend.working_dir()
    await a.close()
    stale = await mgr._all_mappings()  # pyright: ignore[reportPrivateUsage]  # says GROUP is idle
    busy = mgr.session(GROUP, invocation_id="busy", cfg=cfg)
    busy.backend.exec = Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    await busy.backend.working_dir()

    async def snapshot() -> list[Any]:
        return stale

    monkeypatch.setattr(mgr, "_all_mappings", snapshot)
    other = mgr.session(GROUP + 1, invocation_id="other", cfg=cfg)
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()
    assert await _claim(fake_redis) in kube.claim_names()
    await busy.close()


async def test_reset_and_destroy_respect_other_users(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    a = _session(mgr, "a")
    b = _session(mgr, "b")
    await a.backend.working_dir()
    await b.backend.working_dir()
    claim = await _claim(fake_redis)
    assert "其他请求" in await a.reset()
    assert kube.claim_names() == [claim]
    assert await fake_redis.hget(ws_key(GROUP), "recreate") == "1"  # pyright: ignore[reportGeneralTypeIssues]
    await b.close()
    assert "清空" in await a.reset()  # only A itself still uses it
    assert kube.claim_names() == []
    await a.close()
    assert not await fake_redis.exists(ws_key(GROUP))


async def test_touch_renews_from_the_current_mapping_when_due(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    a = _session(mgr, "a")
    await a.backend.working_dir()
    ref = a.backend.ref
    assert ref is not None
    patches = kube.patches
    await a.touch(ref)
    assert kube.patches == patches  # renewed at attach; nothing due yet
    # Another invocation served a link meanwhile: renewal must keep the workspace up to its max age.
    link = '[{"id": "abcdefghij", "port": 8000, "url": "u", "command": "c", "created_at": 0, "pgid": 7}]'
    await fake_redis.hset(ws_key(GROUP), "links", link)  # pyright: ignore[reportGeneralTypeIssues]
    a._renewed_at -= CFG.sandbox_idle_min * 60  # pyright: ignore[reportPrivateUsage]
    await a.touch(ref)
    assert kube.patches == patches + 1
    expires = float(await fake_redis.hget(ws_key(GROUP), "expires_at") or 0)  # pyright: ignore[reportGeneralTypeIssues]
    assert expires > time.time() + CFG.sandbox_max_age_min * 60 - 120
    await a.close()


async def test_keep_hash_is_spared_by_reap_and_cleared_with_the_workspace(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await fake_redis.hset(keep_key(GROUP), "kernel", "999")  # pyright: ignore[reportGeneralTypeIssues]  # a stale one
    reaper = Reaper()
    a = _session(mgr, "a", reaper)
    await a.backend.working_dir()
    assert not await fake_redis.exists(keep_key(GROUP))  # a new workspace starts without kept processes
    await a.keep_kernel(12)
    await fake_redis.hset(keep_key(GROUP), "job:abc", "30")  # pyright: ignore[reportGeneralTypeIssues]
    await a.close()
    assert reaper.calls == [["/opt/ygg/reap", "--keep-tree", "30", "--keep-tree", "12"]]
    await a.keep_kernel(None)
    assert await fake_redis.hgetall(keep_key(GROUP)) == {"job:abc": "30"}  # pyright: ignore[reportGeneralTypeIssues]
    assert await mgr.destroy(GROUP, reason="admin", force=True)  # admin destroy ignores jobs
    assert not await fake_redis.exists(keep_key(GROUP))
    assert kube.claim_names() == []


async def test_background_jobs_keep_a_workspace_from_reset_and_eviction(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces=1)
    a = mgr.session(GROUP, invocation_id="a", cfg=cfg)
    a.backend.exec = Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    await a.backend.working_dir()
    claim = await _claim(fake_redis)
    await fake_redis.hset(keep_key(GROUP), "job:abc", "30")  # pyright: ignore[reportGeneralTypeIssues]
    with pytest.raises(SandboxError, match="cancel_job"):
        await a.reset()
    assert await fake_redis.hget(ws_key(GROUP), "recreate") == "0"  # pyright: ignore[reportGeneralTypeIssues]
    await a.close()
    other = mgr.session(GROUP + 1, invocation_id="other", cfg=cfg)
    other.backend.exec = Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()  # idle, but its job keeps it
    assert kube.claim_names() == [claim]
    await fake_redis.hdel(keep_key(GROUP), "job:abc")  # pyright: ignore[reportGeneralTypeIssues]
    await other.backend.working_dir()
    assert claim not in kube.claim_names()
    await other.close()


async def test_backend_for_and_renew_never_create(mgr: WorkspaceManager, kube: FakeKube) -> None:
    assert await mgr.backend_for(GROUP) is None
    assert not await mgr.renew(GROUP, CFG)
    assert kube.claims() == []
    a = _session(mgr, "a")
    await a.backend.working_dir()
    found = await mgr.backend_for(GROUP)
    assert found is not None
    backend, claim = found
    assert backend.ref == WorkspaceRef(provider="sandboxd", id=claim)
    await backend.aclose()
    assert (await mgr.get(GROUP)).active == 1  # pyright: ignore[reportOptionalMemberAccess]  # not counted
    patches = kube.patches
    assert await mgr.renew(GROUP, CFG)
    assert kube.patches == patches + 1
    await a.close()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]
    assert not await mgr.renew(GROUP, CFG)
    assert await mgr.get(GROUP) is None


async def test_publish_links_the_listener_and_unpublish_removes_it(
    kube_client: KubeClient, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = WorkspaceManager(
        token="test",
        namespace="ns",
        warmpool="pool",
        link_domain="links.example.com",
        kube=kube_client,
        redis=lambda: fake_redis,
        endpoint=lambda _ip: Endpoint("http://127.0.0.1:9", "127.0.0.1:9"),
    )

    async def healthy(_http: httpx.AsyncClient, _ip: str) -> bool:
        return True

    async def listener(_gid: int, port: int) -> Listener:
        return Listener(pid=41, pgid=40, addr="0.0.0.0", cmdline=f"python3 -m http.server {port}")

    monkeypatch.setattr(mgr, "_healthy", healthy)
    monkeypatch.setattr(mgr, "_listener", listener)
    cfg = ToolSettings(sandbox_max_links=1)
    with pytest.raises(SandboxError, match="回收"):
        await mgr.publish(GROUP, 8000, cfg, audit={})
    a = mgr.session(GROUP, invocation_id="a", cfg=cfg)
    reaper = Reaper()
    a.backend.exec = reaper  # pyright: ignore[reportAttributeAccessIssue]
    await a.backend.working_dir()
    with pytest.raises(SandboxError, match="8080"):
        await a.publish(8080)
    link = await a.publish(8000)
    assert link.pid == 41
    assert link.command == "python3 -m http.server 8000"
    assert link.url.startswith("https://sbx-")
    assert link.url.endswith(".links.example.com")
    assert await a.publish(8000) == link  # already public: the same link
    with pytest.raises(SandboxError, match="最多 1 个"):
        await a.publish(8001)
    info = await mgr.get(GROUP)
    assert info is not None
    assert info.expires_at > time.time() + CFG.sandbox_max_age_min * 60 - 120  # links keep it to max age
    rid = link.url.removeprefix("https://sbx-").split(".", 1)[0]
    assert f"/api/v1/namespaces/ns/services/sbx-{rid}" in kube.objects
    await a.close()
    assert reaper.calls == [["/opt/ygg/reap", "--keep-pgid", "40", "--keep-tree", "41"]]
    assert await mgr.unpublish(GROUP, 8000, cfg, audit={"job": "x"})
    assert not await mgr.unpublish(GROUP, 8000, cfg, audit={})
    assert f"/api/v1/namespaces/ns/services/sbx-{rid}" not in kube.objects
    assert (await mgr.get(GROUP)).links == ()  # pyright: ignore[reportOptionalMemberAccess]

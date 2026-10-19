"""Concurrency rules of WorkspaceManager / Session with per-topic workspaces, made deterministic with a fake
Kubernetes API and an in-process Redis (no sandboxd: the in-sandbox reap is replaced by a recorder that can be held
open)."""

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import fakeredis
import httpx
import pytest
from pydantic_ai.workspaces import WorkspaceError, WorkspaceRef

from tests.sandbox.helpers import FakeKube
from yggdrasil.agent import job_tools, sandbox_tools
from yggdrasil.sandbox import SandboxError, SandboxToolError, Session, WorkspaceInfo, WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint, _Exec
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.manager import Listener
from yggdrasil.sandbox.state import LinkRecord, index_key, keep_key, links_json, ws_key
from yggdrasil.settings import ToolSettings

GROUP = 10001
WS = "topic"
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


def _manager(kube_client: KubeClient, redis: fakeredis.FakeAsyncRedis, *, link_domain: str | None = None):
    manager = WorkspaceManager(
        token="test",
        namespace="ns",
        warmpool="pool",
        link_domain=link_domain,
        kube=kube_client,
        redis=lambda: redis,
        endpoint=lambda _ip: Endpoint("http://127.0.0.1:9", "127.0.0.1:9"),
    )

    async def healthy(_http: httpx.AsyncClient, _ip: str) -> bool:
        return True

    manager._healthy = healthy  # pyright: ignore[reportAttributeAccessIssue]
    return manager


@pytest.fixture
def mgr(kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> WorkspaceManager:
    return _manager(kube_client, fake_redis)


def _session(
    mgr: WorkspaceManager,
    invocation: str,
    reaper: Reaper | None = None,
    *,
    workspace: str | None = WS,
    cfg: ToolSettings = CFG,
    group: int = GROUP,
) -> Session:
    s = mgr.session(group, invocation_id=invocation, cfg=cfg, workspace=workspace)
    s.backend.exec = reaper or Reaper()  # pyright: ignore[reportAttributeAccessIssue]
    return s


async def _reserve(mgr: WorkspaceManager, name: str = WS, *, group: int = GROUP, cfg: ToolSettings = CFG) -> None:
    await mgr.reserve(group, name, f"{name} 的说明", cfg)


async def _active(redis: fakeredis.FakeAsyncRedis, name: str = WS, group: int = GROUP) -> int:
    return int(await redis.hget(ws_key(group, name), "active") or 0)  # pyright: ignore[reportGeneralTypeIssues]


async def _claim(redis: fakeredis.FakeAsyncRedis, name: str = WS, group: int = GROUP) -> str | None:
    claim = await redis.hget(ws_key(group, name), "claim")  # pyright: ignore[reportGeneralTypeIssues]
    return str(claim) if claim else None


async def _names(mgr: WorkspaceManager, group: int = GROUP) -> list[str]:
    return sorted(w.name for w in await mgr.workspaces(group))


# --- one workspace, several invocations (as before, now per (group, name)) -------------------------------------


async def test_close_waits_for_an_acquisition_still_in_flight(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
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
    await _reserve(mgr)
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
    await _reserve(mgr)
    reaper = Reaper()
    a = _session(mgr, "a", reaper)
    other = _session(mgr, "other")
    await a.backend.working_dir()
    await other.backend.working_dir()
    old = await _claim(fake_redis)
    assert "其他请求" in await other.reset()  # deferred: A is using it too
    await other.close()
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
    await _reserve(mgr)
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


async def test_failed_pod_is_replaced_under_the_same_name_with_a_notice(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
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
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.description == f"{WS} 的说明"  # same workspace, fresh pod
    await b.close()


async def test_aged_workspace_in_use_is_retired_not_destroyed(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    a_reaper = Reaper()
    a = _session(mgr, "a", a_reaper)
    await a.backend.working_dir()
    old = await _claim(fake_redis)
    assert old is not None
    # Fewer than sandbox_invocation_s seconds left before its hard cap: a new run must not start on it.
    await fake_redis.hset(ws_key(GROUP, WS), "max_until", repr(time.time() + CFG.sandbox_invocation_s / 2))  # pyright: ignore[reportGeneralTypeIssues]
    b = _session(mgr, "b")
    attachment = await b.acquire(None, lambda _ref: None)
    assert attachment.notice is not None
    assert "最长存活时间" in attachment.notice
    new = await _claim(fake_redis)
    assert new is not None
    assert new != old
    assert kube.claim_names() == sorted([old, new])  # A keeps using the old one until it expires
    await a.close()
    assert a_reaper.calls == []  # A's release no longer owns the workspace
    assert await _active(fake_redis) == 1
    await b.close()


async def test_reset_and_destroy_respect_other_users(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    a = _session(mgr, "a")
    b = _session(mgr, "b")
    await a.backend.working_dir()
    await b.backend.working_dir()
    claim = await _claim(fake_redis)
    assert "其他请求" in await a.reset()
    assert kube.claim_names() == [claim]
    assert await fake_redis.hget(ws_key(GROUP, WS), "recreate") == "1"  # pyright: ignore[reportGeneralTypeIssues]
    await b.close()
    assert "已清空" in await a.reset()  # only A itself still uses it
    assert kube.claim_names() == []
    await a.close()
    # The workspace stays, empty: same name and description, a fresh pod on next use.
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.pending
    assert info.description == f"{WS} 的说明"
    assert await fake_redis.ttl(ws_key(GROUP, WS)) > 0  # pyright: ignore[reportGeneralTypeIssues]


async def test_touch_renews_from_the_current_record_when_due(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    a = _session(mgr, "a")
    await a.backend.working_dir()
    ref = a.backend.ref
    assert ref is not None
    patches = kube.patches
    await a.touch(ref)
    assert kube.patches == patches  # renewed at attach; nothing due yet
    # Another invocation served a link meanwhile: renewal must keep the workspace up to its hard cap.
    link = '[{"id": "abcdefghij", "port": 8000, "url": "u", "command": "c", "created_at": 0, "pgid": 7}]'
    await fake_redis.hset(ws_key(GROUP, WS), "links", link)  # pyright: ignore[reportGeneralTypeIssues]
    a._renewed_at -= CFG.sandbox_idle_min * 60  # pyright: ignore[reportPrivateUsage]
    await a.touch(ref)
    assert kube.patches == patches + 1
    expires = float(await fake_redis.hget(ws_key(GROUP, WS), "expires_at") or 0)  # pyright: ignore[reportGeneralTypeIssues]
    assert expires > time.time() + CFG.sandbox_max_age_min * 60 - 120
    await a.close()


async def test_keep_hash_is_spared_by_reap_and_cleared_with_the_workspace(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    await fake_redis.hset(keep_key(GROUP, WS), "kernel", "999")  # pyright: ignore[reportGeneralTypeIssues]  # a stale one
    reaper = Reaper()
    a = _session(mgr, "a", reaper)
    await a.backend.working_dir()
    assert not await fake_redis.exists(keep_key(GROUP, WS))  # a new pod starts without kept processes
    await a.keep_kernel(12)
    await fake_redis.hset(keep_key(GROUP, WS), "job:abc", "30")  # pyright: ignore[reportGeneralTypeIssues]
    await a.close()
    assert reaper.calls == [["/opt/ygg/reap", "--keep-tree", "30", "--keep-tree", "12"]]
    await a.keep_kernel(None)
    assert await fake_redis.hgetall(keep_key(GROUP, WS)) == {"job:abc": "30"}  # pyright: ignore[reportGeneralTypeIssues]
    assert await mgr.destroy(GROUP, WS, reason="admin")  # admin destroy ignores jobs
    assert not await fake_redis.exists(keep_key(GROUP, WS))
    assert not await fake_redis.exists(ws_key(GROUP, WS))
    assert not await fake_redis.sismember(index_key(GROUP), WS)  # pyright: ignore[reportGeneralTypeIssues]
    assert kube.claim_names() == []


async def test_backend_for_and_renew_never_create(mgr: WorkspaceManager, kube: FakeKube) -> None:
    assert await mgr.backend_for(GROUP, WS) is None
    assert not await mgr.renew(GROUP, WS, CFG)
    await _reserve(mgr)
    assert await mgr.backend_for(GROUP, WS) is None  # pending: no pod yet
    assert kube.claims() == []
    a = _session(mgr, "a")
    await a.backend.working_dir()
    found = await mgr.backend_for(GROUP, WS)
    assert found is not None
    backend, claim = found
    assert backend.ref == WorkspaceRef(provider="sandboxd", id=claim)
    await backend.aclose()
    assert (await mgr.get(GROUP, WS)).active == 1  # pyright: ignore[reportOptionalMemberAccess]  # not counted
    patches = kube.patches
    assert await mgr.renew(GROUP, WS, CFG)
    assert kube.patches == patches + 1
    await a.close()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]
    assert not await mgr.renew(GROUP, WS, CFG)
    assert await mgr.get(GROUP, WS) is None  # expired: the record is gone


async def test_publish_links_the_listener_and_unpublish_removes_it(
    kube_client: KubeClient, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = _manager(kube_client, fake_redis, link_domain="links.example.com")

    async def listener(_gid: int, _name: str, port: int) -> Listener:
        return Listener(pid=41, pgid=40, addr="0.0.0.0", cmdline=f"python3 -m http.server {port}")

    monkeypatch.setattr(mgr, "_listener", listener)
    cfg = ToolSettings(sandbox_max_links=1)
    with pytest.raises(SandboxError, match="回收"):
        await mgr.publish(GROUP, WS, 8000, cfg, audit={})
    await _reserve(mgr)
    a = _session(mgr, "a", cfg=cfg)
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
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.expires_at == info.max_until  # links keep it to its hard cap
    rid = link.url.removeprefix("https://sbx-").split(".", 1)[0]
    service = kube.objects[f"/api/v1/namespaces/ns/services/sbx-{rid}"]
    assert service["metadata"]["labels"]["yggdrasil/workspace"] == WS
    await a.close()
    assert reaper.calls == [["/opt/ygg/reap", "--keep-pgid", "40", "--keep-tree", "41"]]
    assert await mgr.unpublish(GROUP, WS, 8000, cfg, audit={"job": "x"})
    assert not await mgr.unpublish(GROUP, WS, 8000, cfg, audit={})
    assert f"/api/v1/namespaces/ns/services/sbx-{rid}" not in kube.objects
    assert (await mgr.get(GROUP, WS)).links == ()  # pyright: ignore[reportOptionalMemberAccess]


# --- the deployment-wide cap --------------------------------------------------------------------------------


async def test_global_eviction_rechecks_idleness_atomically(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces=1)
    await _reserve(mgr, cfg=cfg)
    a = _session(mgr, "a", cfg=cfg)
    await a.backend.working_dir()
    await a.close()
    stale = await mgr._all_mappings()  # pyright: ignore[reportPrivateUsage]  # says GROUP's workspace is idle
    busy = _session(mgr, "busy", cfg=cfg)
    await busy.backend.working_dir()

    async def snapshot() -> list[Any]:
        return stale

    monkeypatch.setattr(mgr, "_all_mappings", snapshot)
    await _reserve(mgr, "other", group=GROUP + 1, cfg=cfg)
    other = _session(mgr, "other", workspace="other", cfg=cfg, group=GROUP + 1)
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()
    assert await _claim(fake_redis) in kube.claim_names()
    await busy.close()


async def test_background_jobs_keep_a_workspace_from_reset_and_eviction(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces=1)
    await _reserve(mgr, cfg=cfg)
    a = _session(mgr, "a", cfg=cfg)
    await a.backend.working_dir()
    claim = await _claim(fake_redis)
    await fake_redis.hset(keep_key(GROUP, WS), "job:abc", "30")  # pyright: ignore[reportGeneralTypeIssues]
    with pytest.raises(SandboxError, match="cancel_job"):
        await a.reset()
    assert await fake_redis.hget(ws_key(GROUP, WS), "recreate") == "0"  # pyright: ignore[reportGeneralTypeIssues]
    await a.close()
    await _reserve(mgr, "other", group=GROUP + 1, cfg=cfg)
    other = _session(mgr, "other", workspace="other", cfg=cfg, group=GROUP + 1)
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()  # idle, but its job keeps it
    assert kube.claim_names() == [claim]
    await fake_redis.hdel(keep_key(GROUP, WS), "job:abc")  # pyright: ignore[reportGeneralTypeIssues]
    await other.backend.working_dir()
    assert claim not in kube.claim_names()
    assert await mgr.get(GROUP, WS) is None  # evicted for another group: gone, not kept pending
    await other.close()


# --- selecting, switching, the per-group cap ----------------------------------------------------------------


async def test_tools_before_selecting_get_the_workspace_list(mgr: WorkspaceManager, kube: FakeKube) -> None:
    s = mgr.session(GROUP, invocation_id="a", cfg=CFG)
    with pytest.raises(SandboxToolError) as none_yet:
        await s.backend.working_dir()
    assert "本群也还没有工作区" in str(none_yet.value)
    assert "new=True" in str(none_yet.value)
    await mgr.reserve(GROUP, "pelican-game", "鹈鹕小游戏", CFG)
    await mgr.reserve(GROUP, "errand-site", "跑腿网站", CFG)
    # Harness file tools reach the backend directly: the refusal is a WorkspaceError they report to the model.
    with pytest.raises(WorkspaceError) as listed:
        await s.backend.read_bytes("/workspace/a.txt")
    text = str(listed.value)
    assert "还没有选工作区" in text
    assert "- pelican-game：鹈鹕小游戏" in text
    assert "- errand-site：跑腿网站" in text
    assert 'open_workspace(name="名字")' in text
    with pytest.raises(SandboxToolError, match="pelican-game"):
        await s.require()
    assert kube.claims() == []
    await s.close()
    assert await _names(mgr) == ["errand-site", "pelican-game"]  # not this session's reservations


async def test_open_selects_switches_and_releases_the_previous(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    reaper = Reaper()
    s = _session(mgr, "a", reaper, workspace=None)
    opened = await s.open("alpha", new=True, description="  第一个\n话题 ")
    assert opened.created
    assert opened.info.pending
    assert opened.info.description == "第一个 话题"
    assert "已新建工作区「alpha」" in await s.summary(opened)
    await s.backend.working_dir()
    [claim] = kube.claims()
    alpha = claim["metadata"]["name"]
    assert alpha.startswith(f"ygg-sbx-{GROUP}-alpha-")
    assert claim["metadata"]["labels"]["yggdrasil/workspace"] == "alpha"
    assert claim["metadata"]["labels"]["yggdrasil/group"] == str(GROUP)
    assert await _active(fake_redis, "alpha") == 1
    assert await fake_redis.ttl(ws_key(GROUP, "alpha")) == -1  # pyright: ignore[reportGeneralTypeIssues]  # has a pod: no TTL

    switched = await s.open("beta", new=True, description="第二个话题")
    assert switched.left == "alpha"
    assert reaper.calls == [["/opt/ygg/reap"]]  # alpha was released like at the end of an invocation
    assert await _active(fake_redis, "alpha") == 0
    assert "已离开工作区「alpha」" in await s.summary(switched)
    await s.backend.working_dir()
    assert s.name == "beta"
    assert await _active(fake_redis, "beta") == 1
    assert len(kube.claims()) == 2

    back = await s.open("alpha", description="第一个话题，改过")
    assert (back.left, back.created, back.updated) == ("beta", False, True)
    await s.backend.working_dir()
    assert s.backend.ref == WorkspaceRef(provider="sandboxd", id=alpha)  # the same pod again
    assert (await mgr.get(GROUP, "alpha")).description == "第一个话题，改过"  # pyright: ignore[reportOptionalMemberAccess]
    assert await _active(fake_redis, "beta") == 0

    with pytest.raises(SandboxError, match="没有叫「gamma」") as missing:
        await s.open("gamma")
    assert "- alpha（当前选中）" in str(missing.value)
    with pytest.raises(SandboxError, match="已经有叫「beta」"):
        await s.open("beta", new=True, description="重复")
    with pytest.raises(SandboxError, match="description"):
        await s.open("gamma", new=True)
    with pytest.raises(SandboxError, match="小写字母"):
        await s.open("Gamma Ray", new=True, description="x")
    assert s.name == "alpha"  # failed opens change nothing
    await s.close()
    assert await _active(fake_redis, "alpha") == 0


async def test_unused_reservations_are_dropped_when_the_invocation_ends(
    mgr: WorkspaceManager, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    s = _session(mgr, "a", workspace=None)
    await s.open("used", new=True, description="用过的")
    await s.backend.working_dir()
    await s.open("unused", new=True, description="没用过的")
    assert await _names(mgr) == ["unused", "used"]
    assert 0 < await fake_redis.ttl(ws_key(GROUP, "unused")) <= 1800  # pyright: ignore[reportGeneralTypeIssues]
    await s.close()
    assert await _names(mgr) == ["used"]
    assert not await fake_redis.sismember(index_key(GROUP), "unused")  # pyright: ignore[reportGeneralTypeIssues]


async def test_group_cap_evicts_the_least_recently_used_idle_workspace(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces_per_group=2)
    holder = _session(mgr, "holder", workspace=None, cfg=cfg)
    await holder.open("busy", new=True, description="一直有人在用")
    await holder.backend.working_dir()
    idle = _session(mgr, "idle", workspace=None, cfg=cfg)
    await idle.open("idle", new=True, description="没人用了")
    await idle.backend.working_dir()
    await idle.close()
    link = LinkRecord(
        id="abcdefghij", port=8000, url="https://sbx-abcdefghij.example.com", command="", created_at=0, pgid=1
    )
    await fake_redis.hset(ws_key(GROUP, "idle"), "links", links_json([link]))  # pyright: ignore[reportGeneralTypeIssues]
    idle_claim = await _claim(fake_redis, "idle")

    s = _session(mgr, "s", workspace=None, cfg=cfg)
    opened = await s.open("third", new=True, description="第三个话题")
    assert [w.name for w in opened.evicted] == ["idle"]
    summary = await s.summary(opened)
    assert "回收了最久没用的「idle」（没人用了）" in summary
    assert "公开链接也随之失效：https://sbx-abcdefghij.example.com" in summary
    assert idle_claim not in kube.claim_names()
    assert await _names(mgr) == ["busy", "third"]
    await s.backend.working_dir()

    # Both remaining ones are in use: nothing to evict, the model is told to reuse one.
    late = _session(mgr, "late", workspace=None, cfg=cfg)
    with pytest.raises(SandboxError, match="都在使用中或有后台任务") as full:
        await late.open("fourth", new=True, description="第四个话题")
    assert "- busy：一直有人在用" in str(full.value)
    assert 'open_workspace(name="名字")' in str(full.value)
    # A background job keeps an otherwise idle workspace too.
    await s.close()
    await fake_redis.hset(keep_key(GROUP, "third"), "job:j1", "77")  # pyright: ignore[reportGeneralTypeIssues]
    with pytest.raises(SandboxError, match="都在使用中或有后台任务"):
        await late.open("fourth", new=True, description="第四个话题")
    # The caller's own workspace is not used by another invocation: it is evictable, and here the least recently
    # used one.
    await fake_redis.hdel(keep_key(GROUP, "third"), "job:j1")  # pyright: ignore[reportGeneralTypeIssues]
    mine = await holder.open("fourth", new=True, description="换个话题")
    assert [w.name for w in mine.evicted] == ["busy"]
    assert mine.left is None  # it made room: nothing of it is left behind
    assert not holder.used
    await holder.close()  # "fourth" was never used: the reservation goes
    await late.close()
    assert await _names(mgr) == ["third"]


async def test_concurrent_creates_in_two_replicas_respect_the_group_cap(
    kube_client: KubeClient, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    """Two invocations on two bot replicas (separate managers, shared Redis and API server) create a workspace each
    while the group is one below its cap: the per-group lock serializes them, so exactly one eviction happens and
    the cap holds."""
    cfg = ToolSettings(sandbox_max_workspaces_per_group=2)
    one, two = _manager(kube_client, fake_redis), _manager(kube_client, fake_redis)
    old = _session(one, "old", workspace=None, cfg=cfg)
    await old.open("old", new=True, description="旧话题")
    await old.backend.working_dir()
    await old.close()

    for m in (one, two):
        original = m.workspaces

        async def slow(group_id: int, original: Any = original) -> list[WorkspaceInfo]:
            seen = await original(group_id)
            await asyncio.sleep(0.2)  # without the lock both would act on the same snapshot
            return seen

        m.workspaces = slow  # pyright: ignore[reportAttributeAccessIssue]

    a = _session(one, "a", workspace=None, cfg=cfg)
    b = _session(two, "b", workspace=None, cfg=cfg)
    first, second = await asyncio.gather(
        a.open("x", new=True, description="话题 x"), b.open("y", new=True, description="话题 y")
    )
    assert sorted(w.name for w in [*first.evicted, *second.evicted]) == ["old"]
    assert await _names(one) == ["x", "y"]
    assert len(kube.claims()) == 0  # old's pod is gone, x and y are not created yet
    await asyncio.gather(a.backend.working_dir(), b.backend.working_dir())
    assert len(kube.claims()) == 2
    await asyncio.gather(a.close(), b.close())


async def test_eviction_skips_a_workspace_an_invocation_attached_to_meanwhile(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = ToolSettings(sandbox_max_workspaces_per_group=1)
    a = _session(mgr, "a", workspace=None, cfg=cfg)
    await a.open("solo", new=True, description="唯一的")
    await a.backend.working_dir()
    await a.close()
    stale = await mgr.workspaces(GROUP)  # says "solo" is idle
    busy = _session(mgr, "busy", workspace="solo", cfg=cfg)
    await busy.backend.working_dir()
    original = mgr.workspaces

    async def snapshot(group_id: int) -> list[WorkspaceInfo]:
        await original(group_id)
        return stale

    monkeypatch.setattr(mgr, "workspaces", snapshot)
    late = _session(mgr, "late", workspace=None, cfg=cfg)
    with pytest.raises(SandboxError, match="都在使用中"):
        await late.open("new-topic", new=True, description="新话题")
    assert await _claim(fake_redis, "solo") in kube.claim_names()
    await busy.close()
    await late.close()


async def test_extend_pushes_the_hard_cap_and_attach_honours_it(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    a = _session(mgr, "a")
    await a.backend.working_dir()
    claim = await _claim(fake_redis)
    assert claim is not None
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.max_until == pytest.approx(info.created_at + CFG.sandbox_max_age_min * 60)
    patches = kube.patches
    later = time.time() + 10 * 3600
    assert await mgr.extend(GROUP, WS, claim, later, CFG) == pytest.approx(later)
    assert kube.patches == patches + 1  # renewed right away
    assert await mgr.extend(GROUP, WS, claim, time.time() + 60, CFG) == pytest.approx(later)  # never shortened
    assert await mgr.extend(GROUP, WS, "ygg-sbx-other", later + 1, CFG) is None
    assert await mgr.extend(GROUP, "nope", claim, later + 1, CFG) is None
    await a.close()
    # Past the default max age, but the extended cap keeps it usable: no retirement on attach.
    await fake_redis.hset(ws_key(GROUP, WS), "created_at", repr(time.time() - 2 * CFG.sandbox_max_age_min * 60))  # pyright: ignore[reportGeneralTypeIssues]
    b = _session(mgr, "b")
    attachment = await b.acquire(None, lambda _ref: None)
    assert attachment.notice is None
    assert await _claim(fake_redis) == claim
    await b.close()


def test_selecting_tools_run_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_tools, "relay_enabled", lambda: True)
    tools = {t.name: t for t in sandbox_tools.bot_tools()}
    assert next(iter(tools)) == "open_workspace"
    assert {name for name, t in tools.items() if t.sequential} == {"open_workspace", "reset_workspace", "delegate"}

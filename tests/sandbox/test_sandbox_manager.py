"""WorkspaceManager / Session flows with a fake Kubernetes API, in-process Redis and an in-memory sandboxd (files
per claim): what is checked here is the manager's bookkeeping, not sandboxd itself."""

import re
import time
from typing import Any

import fakeredis
import httpx
import pytest

from tests.sandbox.helpers import FakeKube, MemorySandboxd
from yggdrasil.sandbox import SandboxError, WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.state import ws_key
from yggdrasil.settings import ToolSettings

GROUP = 10001
WS = "notes"


@pytest.fixture
def pods(monkeypatch: pytest.MonkeyPatch) -> MemorySandboxd:
    pods = MemorySandboxd()
    pods.install(monkeypatch)
    return pods


@pytest.fixture
def mgr(pods: MemorySandboxd, kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> WorkspaceManager:
    manager = WorkspaceManager(
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

    manager._healthy = healthy  # pyright: ignore[reportAttributeAccessIssue]
    return manager


def _cfg(**kw: Any) -> ToolSettings:
    return ToolSettings(**kw)


async def _reserve(
    mgr: WorkspaceManager, name: str = WS, *, group: int = GROUP, cfg: ToolSettings | None = None
) -> None:
    await mgr.reserve(group, name, "测试用", cfg or _cfg())


async def test_lazy_session_creates_renews_and_releases(mgr: WorkspaceManager, kube: FakeKube) -> None:
    session = mgr.session(GROUP, invocation_id="inv-1", cfg=_cfg())
    opened = await session.open(WS, new=True, description="测试用")
    assert opened.created
    assert not session.used
    assert kube.claims() == []
    await session.put_bytes("in-ws.txt", b"hi\n")
    assert session.used
    [claim] = kube.claims()
    name = claim["metadata"]["name"]
    assert re.fullmatch(rf"ygg-sbx-{GROUP}-{WS}-[a-z0-9]{{6}}", name)
    assert claim["metadata"]["labels"]["yggdrasil/group"] == str(GROUP)
    assert claim["metadata"]["labels"]["yggdrasil/workspace"] == WS
    assert claim["spec"]["additionalPodMetadata"]["labels"] == {"sandbox.users.io/workspace": name}
    assert claim["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.claim == name
    assert info.active == 1
    assert info.expires_at > time.time() + 29 * 60
    # A long run keeps renewing the claim from its commands, throttled.
    patches = kube.patches
    session._renewed_at -= 3600  # pyright: ignore[reportPrivateUsage] - pretend the last renewal is long past
    await session.backend.run("true", shell=True)
    assert kube.patches == patches + 1
    await session.backend.run("true", shell=True)
    assert kube.patches == patches + 1
    # A second invocation selects the same workspace and sees the file.
    other = mgr.session(GROUP, invocation_id="inv-2", cfg=_cfg())
    reused = await other.open(WS)
    summary = await other.summary(reused)
    assert "已选中工作区「notes」" in summary
    assert "in-ws.txt" in summary  # the file listing comes with the selection
    assert (await other.backend.read_bytes("/workspace/in-ws.txt")) == b"hi\n"
    assert (await mgr.get(GROUP, WS)).active == 2  # pyright: ignore[reportOptionalMemberAccess]
    await other.close()
    await session.close()
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.active == 0
    assert [w.claim for w in await mgr.list()] == [name]
    await session.backend.aclose()
    assert await mgr.destroy(GROUP, WS, reason="test")
    assert await mgr.get(GROUP, WS) is None
    assert kube.claims() == []


async def test_expired_claim_is_dropped_or_replaced(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    await _reserve(mgr)
    first = mgr.session(GROUP, invocation_id="a", cfg=_cfg(), workspace=WS)
    await first.backend.working_dir()
    await first.close()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]  # expired: the controller deleted it
    assert await fake_redis.exists(ws_key(GROUP, WS))
    assert await mgr.workspaces(GROUP) == []  # listing it drops the record: the model no longer sees it
    assert not await fake_redis.exists(ws_key(GROUP, WS))
    late = mgr.session(GROUP, invocation_id="late", cfg=_cfg(), workspace=WS)
    with pytest.raises(SandboxError, match="不存在"):
        await late.backend.working_dir()  # a report pre-selecting it: told to choose again
    assert late.name is None
    await late.close()

    await _reserve(mgr)
    second = mgr.session(GROUP, invocation_id="b", cfg=_cfg(), workspace=WS)
    await second.backend.working_dir()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]
    third = mgr.session(GROUP, invocation_id="c", cfg=_cfg(), workspace=WS)
    # second still counts as active on the vanished claim; the attaching session gets a fresh pod under the same
    # name and a notice.
    result = await third.backend.run("true", shell=True)
    assert "过期" in result.stderr
    await second.close()
    await third.close()
    assert len(kube.claims()) == 1
    assert (await mgr.get(GROUP, WS)) is not None


async def test_reset_defers_while_others_use_it(mgr: WorkspaceManager, kube: FakeKube) -> None:
    await _reserve(mgr)
    a = mgr.session(GROUP, invocation_id="a", cfg=_cfg(), workspace=WS)
    b = mgr.session(GROUP, invocation_id="b", cfg=_cfg(), workspace=WS)
    await a.backend.working_dir()
    await b.backend.working_dir()
    info = await mgr.get(GROUP, WS)
    assert info is not None
    text = await a.reset()
    assert "其他请求" in text
    assert (await mgr.get(GROUP, WS)).recreate  # pyright: ignore[reportOptionalMemberAccess]
    await b.close()
    old = info.claim
    await a.close()  # last one out applies the pending recreate
    after = await mgr.get(GROUP, WS)
    assert after is not None
    assert after.pending  # same name and description, no pod until next use
    assert all(c["metadata"]["name"] != old for c in kube.claims())

    c = mgr.session(GROUP, invocation_id="c", cfg=_cfg(), workspace=WS)
    await c.put_bytes("data/x.txt", b"x")
    assert "已清空" in await c.reset()
    assert kube.claims() == []
    # The session goes on with a fresh pod.
    await c.backend.working_dir()
    [fresh] = kube.claims()
    info = await mgr.get(GROUP, WS)
    assert info is not None
    assert info.claim == fresh["metadata"]["name"]
    assert info.active == 1
    await c.close()
    assert await mgr.destroy(GROUP, WS, reason="test")


async def test_bot_file_helpers(mgr: WorkspaceManager) -> None:
    await _reserve(mgr)
    s = mgr.session(GROUP, invocation_id="f", cfg=_cfg(), workspace=WS)
    path = await s.put_bytes("in/report.csv", b"a,b\n1,2\n")
    assert path == "/workspace/in/report.csv"
    assert await s.get_bytes("in/report.csv", max_bytes=100) == b"a,b\n1,2\n"
    with pytest.raises(SandboxError, match="太大"):
        await s.get_bytes("in/report.csv", max_bytes=3)
    with pytest.raises(SandboxError):
        await s.put_bytes("../etc/passwd", b"")
    with pytest.raises(SandboxError):
        await s.get_bytes("in", max_bytes=100)
    await s.close()
    await mgr.destroy(GROUP, WS, reason="test")


async def test_eviction_skips_active_workspaces(
    mgr: WorkspaceManager, kube: FakeKube, fake_redis: fakeredis.FakeAsyncRedis
) -> None:
    cfg = _cfg(sandbox_max_workspaces=1)
    await _reserve(mgr, cfg=cfg)
    busy = mgr.session(GROUP, invocation_id="busy", cfg=cfg, workspace=WS)
    await busy.backend.working_dir()
    await _reserve(mgr, "other", group=GROUP + 1, cfg=cfg)
    other = mgr.session(GROUP + 1, invocation_id="other", cfg=cfg, workspace="other")
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()
    await busy.close()
    await other.backend.working_dir()  # now the idle one is evicted
    assert await mgr.get(GROUP, WS) is None
    assert len(kube.claims()) == 1
    await other.close()
    assert await mgr.destroy_all(reason="test") == 1
    assert [k async for k in fake_redis.scan_iter(match="ygg:sandbox:*")] == []


async def test_command_limits_surface_as_tool_errors(mgr: WorkspaceManager) -> None:
    await _reserve(mgr)
    s = mgr.session(GROUP, invocation_id="lim", cfg=_cfg(sandbox_max_commands=1), workspace=WS)
    await s.backend.run("true", shell=True)
    with pytest.raises(SandboxError, match="上限"):
        await s.backend.run("true", shell=True)
    await s.close()
    await mgr.destroy(GROUP, WS, reason="test")

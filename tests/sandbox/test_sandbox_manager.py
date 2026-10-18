"""WorkspaceManager / Session flows against real Redis and a real sandboxd, with a fake Kubernetes API.

Needs the same YGG_TEST_SANDBOXD_* variables as the conformance suite plus a Redis at REDIS_URL (CI provides
both); skipped otherwise. The fake API answers SandboxClaim / Sandbox / Service / IngressRoute calls and
reports every claim as Ready on pod IP 127.0.0.1, which the manager maps onto the local sandboxd.
"""

import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis

from yggdrasil.sandbox import SandboxError, WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint
from yggdrasil.sandbox.k8s import KubeClient
from yggdrasil.sandbox.state import WS_KEY_PREFIX
from yggdrasil.settings import ToolSettings

_REST = os.environ.get("YGG_TEST_SANDBOXD_REST")
_GRPC = os.environ.get("YGG_TEST_SANDBOXD_GRPC")
_TOKEN = os.environ.get("YGG_TEST_SANDBOXD_TOKEN")
_REDIS = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

pytestmark = pytest.mark.skipif(
    not (_REST and _GRPC and _TOKEN),
    reason="set YGG_TEST_SANDBOXD_REST / YGG_TEST_SANDBOXD_GRPC / YGG_TEST_SANDBOXD_TOKEN to run against a sandboxd",
)

GROUP = 10001


class FakeKube:
    """Just enough of the API server: objects keyed by path, claims Ready immediately."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            name = body["metadata"]["name"]
            obj = {**body, "metadata": {**body["metadata"], "uid": f"uid-{name}"}}
            if path.endswith("/sandboxclaims"):
                obj["status"] = {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "sandbox": {"name": name, "podIPs": ["127.0.0.1"]},
                }
                sandbox_path = path.replace("extensions.agents.x-k8s.io/v1beta1", "agents.x-k8s.io/v1beta1").replace(
                    "sandboxclaims", "sandboxes"
                )
                self.objects[f"{sandbox_path}/{name}"] = {"metadata": {"name": name, "uid": f"uid-sb-{name}"}}
            self.objects[f"{path}/{name}"] = obj
            return httpx.Response(201, json=obj)
        if request.method == "GET" and path.endswith(("/sandboxclaims", "/services", "/ingressroutes")):
            items = [v for k, v in self.objects.items() if k.rsplit("/", 1)[0] == path]
            return httpx.Response(200, json={"items": items})
        obj = self.objects.get(path)
        if obj is None:
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "GET":
            return httpx.Response(200, json=obj)
        if request.method == "PATCH":
            obj["spec"]["lifecycle"].update(body["spec"]["lifecycle"])
            return httpx.Response(200, json=obj)
        if request.method == "DELETE":
            del self.objects[path]
            if "/sandboxclaims/" in path:  # the controller cascades to the Sandbox and its links
                name = path.rsplit("/", 1)[1]
                for key in [k for k in self.objects if k.endswith(f"/sandboxes/{name}")]:
                    del self.objects[key]
            return httpx.Response(200, json={})
        return httpx.Response(405)

    def claims(self) -> list[dict[str, Any]]:
        return [v for k, v in self.objects.items() if "/sandboxclaims/" in k]


@pytest.fixture
async def redis_conn() -> AsyncIterator[aioredis.Redis]:
    conn = aioredis.from_url(_REDIS, decode_responses=True)
    try:
        await conn.ping()
    except Exception:
        await conn.aclose()
        pytest.skip(f"no Redis at {_REDIS}")
    await _clean(conn)
    yield conn
    await _clean(conn)
    await conn.aclose()


async def _clean(conn: aioredis.Redis) -> None:
    async for key in conn.scan_iter(match="ygg:sandbox:*"):
        await conn.delete(key)


@pytest.fixture
def kube() -> FakeKube:
    return FakeKube()


@pytest.fixture
async def mgr(kube: FakeKube, redis_conn: aioredis.Redis, tmp_path: Path) -> AsyncIterator[WorkspaceManager]:
    assert _REST
    assert _GRPC
    token = tmp_path / "token"
    token.write_text("test")
    client = KubeClient(base_url="https://k8s.test", token_path=token, transport=httpx.MockTransport(kube.handler))
    endpoint = Endpoint(rest_url=_REST, grpc_target=_GRPC)
    manager = WorkspaceManager(
        token=_TOKEN,
        namespace="ns",
        warmpool="pool",
        link_domain="links.example.com",
        kube=client,
        redis=lambda: redis_conn,
        endpoint=lambda _ip: endpoint,
    )
    yield manager
    await client.aclose()


def _cfg(**kw: Any) -> ToolSettings:
    return ToolSettings(**kw)


async def test_lazy_session_creates_renews_and_releases(mgr: WorkspaceManager, kube: FakeKube) -> None:
    session = mgr.session(GROUP, invocation_id="inv-1", cfg=_cfg())
    assert not session.used
    assert kube.claims() == []
    result = await session.backend.run("echo hi > in-ws.txt && pwd", shell=True)
    assert result.stdout == "/workspace\n"
    assert session.used
    [claim] = kube.claims()
    name = claim["metadata"]["name"]
    assert re.fullmatch(rf"ygg-sbx-{GROUP}-[a-z0-9]{{6}}", name)
    assert claim["metadata"]["labels"]["yggdrasil/group"] == str(GROUP)
    assert claim["spec"]["additionalPodMetadata"]["labels"] == {"sandbox.users.io/workspace": name}
    assert claim["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    info = await mgr.get(GROUP)
    assert info is not None
    assert info.claim == name
    assert info.active == 1
    assert info.expires_at > time.time() + 29 * 60
    # A second invocation attaches to the same workspace and sees the file.
    other = mgr.session(GROUP, invocation_id="inv-2", cfg=_cfg())
    assert (await other.backend.read_bytes("/workspace/in-ws.txt")) == b"hi\n"
    assert (await mgr.get(GROUP)).active == 2  # pyright: ignore[reportOptionalMemberAccess]
    await other.close()
    await session.close()
    info = await mgr.get(GROUP)
    assert info is not None
    assert info.active == 0
    assert [w.claim for w in await mgr.list()] == [name]
    described = await mgr.describe(GROUP)
    assert described is not None
    assert "in-ws.txt" in described
    await session.backend.aclose()
    assert await mgr.destroy(GROUP, reason="test")
    assert await mgr.get(GROUP) is None
    assert kube.claims() == []


async def test_expired_claim_is_reported_and_replaced(
    mgr: WorkspaceManager, kube: FakeKube, redis_conn: aioredis.Redis
) -> None:
    first = mgr.session(GROUP, invocation_id="a", cfg=_cfg())
    await first.backend.working_dir()
    await first.close()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]  # expired: the controller deleted it
    assert await redis_conn.exists(f"{WS_KEY_PREFIX}{GROUP}")
    described = await mgr.describe(GROUP)
    assert described is not None
    assert "过期" in described
    assert not await redis_conn.exists(f"{WS_KEY_PREFIX}{GROUP}")

    second = mgr.session(GROUP, invocation_id="b", cfg=_cfg())
    await second.backend.working_dir()
    for key in [k for k in kube.objects if "/sandboxclaims/" in k]:
        del kube.objects[key]
    third = mgr.session(GROUP, invocation_id="c", cfg=_cfg())
    # second still counts as active on the vanished claim; the new session gets a new workspace and a notice.
    result = await third.backend.run("true", shell=True)
    assert "过期" in result.stderr
    await second.close()
    await third.close()
    assert len(kube.claims()) == 1


async def test_destroy_defers_while_active_and_reset(mgr: WorkspaceManager, kube: FakeKube) -> None:
    a = mgr.session(GROUP, invocation_id="a", cfg=_cfg())
    b = mgr.session(GROUP, invocation_id="b", cfg=_cfg())
    await a.backend.working_dir()
    await b.backend.working_dir()
    assert not await mgr.destroy(GROUP, reason="admin")
    info = await mgr.get(GROUP)
    assert info is not None
    assert info.recreate
    text = await a.reset()
    assert "其他请求" in text
    await b.close()
    old = info.claim
    await a.close()  # last one out applies the pending recreate
    assert await mgr.get(GROUP) is None
    assert all(c["metadata"]["name"] != old for c in kube.claims())

    c = mgr.session(GROUP, invocation_id="c", cfg=_cfg())
    await c.put_bytes("data/x.txt", b"x")
    assert "清空" in await c.reset()
    assert kube.claims() == []
    # The session goes on with a fresh workspace (the local sandboxd is shared, so only the claim is new).
    await c.backend.working_dir()
    [fresh] = kube.claims()
    info = await mgr.get(GROUP)
    assert info is not None
    assert info.claim == fresh["metadata"]["name"]
    assert info.active == 1
    await c.close()
    assert await mgr.destroy(GROUP, reason="test")


async def test_bot_file_helpers(mgr: WorkspaceManager) -> None:
    s = mgr.session(GROUP, invocation_id="f", cfg=_cfg())
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
    await mgr.destroy(GROUP, reason="test")


async def test_eviction_skips_active_workspaces(mgr: WorkspaceManager, kube: FakeKube) -> None:
    cfg = _cfg(sandbox_max_workspaces=1)
    busy = mgr.session(GROUP, invocation_id="busy", cfg=cfg)
    await busy.backend.working_dir()
    other = mgr.session(GROUP + 1, invocation_id="other", cfg=cfg)
    with pytest.raises(SandboxError, match="上限"):
        await other.backend.working_dir()
    await busy.close()
    await other.backend.working_dir()  # now the idle one is evicted
    assert await mgr.get(GROUP) is None
    assert len(kube.claims()) == 1
    await other.close()
    assert await mgr.destroy_all(reason="test") == 1


async def test_command_limits_surface_as_tool_errors(mgr: WorkspaceManager) -> None:
    s = mgr.session(GROUP, invocation_id="lim", cfg=_cfg(sandbox_max_commands=1))
    await s.backend.run("true", shell=True)
    with pytest.raises(SandboxError, match="上限"):
        await s.backend.run("true", shell=True)
    await s.close()
    await mgr.destroy(GROUP, reason="test")


async def test_serve_validation_and_link_lifecycle(mgr: WorkspaceManager, kube: FakeKube) -> None:
    s = mgr.session(GROUP, invocation_id="srv", cfg=_cfg(sandbox_max_links=1))
    with pytest.raises(SandboxError):
        await s.serve("true", 8080)
    with pytest.raises(SandboxError):
        await s.serve("true", 80)
    if (await s.backend.run(["sh", "-c", "command -v python3"])).exit_code != 0:
        await s.close()
        await mgr.destroy(GROUP, reason="test")
        pytest.skip("the sandbox image has no python3 to serve with")
    with pytest.raises(SandboxError, match="日志"):
        await s.serve("echo broken >&2; exit 1", 8001)
    link = await s.serve("python3 -m http.server 8000 --bind 0.0.0.0", 8000)
    assert re.fullmatch(r"https://[a-z0-9]{10}\.links\.example\.com", link.url)
    rid = link.url.removeprefix("https://").split(".", 1)[0]
    service = kube.objects[f"/api/v1/namespaces/ns/services/sbx-{rid}"]
    claim = kube.claims()[0]["metadata"]["name"]
    assert service["spec"]["selector"] == {"sandbox.users.io/workspace": claim}
    assert service["spec"]["ports"][0]["targetPort"] == 8000
    assert service["metadata"]["ownerReferences"][0]["kind"] == "Sandbox"
    route = kube.objects[f"/apis/traefik.io/v1alpha1/namespaces/ns/ingressroutes/sbx-{rid}"]
    assert route["spec"]["routes"][0]["match"] == f"Host(`{rid}.links.example.com`)"
    with pytest.raises(SandboxError, match="最多"):
        await s.serve("python3 -m http.server 8002", 8002)
    info = await mgr.get(GROUP)
    assert info is not None
    assert [x.port for x in info.links] == [8000]
    # With a live link the claim lives to its max age.
    assert info.expires_at > time.time() + 200 * 60
    await s.close()  # reap keeps the served process group
    probe = mgr.session(GROUP, invocation_id="probe", cfg=_cfg())
    alive = await probe.backend.run(["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8000"])
    assert alive.exit_code == 0
    assert await probe.unserve(8000)
    assert not await probe.unserve(8000)
    assert f"/api/v1/namespaces/ns/services/sbx-{rid}" not in kube.objects
    dead = await probe.backend.run(["bash", "-c", "sleep 1.5; exec 3<>/dev/tcp/127.0.0.1/8000"])
    assert dead.exit_code != 0
    await probe.close()
    await mgr.destroy(GROUP, reason="test")

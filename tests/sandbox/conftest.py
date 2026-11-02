from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest

from tests.sandbox.helpers import SANDBOXD_GRPC, SANDBOXD_REST, SANDBOXD_TOKEN, FakeKube
from yggdrasil.sandbox import WorkspaceManager
from yggdrasil.sandbox.backend import Endpoint, SandboxdWorkspace, StaticResolver
from yggdrasil.sandbox.k8s import KubeClient

_sandboxd_down: list[str] = []


@pytest.fixture
async def sandboxd() -> Endpoint:
    """The local sandboxd, checked before every test that uses it: once it stops answering, the remaining
    tests fail at once instead of each spending its retries (seconds per operation) against a dead daemon."""
    assert SANDBOXD_REST
    assert SANDBOXD_GRPC
    if _sandboxd_down:
        pytest.fail(_sandboxd_down[0])
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            healthy = (await http.get(f"{SANDBOXD_REST}/v1/health")).status_code == 200
    except httpx.HTTPError as e:
        healthy, why = False, repr(e)
    else:
        why = "unhealthy"
    if not healthy:
        _sandboxd_down.append(f"sandboxd at {SANDBOXD_REST} is not answering /v1/health ({why})")
        pytest.fail(_sandboxd_down[0])
    return Endpoint(rest_url=SANDBOXD_REST, grpc_target=SANDBOXD_GRPC)


@pytest.fixture
async def sandbox_ws(sandboxd: Endpoint) -> AsyncIterator[SandboxdWorkspace]:
    """A backend on the local sandboxd for running the in-sandbox helpers (/opt/ygg/*) directly."""
    assert SANDBOXD_TOKEN
    ws = SandboxdWorkspace(resolver=StaticResolver(sandboxd, "helpers"), token=SANDBOXD_TOKEN)
    yield ws
    await ws.aclose()


@pytest.fixture
def kube() -> FakeKube:
    return FakeKube()


@pytest.fixture
async def kube_client(kube: FakeKube, tmp_path: Path) -> AsyncIterator[KubeClient]:
    token = tmp_path / "token"
    token.write_text("test")
    client = KubeClient(base_url="https://k8s.test", token_path=token, transport=httpx.MockTransport(kube.handler))
    yield client
    await client.aclose()


@pytest.fixture
def make_manager(kube_client: KubeClient, fake_redis: fakeredis.FakeAsyncRedis) -> Callable[..., WorkspaceManager]:
    """``make_manager(link_domain=None, redis=None)``: a ``WorkspaceManager`` on the fake API server and ``redis``
    (default the test's fake Redis), its pods always healthy."""

    async def healthy(_http: httpx.AsyncClient, _ip: str) -> bool:
        return True

    def make(*, link_domain: str | None = None, redis: Any = None) -> WorkspaceManager:
        return WorkspaceManager(
            token="test",
            namespace="ns",
            warmpool="pool",
            link_domain=link_domain,
            kube=kube_client,
            redis=lambda: redis or fake_redis,
            endpoint=lambda _ip: Endpoint("http://127.0.0.1:9", "127.0.0.1:9"),
            healthy=healthy,
        )

    return make

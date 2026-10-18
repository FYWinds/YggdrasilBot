from collections.abc import AsyncIterator
from pathlib import Path

import fakeredis
import httpx
import pytest

from tests.sandbox.helpers import SANDBOXD_GRPC, SANDBOXD_REST, FakeKube
from yggdrasil.sandbox.backend import Endpoint
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
async def fake_redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    # In-process Redis (Lua via lupa): the tests never touch a shared server.
    conn = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield conn
    await conn.aclose()

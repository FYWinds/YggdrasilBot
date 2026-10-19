"""``SandboxdWorkspace.exec(once=True)``: commands that must not run twice (``sh`` / ``py`` runs, job starts, writes
into omp's stdin) are retried only while the connection is not established, never after they were sent."""

from collections.abc import Callable
from typing import Any, cast

import grpc
import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.sandbox_tools import sh
from yggdrasil.sandbox import SandboxToolError
from yggdrasil.sandbox import backend as backend_mod
from yggdrasil.sandbox.backend import UNKNOWN_OUTCOME, Endpoint, SandboxdWorkspace, StaticResolver, _Conn
from yggdrasil.settings import Settings

ENDPOINT = Endpoint(rest_url="http://sandboxd.test", grpc_target="sandboxd.test:9090")


class FakeChannel:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.waits = 0

    async def channel_ready(self) -> None:
        self.waits += 1
        if not self.ready:
            import asyncio

            await asyncio.Event().wait()

    async def close(self) -> None:
        return None


class Response:
    def __init__(self, exit_code: int, stdout: bytes = b"") -> None:
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, b""


def unavailable() -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata(), "reset")


class FakeStub:
    """Execute: ``fail(argv)`` decides per command whether the connection drops (UNAVAILABLE)."""

    def __init__(self, fail: Callable[[list[str]], bool]) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def Execute(self, request: Any, **_: Any) -> Response:
        argv = list(request.config.command)
        self.calls.append(argv)
        if self.fail(argv):
            raise unavailable()
        return Response(0)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_mod, "_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(backend_mod, "_READY_WAIT", 0.05)


def workspace(stub: FakeStub, channel: FakeChannel, monkeypatch: pytest.MonkeyPatch) -> SandboxdWorkspace:
    http = httpx.AsyncClient(base_url=ENDPOINT.rest_url, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(
        SandboxdWorkspace, "_connect", lambda self, ep: _Conn(ep, cast(Any, channel), cast(Any, stub), http)
    )
    return SandboxdWorkspace(resolver=StaticResolver(ENDPOINT, "ws-1"), token="t")


async def test_once_is_not_resent_after_the_connection_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = FakeStub(lambda argv: True)
    ws = workspace(stub, FakeChannel(), monkeypatch)
    with pytest.raises(SandboxToolError, match="结果未知"):
        await ws.exec(["/opt/ygg/ygg", "sh", "start", "/workspace/.ygg/jobs/x"], once=True)
    assert len(stub.calls) == 1


async def test_idempotent_helpers_are_still_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = FakeStub(lambda argv: True)
    ws = workspace(stub, FakeChannel(), monkeypatch)
    with pytest.raises(SandboxToolError, match="连不上"):
        await ws.exec(["cat", "/workspace/a"])
    assert len(stub.calls) == 3  # two retries


async def test_once_waits_for_the_connection_and_never_sends_while_down(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = FakeStub(lambda argv: False)
    channel = FakeChannel(ready=False)
    ws = workspace(stub, channel, monkeypatch)
    with pytest.raises(SandboxToolError, match="连不上"):
        await ws.exec(["/opt/ygg/ygg", "rpc", "send", "/d", "--json", "{}"], once=True)
    assert stub.calls == []
    assert channel.waits == 3  # retried while not even connected


async def test_sh_reports_an_unknown_outcome_instead_of_running_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = FakeStub(lambda argv: argv[:3] == ["/opt/ygg/ygg", "sh", "run"])
    ws = workspace(stub, FakeChannel(), monkeypatch)
    settings = Settings.model_validate({"moderation": {"enabled": False}})
    deps = Deps(cast(Any, None), 10001, 2, settings, Handles(), "chat")
    deps.sandbox = cast(Any, type("S", (), {"backend": ws})())
    text = str(await sh(RunContext(deps=deps, model=TestModel(), usage=RunUsage()), "touch /workspace/once"))
    assert UNKNOWN_OUTCOME in text
    assert sum(1 for argv in stub.calls if argv[:3] == ["/opt/ygg/ygg", "sh", "run"]) == 1

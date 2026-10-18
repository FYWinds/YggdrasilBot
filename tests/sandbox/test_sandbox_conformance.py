"""pydantic-ai's WorkspaceBackendSuite against a real sandboxd.

Point it at one with YGG_TEST_SANDBOXD_REST (e.g. http://127.0.0.1:18080), YGG_TEST_SANDBOXD_GRPC
(127.0.0.1:19090) and YGG_TEST_SANDBOXD_TOKEN (the raw token whose sha256 sandboxd was started with);
skipped when unset. CI starts the `sandboxd` target of sandbox/Dockerfile for this.

The suite marks its tests `anyio`. With this repo's pytest-asyncio auto mode, pytest-asyncio claims every
coroutine test first and runs it on the shared session loop, so the anyio marker is inert here (no double run);
`anyio_backend` is still pinned to asyncio for runs with pytest-asyncio disabled (`-p no:asyncio`).
"""

import os
from collections.abc import AsyncIterator, Callable

import pytest
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from yggdrasil.sandbox.backend import Endpoint, SandboxdWorkspace, StaticResolver

_REST = os.environ.get("YGG_TEST_SANDBOXD_REST")
_GRPC = os.environ.get("YGG_TEST_SANDBOXD_GRPC")
_TOKEN = os.environ.get("YGG_TEST_SANDBOXD_TOKEN")

pytestmark = pytest.mark.skipif(
    not (_REST and _GRPC and _TOKEN),
    reason="set YGG_TEST_SANDBOXD_REST / YGG_TEST_SANDBOXD_GRPC / YGG_TEST_SANDBOXD_TOKEN to run against a sandboxd",
)


def _make(ref: WorkspaceRef | None = None) -> SandboxdWorkspace:
    assert _REST
    assert _GRPC
    assert _TOKEN
    resolver = StaticResolver(Endpoint(rest_url=_REST, grpc_target=_GRPC), "conformance")
    return SandboxdWorkspace(resolver=resolver, token=_TOKEN, ref=ref)


class TestSandboxdWorkspace(WorkspaceBackendSuite):
    @pytest.fixture(scope="session")
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture
    async def backend(self) -> AsyncIterator[SandboxdWorkspace]:
        backend = _make()
        yield backend
        await backend.aclose()

    @pytest.fixture
    async def _made(self) -> AsyncIterator[list[SandboxdWorkspace]]:
        made: list[SandboxdWorkspace] = []
        yield made
        for backend in made:
            await backend.aclose()

    @pytest.fixture
    def fresh_backend(self, _made: list[SandboxdWorkspace]) -> Callable[[], SandboxdWorkspace]:
        # A fresh backend resolves the same sandboxd, as a group's second invocation does.
        def factory() -> SandboxdWorkspace:
            _made.append(backend := _make())
            return backend

        return factory

    @pytest.fixture
    def destructive_backend(self) -> None:
        # Destroy rules need a disposable sandbox per test (the cluster checks cover expiry); never destroy the
        # shared sandboxd the other rules run against.
        return None

    @pytest.fixture
    def attach_backend(self, _made: list[SandboxdWorkspace]) -> Callable[[WorkspaceRef], SandboxdWorkspace]:
        def factory(ref: WorkspaceRef) -> SandboxdWorkspace:
            _made.append(backend := _make(ref))
            return backend

        return factory

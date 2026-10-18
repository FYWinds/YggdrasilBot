"""pydantic-ai's WorkspaceBackendSuite against a real sandboxd.

Point it at one with YGG_TEST_SANDBOXD_REST (e.g. http://127.0.0.1:18080), YGG_TEST_SANDBOXD_GRPC
(127.0.0.1:19090) and YGG_TEST_SANDBOXD_TOKEN (the raw token whose sha256 sandboxd was started with);
skipped when unset. CI starts the `sandboxd` target of sandbox/Dockerfile for this.

The suite marks its tests `anyio`. With this repo's pytest-asyncio auto mode, pytest-asyncio claims every
coroutine test first and runs it on the shared session loop, so the anyio marker is inert here (no double run);
`anyio_backend` is still pinned to asyncio for runs with pytest-asyncio disabled (`-p no:asyncio`).
"""

from collections.abc import AsyncIterator, Callable

import pytest
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from tests.sandbox.helpers import HAVE_SANDBOXD, NO_SANDBOXD, SANDBOXD_TOKEN
from yggdrasil.sandbox.backend import CommandLimits, Endpoint, SandboxdWorkspace, StaticResolver

pytestmark = pytest.mark.skipif(not HAVE_SANDBOXD, reason=NO_SANDBOXD)

# A hang guard, not a limit under test: every command without its own timeout gets a 120s deadline, so a lost
# exit status fails one rule instead of stalling the run (no rule needs a command to outlive it).
_HANG_GUARD = CommandLimits(max_commands=1_000_000, invocation_s=1e9, command_max_s=120.0, default_timeout_s=120.0)


def _make(endpoint: Endpoint, ref: WorkspaceRef | None = None) -> SandboxdWorkspace:
    assert SANDBOXD_TOKEN
    resolver = StaticResolver(endpoint, "conformance")
    return SandboxdWorkspace(resolver=resolver, token=SANDBOXD_TOKEN, ref=ref, limits=_HANG_GUARD)


class TestSandboxdWorkspace(WorkspaceBackendSuite):
    @pytest.fixture(scope="session")
    def anyio_backend(self) -> str:
        return "asyncio"

    @pytest.fixture
    async def backend(self, sandboxd: Endpoint) -> AsyncIterator[SandboxdWorkspace]:
        backend = _make(sandboxd)
        yield backend
        await backend.aclose()

    @pytest.fixture
    async def _made(self) -> AsyncIterator[list[SandboxdWorkspace]]:
        made: list[SandboxdWorkspace] = []
        yield made
        for backend in made:
            await backend.aclose()

    @pytest.fixture
    def fresh_backend(self, sandboxd: Endpoint, _made: list[SandboxdWorkspace]) -> Callable[[], SandboxdWorkspace]:
        # A fresh backend resolves the same sandboxd, as a group's second invocation does.
        def factory() -> SandboxdWorkspace:
            _made.append(backend := _make(sandboxd))
            return backend

        return factory

    @pytest.fixture
    def destructive_backend(self) -> None:
        # Destroy rules need a disposable sandbox per test (the cluster checks cover expiry); never destroy the
        # shared sandboxd the other rules run against.
        return None

    @pytest.fixture
    def attach_backend(
        self, sandboxd: Endpoint, _made: list[SandboxdWorkspace]
    ) -> Callable[[WorkspaceRef], SandboxdWorkspace]:
        def factory(ref: WorkspaceRef) -> SandboxdWorkspace:
            _made.append(backend := _make(sandboxd, ref))
            return backend

        return factory

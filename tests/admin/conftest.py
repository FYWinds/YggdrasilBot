from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI

from tests.admin.helpers import ADMIN
from yggdrasil import runtime
from yggdrasil.admin import mount
from yggdrasil.config import APP_CONFIG


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture reload notifications instead of reloading a running bot."""
    reasons: list[str] = []

    async def _publish(reason: str) -> None:
        reasons.append(reason)

    monkeypatch.setattr(runtime, "publish_change", _publish)
    return reasons


@pytest.fixture
def prod(monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]) -> None:
    monkeypatch.setattr(APP_CONFIG, "environment", "prod")
    snapshot(admin_emails=frozenset({ADMIN}))


@pytest.fixture
async def client(published: list[str], prod: None) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    mount(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={APP_CONFIG.auth_email_header: ADMIN}
    ) as c:
        yield c

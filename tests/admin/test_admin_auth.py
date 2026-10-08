from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.admin.conftest import ADMIN
from yggdrasil.admin import mount
from yggdrasil.config import APP_CONFIG

HEADER = APP_CONFIG.auth_email_header


async def test_missing_header_is_401_outside_dev(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/me", headers={HEADER: ""})
    assert resp.status_code == 401


async def test_non_admin_is_403(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/me", headers={HEADER: "stranger@example.invalid"})
    assert resp.status_code == 403


async def test_admin_email_is_case_insensitive(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/me", headers={HEADER: ADMIN.upper()})
    assert resp.status_code == 200
    assert resp.json()["email"] == ADMIN


async def test_dev_falls_back_to_dev_email(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(APP_CONFIG, "environment", "dev")
    monkeypatch.setattr(APP_CONFIG, "dev_admin_email", "Dev@Example.invalid")
    resp = await client.get("/api/me", headers={HEADER: ""})
    assert resp.status_code == 200
    assert resp.json()["email"] == "dev@example.invalid"
    # A forwarded identity still wins over the fallback and is still checked.
    assert (await client.get("/api/me", headers={HEADER: "stranger@example.invalid"})).status_code == 403


async def test_dev_without_dev_email_still_requires_login(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(APP_CONFIG, "environment", "dev")
    monkeypatch.setattr(APP_CONFIG, "dev_admin_email", None)
    assert (await client.get("/api/me", headers={HEADER: ""})).status_code == 401


async def test_prod_ignores_dev_email(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(APP_CONFIG, "dev_admin_email", "dev@example.invalid")
    assert (await client.get("/api/me", headers={HEADER: ""})).status_code == 401
    assert (await client.get("/api/me", headers={HEADER: "dev@example.invalid"})).status_code == 403


async def test_spa_fallback_does_not_shadow_api_or_escape_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prod: None
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(APP_CONFIG, "web_dist", str(dist))
    app = FastAPI()
    mount(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/")).text == "<html>spa</html>"
        assert (await c.get("/settings/groups")).text == "<html>spa</html>"
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        assert (await c.get("/assets/missing-abc123.js")).status_code == 404
        assert (await c.get("/api/no-such-endpoint", headers={HEADER: ADMIN})).status_code == 404
        assert (await c.get("/onebot/v11/ws")).status_code == 404
        assert "secret" not in (await c.get("/%2E%2E/secret.txt")).text

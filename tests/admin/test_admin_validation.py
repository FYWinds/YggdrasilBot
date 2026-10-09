import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, select

from tests.admin.conftest import ADMIN
from yggdrasil.db.models import CommandTrigger, ConfigAudit, Group, Provider, SettingsDoc
from yggdrasil.db.session import get_session

VALID_TRIGGER = {"handler": "tldr", "match_type": "regex", "patterns": [r"总结最近(\d+)条"], "arg_template": "-n {0}"}


@pytest.fixture
async def cleanup_audit() -> AsyncIterator[None]:
    yield
    async with get_session() as s:
        await s.execute(delete(ConfigAudit).where(ConfigAudit.actor == ADMIN))


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"patterns": ["总结(最近"]}, "无法编译"),
        ({"allowed_roles": ["member", "moderator"]}, "未知角色"),
        ({"match_type": "fuzzy"}, "未知匹配方式"),
        ({"target": "everyone"}, "未知目标"),
        ({"handler": "rm_rf"}, "未知 handler"),
        ({"scope": "group"}, "group_id"),
        ({"scope": "global", "group_id": 123}, "group_id"),
        ({"patterns": ["  "]}, "至少需要一个 pattern"),
        ({"arg_template": "-n {user}"}, "参数模板"),
        ({"cooldown_s": -1}, "greater than or equal"),
    ],
)
async def test_trigger_validation_rejects(
    client: httpx.AsyncClient, published: list[str], patch: dict, needle: str
) -> None:
    resp = await client.post("/api/triggers", json={**VALID_TRIGGER, **patch})
    assert resp.status_code == 422, resp.text
    assert needle in resp.text
    assert published == []


async def test_trigger_roundtrip_and_test_endpoint_unaffected_by_unknown_stored_handler(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None
) -> None:
    resp = await client.post("/api/triggers", json={**VALID_TRIGGER, "allowed_roles": ["admin", "admin"]})
    assert resp.status_code == 200, resp.text
    rule = resp.json()
    try:
        assert rule["allowed_roles"] == ["admin"]
        assert published == [f"trigger.create {rule['id']}"]
        # Rows written by other tools (e.g. seeds with newer handlers) must still be listable.
        async with get_session() as s:
            row = await s.get(CommandTrigger, rule["id"])
            assert row is not None
            row.handler = "future.handler"
        listed = await client.get("/api/triggers")
        assert listed.status_code == 200
        assert any(t["id"] == rule["id"] and t["handler"] == "future.handler" for t in listed.json())
    finally:
        async with get_session() as s:
            await s.execute(delete(CommandTrigger).where(CommandTrigger.id == rule["id"]))


@pytest.mark.parametrize(
    "doc",
    [
        {"interaction": {"message_interval_s": "soon"}},
        {"interaction": {"message_interval_s": -1}},
        {"interaction": {"forward_render": "html"}},
        {"interaction": {"metadata_fields": ["model", "password"]}},
        {"moderation": {"category_rules": {"Jailbreak": {"threshold": 2}}}},
        {"persona": "ygg"},
        {"interaction": {"ack_delay": 1}},  # typo: would be silently ignored without the unknown-key check
        {"moderation": {"category_rules": {"Jailbreak": {"treshold": 0.3}}}},
        {"nonsense": True},
    ],
)
async def test_global_settings_put_rejects_invalid(client: httpx.AsyncClient, published: list[str], doc: dict) -> None:
    async with get_session() as s:
        before = await s.get(SettingsDoc, "global")
        before_value = dict(before.value) if before else None
    resp = await client.put("/api/settings/global", json=doc)
    assert resp.status_code == 422, resp.text
    assert published == []
    async with get_session() as s:
        after = await s.get(SettingsDoc, "global")
        assert (dict(after.value) if after else None) == before_value


async def test_global_settings_roundtrip_is_sparse(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None
) -> None:
    async with get_session() as s:
        before = await s.get(SettingsDoc, "global")
        before_value = dict(before.value) if before else None
    try:
        resp = await client.put("/api/settings/global", json={"interaction": {"message_interval_s": 0.5}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["value"] == {"interaction": {"message_interval_s": 0.5}}
        assert body["effective"]["interaction"]["message_interval_s"] == 0.5
        # Untouched keys keep their defaults rather than being frozen into the document.
        assert (
            body["effective"]["interaction"]["message_max_chars"]
            == body["defaults"]["interaction"]["message_max_chars"]
        )
        assert published == ["settings.update global"]
        got = (await client.get("/api/settings/global")).json()
        assert got["value"] == {"interaction": {"message_interval_s": 0.5}}
    finally:
        async with get_session() as s:
            doc = await s.get(SettingsDoc, "global")
            if before_value is None:
                if doc is not None:
                    await s.delete(doc)
            elif doc is not None:
                doc.value = before_value


async def test_group_settings_validated_over_global(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None
) -> None:
    gid = 900_000_000_000 + uuid.uuid4().int % 1_000_000
    try:
        assert (await client.put(f"/api/groups/{gid}", json={"name": "t", "enabled": False})).status_code == 200
        bad = await client.put(f"/api/settings/groups/{gid}", json={"ratelimit": {"user_per_minute": "many"}})
        assert bad.status_code == 422
        bad_upsert = await client.put(
            f"/api/groups/{gid}", json={"name": "t", "enabled": True, "settings": {"context": {"recent_messages": 999}}}
        )
        assert bad_upsert.status_code == 422
        ok = await client.put(f"/api/settings/groups/{gid}", json={"persona": {"name": "小树"}})
        assert ok.status_code == 200
        assert ok.json()["effective"]["persona"]["name"] == "小树"
        async with get_session() as s:
            g = await s.get(Group, gid)
            assert g is not None
            assert g.enabled is False
            assert g.settings == {"persona": {"name": "小树"}}
    finally:
        async with get_session() as s:
            await s.execute(delete(Group).where(Group.group_id == gid))


async def test_provider_key_is_never_returned(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None
) -> None:
    secret = f"sk-{uuid.uuid4().hex}"
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/providers",
        json={"name": name, "base_url": "http://127.0.0.1:9/v1/", "api_key": secret, "api_format": "anthropic"},
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]
    try:
        assert secret not in resp.text
        assert resp.json()["api_key_masked"] == f"…{secret[-4:]}"
        assert resp.json()["base_url"] == "http://127.0.0.1:9/v1"
        assert resp.json()["api_format"] == "anthropic"

        bad_format = await client.put(
            f"/api/providers/{pid}", json={"name": name, "base_url": "http://127.0.0.1:9/v1", "api_format": "grpc"}
        )
        assert bad_format.status_code == 422

        # Updating without a key keeps the stored one.
        upd = await client.put(f"/api/providers/{pid}", json={"name": name, "base_url": "http://127.0.0.1:9/v1"})
        assert upd.status_code == 200
        assert upd.json()["api_key_masked"] == f"…{secret[-4:]}"
        assert upd.json()["api_format"] == "openai"

        listed = await client.get("/api/providers")
        assert secret not in listed.text
        async with get_session() as s:
            row = await s.scalar(select(Provider).where(Provider.id == pid))
            assert row is not None
            assert row.api_key_enc is not None
            assert secret.encode() not in row.api_key_enc
        audits = await _audit_details(pid)
        assert audits
        assert all(secret not in str(d) for d in audits)

        cleared = await client.put(
            f"/api/providers/{pid}", json={"name": name, "base_url": "http://127.0.0.1:9/v1", "clear_api_key": True}
        )
        assert cleared.json()["api_key_masked"] is None
    finally:
        async with get_session() as s:
            await s.execute(delete(Provider).where(Provider.id == pid))


async def _audit_details(provider_id: int) -> list[dict]:
    async with get_session() as s:
        rows = await s.scalars(
            select(ConfigAudit.detail).where(
                ConfigAudit.entity == "provider", ConfigAudit.entity_id == str(provider_id)
            )
        )
        return list(rows.all())

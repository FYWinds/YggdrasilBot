import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sqlalchemy import delete, select

from tests.admin.conftest import ADMIN
from yggdrasil.admin import providers as providers_api
from yggdrasil.db.models import CommandTrigger, ConfigAudit, Group, Provider, SearchProvider, SettingsDoc, TaskRoute
from yggdrasil.db.session import get_session
from yggdrasil.llm.catalog import Catalog

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
        {"interaction": {"forward_max_nodes": "many"}},
        {"interaction": {"forward_max_nodes": 0}},
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
        resp = await client.put("/api/settings/global", json={"interaction": {"forward_max_nodes": 8}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["value"] == {"interaction": {"forward_max_nodes": 8}}
        assert body["effective"]["interaction"]["forward_max_nodes"] == 8
        # Untouched keys keep their defaults rather than being frozen into the document.
        assert body["effective"]["interaction"]["node_max_chars"] == body["defaults"]["interaction"]["node_max_chars"]
        assert published == ["settings.update global"]
        got = (await client.get("/api/settings/global")).json()
        assert got["value"] == {"interaction": {"forward_max_nodes": 8}}
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


async def test_search_provider_key_rules_and_default_endpoint(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None
) -> None:
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    secret = f"exa-{uuid.uuid4().hex}"
    for keyed in ("exa", "deepseek"):
        keyless = await client.post("/api/search-providers", json={"name": name, "kind": keyed})
        assert keyless.status_code == 422
    no_url = await client.post("/api/search-providers", json={"name": name, "kind": "searxng"})
    assert no_url.status_code == 422

    resp = await client.post(
        "/api/search-providers", json={"name": name, "kind": "exa", "api_key": secret, "monthly_limit": 1300}
    )
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    try:
        assert secret not in resp.text
        assert resp.json()["base_url"] == "https://api.exa.ai"
        assert resp.json()["api_key_masked"] == f"…{secret[-4:]}"
        # Clearing the only key of an Exa provider is refused; the stored key survives.
        cleared = await client.put(
            f"/api/search-providers/{sid}", json={"name": name, "kind": "exa", "clear_api_key": True}
        )
        assert cleared.status_code == 422
        listed = await client.get("/api/search-providers")
        row = next(p for p in listed.json() if p["id"] == sid)
        assert row["api_key_masked"] == f"…{secret[-4:]}"
        assert row["monthly_limit"] == 1300
        assert secret not in listed.text
    finally:
        async with get_session() as s:
            await s.execute(delete(SearchProvider).where(SearchProvider.id == sid))


async def _audit_details(provider_id: int) -> list[dict]:
    async with get_session() as s:
        rows = await s.scalars(
            select(ConfigAudit.detail).where(
                ConfigAudit.entity == "provider", ConfigAudit.entity_id == str(provider_id)
            )
        )
        return list(rows.all())


_CATALOG = Catalog(
    {
        "anthropic": {"models": {"claude-x": {"modalities": {"input": ["text", "image"]}, "tool_call": True}}},
        "acme": {"models": {"text-only": {"modalities": {"input": ["text"]}, "tool_call": False}}},
    }
)


async def test_aliases_resolve_caps_and_routes_reject_incapable_models(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _load() -> Catalog:
        return _CATALOG

    monkeypatch.setattr(providers_api, "load_catalog", _load)
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/api/providers", json={"name": name, "base_url": "http://127.0.0.1:9/v1"})
    pid = resp.json()["id"]
    try:
        async with get_session() as s:
            row = await s.get(Provider, pid)
            assert row is not None
            row.models = ["gw/claude-x", "gw/text-only", "house-model"]
        res = await client.put(
            f"/api/providers/{pid}/aliases", json={"aliases": {"house-model": "anthropic/claude-x", "ghost": "no/such"}}
        )
        assert res.status_code == 200, res.text
        body = res.json()
        caps = body["provider"]["model_caps"]
        assert set(caps) == {"gw/claude-x", "gw/text-only", "house-model"}
        assert caps["house-model"]["vision"] is True
        assert body["unmatched"] == ["ghost"]

        before = await _route("vision")
        bad = await client.put("/api/routes/vision", json={"provider_id": pid, "model": "gw/text-only"})
        assert bad.status_code == 422
        assert "不支持看图" in bad.text
        bad_fb = await client.put(
            "/api/routes/chat",
            json={
                "provider_id": pid,
                "model": "gw/claude-x",
                "fallback_provider_id": pid,
                "fallback_model": "gw/text-only",
            },
        )
        assert bad_fb.status_code == 422
        assert "不支持工具调用" in bad_fb.text
        assert await _route("vision") == before
    finally:
        async with get_session() as s:
            await s.execute(delete(Provider).where(Provider.id == pid))


async def _route(task: str) -> tuple[int, str] | None:
    async with get_session() as s:
        r = await s.get(TaskRoute, task)
        return (r.provider_id, r.model) if r else None


def _arch_model(mid: str, inputs: list[str], outputs: list[str], params: list[str], **extra: object) -> dict:
    return {
        "id": mid,
        "architecture": {"input_modalities": inputs, "output_modalities": outputs},
        "supported_parameters": params,
        **extra,
    }


_OR_TEXT = [
    _arch_model(
        "openai/gpt-x",
        ["text", "image", "file"],
        ["text"],
        ["tools", "include_reasoning"],
        context_length=400000,
        top_provider={"max_completion_tokens": 128000},
    ),
    _arch_model("acme/plain", ["text"], ["text"], ["temperature"], context_length=None, top_provider=None),
    _arch_model("google/gemini-img", ["text", "image"], ["text", "image"], ["tools"]),
]
_OR_IMAGE = [
    _arch_model("google/gemini-img", ["text", "image"], ["text", "image"], ["tools"]),
    _arch_model("openai/gpt-image-z", ["text", "image"], ["image"], []),
]


def _mock_upstream(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], dict]) -> list[httpx.Request]:
    """Answer every outgoing (non-ASGI) request with ``handler``'s JSON; returns the seen requests."""
    seen: list[httpx.Request] = []

    async def upstream(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=handler(request))

    async def _load() -> Catalog:
        return _CATALOG

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", upstream)
    monkeypatch.setattr(providers_api, "load_catalog", _load)
    return seen


async def _snapshot_route(task: str) -> dict | None:
    async with get_session() as s:
        r = await s.get(TaskRoute, task)
        if r is None:
            return None
        keys = ("provider_id", "model", "params", "fallback_provider_id", "fallback_model", "limits")
        return {k: getattr(r, k) for k in keys}


async def _restore_route(task: str, snap: dict | None) -> None:
    async with get_session() as s:
        r = await s.get(TaskRoute, task)
        if snap is None:
            if r is not None:
                await s.delete(r)
            return
        if r is None:
            r = TaskRoute(task=task)
            s.add(r)
        for k, v in snap.items():
            setattr(r, k, v)


async def test_refresh_merges_openrouter_image_models_with_upstream_caps(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _mock_upstream(
        monkeypatch,
        lambda r: {"data": _OR_IMAGE if r.url.params.get("output_modalities") == "image" else _OR_TEXT},
    )
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        "/api/providers", json={"name": name, "base_url": "https://router.example.com/api/v1", "api_key": "sk-test"}
    )
    pid = resp.json()["id"]
    image_before = await _snapshot_route("image")
    try:
        aliased = await client.put(
            f"/api/providers/{pid}/aliases", json={"aliases": {"acme/plain": "anthropic/claude-x"}}
        )
        assert aliased.status_code == 200, aliased.text

        res = await client.post(f"/api/providers/{pid}/refresh-models")
        assert res.status_code == 200, res.text
        body = res.json()
        assert [(r.url.path, r.url.params.get("output_modalities")) for r in seen] == [
            ("/api/v1/models", None),
            ("/api/v1/models", "image"),
        ]
        assert all(r.headers["authorization"] == "Bearer sk-test" for r in seen)
        assert body["probe"]["model_count"] == 4
        provider = body["provider"]
        assert provider["models"] == ["acme/plain", "google/gemini-img", "openai/gpt-image-z", "openai/gpt-x"]
        caps = provider["model_caps"]
        assert caps["openai/gpt-x"] == {
            "catalog_id": "upstream:openai/gpt-x",
            "vision": True,
            "pdf": True,
            "tools": True,
            "reasoning": True,
            "context": 400000,
            "output": 128000,
            "image_output": False,
        }
        assert caps["openai/gpt-image-z"]["image_output"] is True
        assert caps["openai/gpt-image-z"]["tools"] is False
        assert caps["google/gemini-img"]["image_output"] is True
        # The alias still wins over what the provider reports.
        assert caps["acme/plain"]["catalog_id"] == "anthropic/claude-x"

        # Dropping the alias falls back to the stored upstream caps, not to "unknown".
        unaliased = await client.put(f"/api/providers/{pid}/aliases", json={"aliases": {}})
        plain = unaliased.json()["provider"]["model_caps"]["acme/plain"]
        assert (plain["catalog_id"], plain["tools"], plain["context"]) == ("upstream:acme/plain", False, None)

        no_image = await client.put("/api/routes/image", json={"provider_id": pid, "model": "openai/gpt-x"})
        assert no_image.status_code == 422
        assert "出图" in no_image.text
        assert "上游 /models" in no_image.text
        image_only_chat = await client.put("/api/routes/chat", json={"provider_id": pid, "model": "openai/gpt-image-z"})
        assert image_only_chat.status_code == 422
        assert "工具调用" in image_only_chat.text
        ok = await client.put("/api/routes/image", json={"provider_id": pid, "model": "openai/gpt-image-z"})
        assert ok.status_code == 200, ok.text
    finally:
        await _restore_route("image", image_before)
        async with get_session() as s:
            await s.execute(delete(Provider).where(Provider.id == pid))


async def test_refresh_plain_openai_upstream_single_call_without_upstream_caps(
    client: httpx.AsyncClient, published: list[str], cleanup_audit: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _mock_upstream(monkeypatch, lambda _r: {"object": "list", "data": [{"id": "gw/claude-x"}, {"id": "other"}]})
    name = f"pytest-{uuid.uuid4().hex[:8]}"
    resp = await client.post("/api/providers", json={"name": name, "base_url": "https://gw.example.com/v1"})
    pid = resp.json()["id"]
    try:
        async with get_session() as s:
            row = await s.get(Provider, pid)
            assert row is not None
            row.upstream_caps = {
                "gone": {"catalog_id": "upstream:gone", "vision": True, "pdf": False, "tools": True, "reasoning": False}
            }
        res = await client.post(f"/api/providers/{pid}/refresh-models")
        assert res.status_code == 200, res.text
        assert len(seen) == 1
        provider = res.json()["provider"]
        assert provider["models"] == ["gw/claude-x", "other"]
        assert {k: v["catalog_id"] for k, v in provider["model_caps"].items()} == {"gw/claude-x": "anthropic/claude-x"}
        async with get_session() as s:
            row = await s.get(Provider, pid)
            assert row is not None
            assert row.upstream_caps == {}
    finally:
        async with get_session() as s:
            await s.execute(delete(Provider).where(Provider.id == pid))

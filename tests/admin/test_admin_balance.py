import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sqlalchemy import delete, select

from tests.admin.conftest import ADMIN
from yggdrasil.db.models import ConfigAudit, Provider, SearchProvider
from yggdrasil.db.session import get_session

type Routes = dict[str, Callable[[httpx.Request], httpx.Response] | httpx.Response]
type Cleanup = list[tuple[type[Provider | SearchProvider], int]]


class Upstream:
    """Stands in for every outbound HTTP call (the admin test client itself uses the ASGI transport)."""

    def __init__(self) -> None:
        self.routes: Routes = {}
        self.seen: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url.copy_with(query=None))
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        return route(request) if callable(route) else route

    def auth(self, url: str) -> list[str]:
        return [r.headers["authorization"] for r in self.seen if str(r.url) == url]


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    up = Upstream()

    async def handle(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await up.handle(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return up


@pytest.fixture
async def cleanup() -> AsyncIterator[Cleanup]:
    rows: Cleanup = []
    yield rows
    async with get_session() as s:
        for model, row_id in rows:
            await s.execute(delete(model).where(model.id == row_id))
        await s.execute(delete(ConfigAudit).where(ConfigAudit.actor == ADMIN))


async def _provider(client: httpx.AsyncClient, cleanup: Cleanup, base_url: str, **extra: object) -> int:
    resp = await client.post(
        "/api/providers", json={"name": f"pytest-{uuid.uuid4().hex[:8]}", "base_url": base_url, **extra}
    )
    assert resp.status_code == 200, resp.text
    cleanup.append((Provider, resp.json()["id"]))
    return resp.json()["id"]


async def test_deepseek_balance_parses_string_amounts_per_currency(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    upstream.routes["https://api.deepseek.com/user/balance"] = httpx.Response(
        200,
        json={
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                },
                {"currency": "USD", "total_balance": "1.50", "granted_balance": "0", "topped_up_balance": "1.50"},
            ],
        },
    )
    pid = await _provider(client, cleanup, "https://api.deepseek.com/v1", api_key="sk-deepseek")

    resp = await client.get(f"/api/providers/{pid}/balance")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["kind"], body["available"]) == ("deepseek", True)
    assert [(a["label"], a["currency"], a["value"]) for a in body["amounts"]] == [
        ("余额", "CNY", 110.0),
        ("赠送", "CNY", 10.0),
        ("充值", "CNY", 100.0),
        ("余额", "USD", 1.5),
        ("赠送", "USD", 0.0),
        ("充值", "USD", 1.5),
    ]
    assert upstream.auth("https://api.deepseek.com/user/balance") == ["Bearer sk-deepseek"]


async def test_openrouter_uses_credits_with_balance_key_else_key_usage(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    base = "https://openrouter.ai/api/v1"
    upstream.routes[f"{base}/credits"] = lambda r: (
        httpx.Response(200, json={"data": {"total_credits": 100.5, "total_usage": 25.75}})
        if r.headers["authorization"] == "Bearer mgmt-key"
        else httpx.Response(403, json={"error": {"message": "forbidden"}})
    )
    upstream.routes[f"{base}/key"] = httpx.Response(
        200, json={"data": {"label": "bot", "limit": 20, "limit_remaining": 12.5, "usage": 7.5}}
    )
    pid = await _provider(client, cleanup, base, api_key="sk-or-normal", balance_key="mgmt-key")

    with_mgmt = (await client.get(f"/api/providers/{pid}/balance")).json()
    assert [(a["label"], a["currency"], a["value"]) for a in with_mgmt["amounts"]] == [
        ("余额", "USD", 74.75),
        ("已用", "USD", 25.75),
        ("总充值", "USD", 100.5),
    ]
    assert with_mgmt["available"] is True
    assert upstream.auth(f"{base}/key") == []

    row = next(p for p in (await client.get("/api/providers")).json() if p["id"] == pid)
    cleared = await client.put(
        f"/api/providers/{pid}", json={"name": row["name"], "base_url": base, "clear_balance_key": True}
    )
    assert cleared.status_code == 200
    without = (await client.get(f"/api/providers/{pid}/balance")).json()
    assert [(a["label"], a["value"]) for a in without["amounts"]] == [
        ("key 剩余", 12.5),
        ("key 限额", 20.0),
        ("key 已用", 7.5),
    ]
    assert without["note"]
    assert upstream.auth(f"{base}/key") == ["Bearer sk-or-normal"]


async def test_openrouter_bad_management_key_is_an_upstream_error(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    base = "https://openrouter.ai/api/v1"
    upstream.routes[f"{base}/credits"] = httpx.Response(403, json={"error": {"message": "forbidden"}})
    pid = await _provider(client, cleanup, base, api_key="sk-or-normal", balance_key="not-management")

    resp = await client.get(f"/api/providers/{pid}/balance")

    assert resp.status_code == 503
    assert "403" in resp.json()["detail"]
    assert "not-management" not in resp.text


@pytest.mark.parametrize(
    ("hard_limit", "cents", "remaining", "note"),
    [(74.75, 2525, 49.5, "网关额度，单位随站点的额度显示设置"), (100000000.0, 1234, None, "不限额度")],
)
async def test_newapi_remaining_from_subscription_and_usage_cents(
    client: httpx.AsyncClient,
    upstream: Upstream,
    cleanup: Cleanup,
    hard_limit: float,
    cents: int,
    remaining: float | None,
    note: str,
) -> None:
    base = "https://gateway.example.com/v1"
    upstream.routes[f"{base}/dashboard/billing/subscription"] = httpx.Response(
        200, json={"object": "billing_subscription", "hard_limit_usd": hard_limit, "soft_limit_usd": hard_limit}
    )
    upstream.routes[f"{base}/dashboard/billing/usage"] = httpx.Response(
        200, json={"object": "list", "total_usage": cents}
    )
    pid = await _provider(client, cleanup, base, api_key="sk-gateway")

    body = (await client.get(f"/api/providers/{pid}/balance")).json()

    assert body["kind"] == "new-api"
    assert [(a["label"], a["currency"], a["value"]) for a in body["amounts"]] == [
        ("剩余额度", None, remaining),
        ("已用", None, cents / 100),
    ]
    assert body["note"] == note
    assert body["available"] is True


async def test_server_without_billing_api_is_unsupported(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    pid = await _provider(client, cleanup, "https://llm.example.com/v1", api_key="sk-local")

    resp = await client.get(f"/api/providers/{pid}/balance")

    assert resp.status_code == 422
    assert "不支持余额查询" in resp.json()["detail"]
    assert (await client.get("/api/providers/2147483000/balance")).status_code == 404


async def test_balance_key_is_write_only_and_clearable(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    secret = f"sk-or-v1-{uuid.uuid4().hex}"
    base = "https://openrouter.ai/api/v1"
    pid = await _provider(client, cleanup, base, balance_key=secret)

    listed = await client.get("/api/providers")
    row = next(p for p in listed.json() if p["id"] == pid)
    assert secret not in listed.text
    assert row["balance_key_masked"] == f"…{secret[-4:]}"
    assert row["api_key_masked"] is None

    kept = await client.put(f"/api/providers/{pid}", json={"name": row["name"], "base_url": base})
    assert kept.json()["balance_key_masked"] == f"…{secret[-4:]}"
    cleared = await client.put(
        f"/api/providers/{pid}", json={"name": row["name"], "base_url": base, "clear_balance_key": True}
    )
    assert cleared.json()["balance_key_masked"] is None
    async with get_session() as s:
        stored = await s.scalar(select(Provider.balance_key_enc).where(Provider.id == pid))
        details = (
            await s.scalars(
                select(ConfigAudit.detail)
                .where(ConfigAudit.entity == "provider", ConfigAudit.entity_id == str(pid))
                .order_by(ConfigAudit.id)
            )
        ).all()
    assert stored is None
    assert [d.get("balance_key_changed") for d in details if "balance_key_changed" in d] == [False, True]
    assert all(secret not in str(d) for d in details)


async def test_search_provider_balance_only_for_deepseek(
    client: httpx.AsyncClient, upstream: Upstream, cleanup: Cleanup
) -> None:
    upstream.routes["https://api.deepseek.com/user/balance"] = httpx.Response(
        200,
        json={
            "is_available": False,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"}
            ],
        },
    )
    ids: dict[str, int] = {}
    for kind, extra in (
        ("deepseek", {"api_key": "sk-search", "base_url": "https://search-proxy.example.com"}),
        ("exa", {"api_key": "exa-key"}),
    ):
        resp = await client.post(
            "/api/search-providers", json={"name": f"pytest-{uuid.uuid4().hex[:8]}", "kind": kind, **extra}
        )
        assert resp.status_code == 200, resp.text
        ids[kind] = resp.json()["id"]
        cleanup.append((SearchProvider, ids[kind]))

    deepseek = await client.get(f"/api/search-providers/{ids['deepseek']}/balance")
    assert deepseek.status_code == 200, deepseek.text
    assert deepseek.json()["available"] is False
    assert deepseek.json()["amounts"][0] == {"label": "余额", "currency": "CNY", "value": 0.0}
    assert upstream.auth("https://api.deepseek.com/user/balance") == ["Bearer sk-search"]

    exa = await client.get(f"/api/search-providers/{ids['exa']}/balance")
    assert exa.status_code == 422
    assert (await client.get("/api/search-providers/2147483000/balance")).status_code == 404

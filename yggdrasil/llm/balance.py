"""Account balance of an LLM provider (DeepSeek, OpenRouter, a new-api gateway) or of the X API.

Each fetcher returns a ``BalanceOut`` or raises ``BalanceUnsupported`` (the endpoint has no balance API)
or ``BalanceError`` (the upstream answered with an error or garbage). Messages never contain key material.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from yggdrasil.config import APP_CONFIG

type BalanceKind = Literal["deepseek", "openrouter", "new-api", "x"]

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
# new-api reports an unlimited token as a huge hard limit.
_UNLIMITED = 1e8
_TIMEOUT = httpx.Timeout(10, connect=5)


class BalanceAmount(BaseModel):
    label: str
    currency: str | None = None  # None: gateway quota units (new-api follows the site's display setting)
    value: float | None = None  # None: unlimited / unknown


class BalanceOut(BaseModel):
    kind: BalanceKind
    available: bool | None
    amounts: list[BalanceAmount]
    note: str | None = None
    fetched_at: datetime


class BalanceUnsupported(Exception):
    """The provider exposes no balance API this module understands."""


class BalanceError(Exception):
    """The balance API failed (HTTP error, network error, unexpected body)."""


def detect_kind(base_url: str) -> Literal["deepseek", "openrouter", "new-api"]:
    """Balance API of an LLM provider (the X API is a search provider, see :func:`x_balance`)."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "api.deepseek.com":
        return "deepseek"
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    return "new-api"


async def fetch_balance(
    base_url: str,
    api_key: str | None,
    *,
    balance_key: str | None = None,
    headers: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> BalanceOut:
    """Balance of an OpenAI-compatible provider, dispatched on its base URL."""
    match detect_kind(base_url):
        case "deepseek":
            return await deepseek_balance(api_key, headers=headers, client=client)
        case "openrouter":
            return await openrouter_balance(base_url, api_key, balance_key, headers=headers, client=client)
        case "new-api":
            return await newapi_balance(base_url, api_key, headers=headers, client=client)


async def deepseek_balance(
    api_key: str | None, *, headers: Mapping[str, str] | None = None, client: httpx.AsyncClient | None = None
) -> BalanceOut:
    if not api_key:
        raise BalanceUnsupported("未配置 API key，无法查询 DeepSeek 余额")
    data = await _get_json(DEEPSEEK_BALANCE_URL, api_key, headers, client)
    infos = data.get("balance_infos")
    if not isinstance(infos, list):
        raise BalanceError("DeepSeek 余额响应缺少 balance_infos")
    amounts: list[BalanceAmount] = []
    # The console shows the first amount: lead with the currency that holds money (accounts list USD and CNY, one 0).
    rows = sorted((i for i in infos if isinstance(i, dict)), key=lambda i: -(_num(i.get("total_balance")) or 0))
    for info in rows:
        currency = str(info.get("currency") or "") or None
        for field, label in (("total_balance", "余额"), ("granted_balance", "赠送"), ("topped_up_balance", "充值")):
            amounts.append(BalanceAmount(label=label, currency=currency, value=_num(info.get(field))))
    available = data.get("is_available")
    return BalanceOut(
        kind="deepseek",
        available=available if isinstance(available, bool) else None,
        amounts=amounts,
        fetched_at=datetime.now(UTC),
    )


async def openrouter_balance(
    base_url: str,
    api_key: str | None,
    balance_key: str | None,
    *,
    headers: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> BalanceOut:
    """Account credits with a management key, else the API key's own limit and usage."""
    base = base_url.rstrip("/")
    if balance_key:
        data = _data(await _get_json(f"{base}/credits", balance_key, headers, client))
        total, usage = _num(data.get("total_credits")), _num(data.get("total_usage"))
        if total is None or usage is None:
            raise BalanceError("OpenRouter /credits 响应缺少 total_credits / total_usage")
        remaining = total - usage
        return BalanceOut(
            kind="openrouter",
            available=remaining > 0,
            amounts=[
                BalanceAmount(label="余额", currency="USD", value=remaining),
                BalanceAmount(label="已用", currency="USD", value=usage),
                BalanceAmount(label="总充值", currency="USD", value=total),
            ],
            fetched_at=datetime.now(UTC),
        )
    if not api_key:
        raise BalanceUnsupported("未配置 API key 或余额查询 key，无法查询 OpenRouter 余额")
    data = _data(await _get_json(f"{base}/key", api_key, headers, client))
    limit, remaining = _num(data.get("limit")), _num(data.get("limit_remaining"))
    amounts: list[BalanceAmount] = []
    if limit is not None:
        amounts.append(BalanceAmount(label="key 剩余", currency="USD", value=remaining))
        amounts.append(BalanceAmount(label="key 限额", currency="USD", value=limit))
    amounts.append(BalanceAmount(label="key 已用", currency="USD", value=_num(data.get("usage"))))
    note = "显示的是该 API key 自身的额度与用量；配置管理 key 可查看账户余额"
    if limit is None:
        note = "该 API key 不限额度，显示的是它的用量；配置管理 key 可查看账户余额"
    return BalanceOut(
        kind="openrouter",
        available=None if limit is None or remaining is None else remaining > 0,
        amounts=amounts,
        note=note,
        fetched_at=datetime.now(UTC),
    )


async def x_balance(base_url: str, api_key: str | None, *, client: httpx.AsyncClient | None = None) -> BalanceOut:
    """X API pay-per-use credits (``GET /2/usage/credits``, app bearer token)."""
    if not api_key:
        raise BalanceUnsupported("未配置 Bearer Token，无法查询 X API 余额")
    data = _data(await _get_json(f"{base_url.rstrip('/')}/2/usage/credits", api_key, None, client))
    total = _num(data.get("total_balance"))
    if total is None:
        raise BalanceError("X API 余额响应缺少 total_balance")
    grants = sorted(
        (str(g.get("expires_at") or "")[:10], amount)
        for g in data.get("free_grants") or []
        if isinstance(g, dict) and (amount := _num(g.get("amount")))
    )
    return BalanceOut(
        kind="x",
        available=total > 0,
        amounts=[
            BalanceAmount(label="余额", currency="USD", value=total),
            BalanceAmount(label="充值", currency="USD", value=_num(data.get("prepaid_balance"))),
            BalanceAmount(label="赠送", currency="USD", value=_num(data.get("free_balance"))),
        ],
        note="；".join(f"赠送 ${amount:.2f} 于 {expires} 到期" for expires, amount in grants) or None,
        fetched_at=datetime.now(UTC),
    )


async def newapi_balance(
    base_url: str,
    api_key: str | None,
    *,
    headers: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> BalanceOut:
    """Remaining quota of a new-api token via its OpenAI-style billing endpoints."""
    if not api_key:
        raise BalanceUnsupported("未配置 API key，无法查询余额")
    base = base_url.rstrip("/")
    sub = await _get_json(f"{base}/dashboard/billing/subscription", api_key, headers, client, probe=True)
    hard_limit = _num(sub.get("hard_limit_usd"))
    if hard_limit is None:
        raise BalanceUnsupported("该 provider 不支持余额查询（不是 new-api 网关）")
    usage = await _get_json(f"{base}/dashboard/billing/usage", api_key, headers, client)
    cents = _num(usage.get("total_usage"))
    if cents is None:
        raise BalanceError("new-api /dashboard/billing/usage 响应缺少 total_usage")
    used = cents / 100
    if hard_limit >= _UNLIMITED:
        return BalanceOut(
            kind="new-api",
            available=True,
            amounts=[BalanceAmount(label="剩余额度"), BalanceAmount(label="已用", value=used)],
            note="不限额度",
            fetched_at=datetime.now(UTC),
        )
    remaining = hard_limit - used
    return BalanceOut(
        kind="new-api",
        available=remaining > 0,
        amounts=[BalanceAmount(label="剩余额度", value=remaining), BalanceAmount(label="已用", value=used)],
        note="网关额度，单位随站点的额度显示设置",
        fetched_at=datetime.now(UTC),
    )


async def _get_json(
    url: str,
    key: str,
    headers: Mapping[str, str] | None,
    client: httpx.AsyncClient | None,
    *,
    probe: bool = False,
) -> dict[str, Any]:
    """GET a JSON object with a bearer key. ``probe``: 404/405 or a non-JSON body mean "no such API"."""
    request_headers = {"User-Agent": APP_CONFIG.llm_user_agent, **(headers or {}), "Authorization": f"Bearer {key}"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
                resp = await owned.get(url, headers=request_headers)
        else:
            resp = await client.get(url, headers=request_headers, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise BalanceError(f"余额查询请求失败: {type(e).__name__}") from e
    if probe and resp.status_code in (404, 405):
        raise BalanceUnsupported(f"该 provider 不支持余额查询（HTTP {resp.status_code}）")
    if resp.status_code >= 400:
        raise BalanceError(f"余额查询失败: HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        if probe:
            raise BalanceUnsupported("该 provider 不支持余额查询（响应不是 JSON）") from e
        raise BalanceError(f"余额响应不是 JSON (HTTP {resp.status_code})") from e
    if not isinstance(data, dict):
        if probe:
            raise BalanceUnsupported("该 provider 不支持余额查询（响应格式不符）")
        raise BalanceError("余额响应格式不符")
    return data


def _data(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    if not isinstance(data, dict):
        raise BalanceError("余额响应缺少 data")
    return data


def _num(value: object) -> float | None:
    """A JSON number or numeric string (DeepSeek sends amounts as strings)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None

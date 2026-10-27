"""OpenAI-compatible providers: CRUD, connectivity test, balance, model list + capability refresh, aliases."""

import time
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unavailable, unprocessable
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import Provider
from yggdrasil.db.session import get_session
from yggdrasil.llm.balance import BalanceError, BalanceOut, BalanceUnsupported, fetch_balance
from yggdrasil.llm.catalog import Catalog, ModelCaps, load_catalog, upstream_caps

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    api_key_masked: str | None
    balance_key_masked: str | None
    headers: dict[str, str]
    enabled: bool
    api_format: str
    models: list[str]
    models_fetched_at: datetime | None
    model_aliases: dict[str, str]
    model_caps: dict[str, ModelCaps]
    created_at: datetime
    updated_at: datetime


class ProviderIn(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1, pattern=r"^https?://")
    # Empty / omitted keeps the stored key on update.
    api_key: str | None = None
    clear_api_key: bool = False
    # Optional key used only to read the account balance (OpenRouter management key); write-only like api_key.
    balance_key: str | None = None
    clear_balance_key: bool = False
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    # Chat protocol: OpenAI chat completions, or Anthropic-native /v1/messages (for tool calling).
    api_format: Literal["openai", "anthropic"] = "openai"


class ProviderProbe(BaseModel):
    ok: bool
    status_code: int | None = None
    latency_ms: int
    model_count: int | None = None
    error: str | None = None


def _masked(enc: bytes | None) -> str | None:
    if not enc:
        return None
    try:
        return mask(decrypt(enc))
    except Exception:
        return "…(无法解密)"


def _out(p: Provider) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        name=p.name,
        base_url=p.base_url,
        api_key_masked=_masked(p.api_key_enc),
        balance_key_masked=_masked(p.balance_key_enc),
        headers=dict(p.headers),
        enabled=p.enabled,
        api_format=p.api_format,
        models=[str(m) for m in p.models or ()],
        models_fetched_at=p.models_fetched_at,
        model_aliases={str(k): str(v) for k, v in (p.model_aliases or {}).items()},
        model_caps={k: ModelCaps.model_validate(v) for k, v in (p.model_caps or {}).items()},
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _apply(p: Provider, body: ProviderIn) -> None:
    p.name = body.name.strip()
    p.base_url = body.base_url.strip().rstrip("/")
    p.headers = body.headers
    p.enabled = body.enabled
    p.api_format = body.api_format
    if body.clear_api_key:
        p.api_key_enc = None
    elif body.api_key:
        p.api_key_enc = encrypt(body.api_key.strip())
    if body.clear_balance_key:
        p.balance_key_enc = None
    elif body.balance_key:
        p.balance_key_enc = encrypt(body.balance_key.strip())


@router.get("")
async def list_providers() -> list[ProviderOut]:
    async with get_session() as s:
        return [_out(p) for p in (await s.scalars(select(Provider).order_by(Provider.id))).all()]


@router.post("")
async def create_provider(body: ProviderIn, actor: Actor) -> ProviderOut:
    try:
        async with change(actor, "provider", "create") as c:
            p = Provider(models=[])
            _apply(p, body)
            c.session.add(p)
            await c.session.flush()
            await c.session.refresh(p)
            c.entity_id, c.detail = str(p.id), {"name": p.name, "base_url": p.base_url}
    except IntegrityError as e:
        raise conflict(f"名称 {body.name} 已存在") from e
    return _out(p)


@router.put("/{provider_id}")
async def update_provider(provider_id: int, body: ProviderIn, actor: Actor) -> ProviderOut:
    try:
        async with change(actor, "provider", "update", provider_id) as c:
            p = await c.session.get(Provider, provider_id)
            if p is None:
                raise not_found("provider ")
            _apply(p, body)
            c.detail = {
                "name": p.name,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "api_format": p.api_format,
                "key_changed": bool(body.api_key) or body.clear_api_key,
                "balance_key_changed": bool(body.balance_key) or body.clear_balance_key,
            }
            await c.session.flush()
            await c.session.refresh(p)
    except IntegrityError as e:
        raise conflict(f"名称 {body.name} 已存在") from e
    return _out(p)


@router.delete("/{provider_id}")
async def delete_provider(provider_id: int, actor: Actor) -> Ok:
    try:
        async with change(actor, "provider", "delete", provider_id) as c:
            p = await c.session.get(Provider, provider_id)
            if p is None:
                raise not_found("provider ")
            c.detail = {"name": p.name}
            await c.session.delete(p)
            await c.session.flush()
    except IntegrityError as e:
        raise conflict("仍有模型路由或向量空间在使用这个 provider") from e
    return Ok()


class _ModelsError(Exception):
    def __init__(self, error: str, status_code: int | None = None) -> None:
        super().__init__(error)
        self.error, self.status_code = error, status_code


async def _get_models(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise _ModelsError(f"{type(e).__name__}: {e}") from e
    if resp.status_code >= 400:
        raise _ModelsError(resp.text[:500] or resp.reason_phrase, resp.status_code)
    try:
        data = resp.json().get("data", [])
    except (ValueError, AttributeError) as e:
        raise _ModelsError(f"无法解析 /models 响应: {e}", resp.status_code) from e
    if not isinstance(data, list):
        raise _ModelsError("无法解析 /models 响应: data 不是列表", resp.status_code)
    return resp.status_code, [m for m in cast(list[Any], data) if isinstance(m, dict) and "id" in m]


async def fetch_models(p: Provider) -> tuple[ProviderProbe, list[str], dict[str, dict[str, Any]]]:
    """Model ids plus the capabilities the provider itself reports (empty unless OpenRouter-style).

    OpenRouter lists only text-output models by default, so when items carry ``architecture`` the
    image-output models are fetched too and merged.
    """
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key_enc:
        headers["Authorization"] = f"Bearer {decrypt(p.api_key_enc)}"
    url = f"{p.base_url.rstrip('/')}/models"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            status, items = await _get_models(client, url, headers)
            if any("architecture" in m for m in items):
                _, extra = await _get_models(client, f"{url}?output_modalities=image", headers)
                items += extra
    except _ModelsError as e:
        latency = int((time.perf_counter() - started) * 1000)
        return ProviderProbe(ok=False, status_code=e.status_code, latency_ms=latency, error=e.error), [], {}
    latency = int((time.perf_counter() - started) * 1000)
    ids = sorted({str(m["id"]) for m in items})
    upstream = {str(m["id"]): c.model_dump() for m in items if (c := upstream_caps(str(m["id"]), m))}
    probe = ProviderProbe(ok=True, status_code=status, latency_ms=latency, model_count=len(ids))
    return probe, ids, upstream


@router.post("/{provider_id}/test")
async def test_provider(provider_id: int) -> ProviderProbe:
    async with get_session() as s:
        p = await s.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
    probe, _, _ = await fetch_models(p)
    return probe


@router.get("/{provider_id}/balance")
async def get_provider_balance(provider_id: int) -> BalanceOut:
    """Account balance: DeepSeek, OpenRouter (credits with the balance key, else the API key's limit), new-api."""
    async with get_session() as s:
        p = await s.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
    try:
        api_key = decrypt(p.api_key_enc) if p.api_key_enc else None
        balance_key = decrypt(p.balance_key_enc) if p.balance_key_enc else None
    except Exception as e:
        raise unprocessable("无法解密已存储的 key") from e
    try:
        return await fetch_balance(p.base_url, api_key, balance_key=balance_key, headers=p.headers)
    except BalanceUnsupported as e:
        raise unprocessable(str(e)) from e
    except BalanceError as e:
        raise unavailable(str(e)) from e


class RefreshResult(BaseModel):
    probe: ProviderProbe
    provider: ProviderOut
    catalog_error: str | None = None  # models.dev unreachable: capabilities kept from the last refresh


async def _catalog() -> tuple[Catalog | None, str | None]:
    """models.dev catalog, or an error text (callers then keep the stored capabilities)."""
    try:
        return await load_catalog(), None
    except (httpx.HTTPError, ValueError) as e:
        return None, f"无法获取 models.dev 能力目录: {type(e).__name__}: {e}"


def apply_caps(p: Provider, catalog: Catalog | None) -> None:
    """Recompute ``model_caps`` from aliases, the stored upstream caps and models.dev."""
    aliases = {str(k): str(v) for k, v in (p.model_aliases or {}).items()}
    upstream = p.upstream_caps or {}
    if catalog is not None:
        p.model_caps = catalog.resolve([str(m) for m in p.models or ()], aliases, upstream)
    elif upstream:
        # models.dev unreachable: still take what the provider reports, keep the rest from last time.
        p.model_caps = {**(p.model_caps or {}), **{k: v for k, v in upstream.items() if k not in aliases}}


@router.post("/{provider_id}/refresh-models")
async def refresh_provider_models(provider_id: int, actor: Actor) -> RefreshResult:
    async with get_session() as s:
        p = await s.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
    probe, ids, upstream = await fetch_models(p)
    catalog, catalog_error = await _catalog()
    if not probe.ok:
        return RefreshResult(probe=probe, provider=_out(p))
    async with change(actor, "provider", "refresh_models", provider_id) as c:
        p = await c.session.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
        p.models = ids
        p.upstream_caps = upstream
        p.models_fetched_at = datetime.now(UTC)
        apply_caps(p, catalog)
        c.detail = {"count": len(ids), "with_caps": len(p.model_caps), "upstream_caps": len(upstream)}
        await c.session.flush()
        await c.session.refresh(p)
    return RefreshResult(probe=probe, provider=_out(p), catalog_error=catalog_error)


class AliasesIn(BaseModel):
    # provider model id -> models.dev id ("anthropic/claude-sonnet-5-5" or a bare "claude-sonnet-5-5")
    aliases: dict[str, str]


class AliasesResult(BaseModel):
    provider: ProviderOut
    catalog_error: str | None = None
    unmatched: list[str] = Field(default_factory=list)  # aliased ids the catalog still can't resolve


@router.put("/{provider_id}/aliases")
async def set_provider_aliases(provider_id: int, body: AliasesIn, actor: Actor) -> AliasesResult:
    aliases = {k.strip(): v.strip() for k, v in body.aliases.items() if k.strip() and v.strip()}
    catalog, catalog_error = await _catalog()
    async with change(actor, "provider", "set_aliases", provider_id) as c:
        p = await c.session.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
        p.model_aliases = aliases
        apply_caps(p, catalog)
        c.detail = {"aliases": aliases}
        await c.session.flush()
        await c.session.refresh(p)
    unmatched = [] if catalog_error else sorted(k for k in aliases if k not in (p.model_caps or {}))
    return AliasesResult(provider=_out(p), catalog_error=catalog_error, unmatched=unmatched)

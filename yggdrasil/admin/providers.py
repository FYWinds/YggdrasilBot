"""OpenAI-compatible providers: CRUD, connectivity test, model list + capability refresh, aliases."""

import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import Provider
from yggdrasil.db.session import get_session
from yggdrasil.llm.catalog import Catalog, ModelCaps, load_catalog

router = APIRouter(prefix="/providers", tags=["providers"])


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str
    api_key_masked: str | None
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


def _out(p: Provider) -> ProviderOut:
    masked = None
    if p.api_key_enc:
        try:
            masked = mask(decrypt(p.api_key_enc))
        except Exception:
            masked = "…(无法解密)"
    return ProviderOut(
        id=p.id,
        name=p.name,
        base_url=p.base_url,
        api_key_masked=masked,
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


async def _fetch_models(p: Provider) -> tuple[ProviderProbe, list[str]]:
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key_enc:
        headers["Authorization"] = f"Bearer {decrypt(p.api_key_enc)}"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            resp = await client.get(f"{p.base_url.rstrip('/')}/models", headers=headers)
    except httpx.HTTPError as e:
        latency = int((time.perf_counter() - started) * 1000)
        return ProviderProbe(ok=False, latency_ms=latency, error=f"{type(e).__name__}: {e}"), []
    latency = int((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        return ProviderProbe(
            ok=False, status_code=resp.status_code, latency_ms=latency, error=resp.text[:500] or resp.reason_phrase
        ), []
    try:
        data = resp.json()
        ids = sorted({str(m["id"]) for m in data.get("data", []) if isinstance(m, dict) and "id" in m})
    except (ValueError, AttributeError) as e:
        return ProviderProbe(
            ok=False, status_code=resp.status_code, latency_ms=latency, error=f"无法解析 /models 响应: {e}"
        ), []
    return ProviderProbe(ok=True, status_code=resp.status_code, latency_ms=latency, model_count=len(ids)), ids


@router.post("/{provider_id}/test")
async def test_provider(provider_id: int) -> ProviderProbe:
    async with get_session() as s:
        p = await s.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
    probe, _ = await _fetch_models(p)
    return probe


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


def _apply_caps(p: Provider, catalog: Catalog | None) -> None:
    if catalog is not None:
        p.model_caps = catalog.resolve([str(m) for m in p.models or ()], p.model_aliases or {})


@router.post("/{provider_id}/refresh-models")
async def refresh_provider_models(provider_id: int, actor: Actor) -> RefreshResult:
    async with get_session() as s:
        p = await s.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
    probe, ids = await _fetch_models(p)
    catalog, catalog_error = await _catalog()
    if not probe.ok:
        return RefreshResult(probe=probe, provider=_out(p))
    async with change(actor, "provider", "refresh_models", provider_id) as c:
        p = await c.session.get(Provider, provider_id)
        if p is None:
            raise not_found("provider ")
        p.models = ids
        p.models_fetched_at = datetime.now(UTC)
        _apply_caps(p, catalog)
        c.detail = {"count": len(ids), "with_caps": len(p.model_caps)}
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
        _apply_caps(p, catalog)
        c.detail = {"aliases": aliases}
        await c.session.flush()
        await c.session.refresh(p)
    unmatched = [] if catalog_error else sorted(k for k in aliases if k not in (p.model_caps or {}))
    return AliasesResult(provider=_out(p), catalog_error=catalog_error, unmatched=unmatched)

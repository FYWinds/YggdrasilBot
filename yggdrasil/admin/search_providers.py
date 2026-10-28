"""Search providers (the pool behind ``web_search``, plus the X API key for ``search_x`` / ``read_x``): CRUD, this
month's usage, test query, balance."""

import time
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unavailable, unprocessable
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import SearchProvider
from yggdrasil.db.session import get_session
from yggdrasil.llm.balance import BalanceError, BalanceOut, BalanceUnsupported, deepseek_balance
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.search import DEFAULT_BASE_URLS, SearchBackendError, monthly_usage, reserve_quota, search_with

router = APIRouter(prefix="/search-providers", tags=["search"])

type SearchKind = Literal["searxng", "exa", "deepseek", "x"]

_KEYED = {"exa": "Exa", "deepseek": "DeepSeek", "x": "X API"}


class SearchProviderOut(BaseModel):
    id: int
    name: str
    kind: str
    base_url: str
    api_key_masked: str | None
    enabled: bool
    priority: int
    monthly_limit: int | None
    params: dict[str, Any]
    used_this_month: int
    created_at: datetime
    updated_at: datetime


class SearchProviderIn(BaseModel):
    name: str = Field(min_length=1)
    kind: SearchKind
    # Empty uses the kind's public endpoint (exa: https://api.exa.ai, deepseek: https://api.deepseek.com);
    # searxng needs its own.
    base_url: str = Field(default="", pattern=r"^(https?://.*)?$")
    # Empty / omitted keeps the stored key on update.
    api_key: str | None = None
    clear_api_key: bool = False
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    monthly_limit: int | None = Field(default=None, ge=1)
    params: dict[str, Any] = Field(default_factory=dict)


class SearchTestIn(BaseModel):
    query: str = Field(default="Python 最新版本", min_length=1)


class SearchTestHit(BaseModel):
    title: str
    url: str
    snippet: str


class SearchTestResult(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None
    hits: list[SearchTestHit] = Field(default_factory=list)


def _key(p: SearchProvider) -> str | None:
    if not p.api_key_enc:
        return None
    try:
        return decrypt(p.api_key_enc)
    except Exception:
        return None


def _info(p: SearchProvider) -> SearchProviderInfo:
    return SearchProviderInfo(
        p.id, p.name, p.kind, p.base_url, _key(p), p.enabled, p.priority, p.monthly_limit, dict(p.params)
    )


async def _out(p: SearchProvider) -> SearchProviderOut:
    masked = None
    if p.api_key_enc:
        key = _key(p)
        masked = mask(key) if key is not None else "…(无法解密)"
    return SearchProviderOut(
        id=p.id,
        name=p.name,
        kind=p.kind,
        base_url=p.base_url,
        api_key_masked=masked,
        enabled=p.enabled,
        priority=p.priority,
        monthly_limit=p.monthly_limit,
        params=dict(p.params),
        used_this_month=await monthly_usage(p.id),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _apply(p: SearchProvider, body: SearchProviderIn) -> None:
    base_url = body.base_url.strip().rstrip("/") or DEFAULT_BASE_URLS.get(body.kind, "")
    if not base_url:
        raise unprocessable(f"{body.kind} 需要填写地址")
    p.name = body.name.strip()
    p.kind = body.kind
    p.base_url = base_url
    p.enabled = body.enabled
    p.priority = body.priority
    p.monthly_limit = body.monthly_limit
    p.params = body.params
    if body.clear_api_key:
        p.api_key_enc = None
    elif body.api_key:
        p.api_key_enc = encrypt(body.api_key.strip())
    if body.kind in _KEYED and not p.api_key_enc:
        raise unprocessable(f"{_KEYED[body.kind]} 需要 API key")


def _detail(p: SearchProvider, body: SearchProviderIn) -> dict[str, Any]:
    return {
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "enabled": p.enabled,
        "priority": p.priority,
        "monthly_limit": p.monthly_limit,
        "params": p.params,
        "key_changed": bool(body.api_key) or body.clear_api_key,
    }


@router.get("")
async def list_search_providers() -> list[SearchProviderOut]:
    async with get_session() as s:
        rows = (await s.scalars(select(SearchProvider).order_by(SearchProvider.priority, SearchProvider.id))).all()
    return [await _out(p) for p in rows]


@router.post("")
async def create_search_provider(body: SearchProviderIn, actor: Actor) -> SearchProviderOut:
    try:
        async with change(actor, "search_provider", "create") as c:
            p = SearchProvider()
            _apply(p, body)
            c.session.add(p)
            await c.session.flush()
            await c.session.refresh(p)
            c.entity_id, c.detail = str(p.id), _detail(p, body)
    except IntegrityError as e:
        raise conflict(f"名称 {body.name} 已存在") from e
    return await _out(p)


@router.put("/{provider_id}")
async def update_search_provider(provider_id: int, body: SearchProviderIn, actor: Actor) -> SearchProviderOut:
    try:
        async with change(actor, "search_provider", "update", provider_id) as c:
            p = await c.session.get(SearchProvider, provider_id)
            if p is None:
                raise not_found("搜索源")
            _apply(p, body)
            c.detail = _detail(p, body)
            await c.session.flush()
            await c.session.refresh(p)
    except IntegrityError as e:
        raise conflict(f"名称 {body.name} 已存在") from e
    return await _out(p)


@router.delete("/{provider_id}")
async def delete_search_provider(provider_id: int, actor: Actor) -> Ok:
    async with change(actor, "search_provider", "delete", provider_id) as c:
        p = await c.session.get(SearchProvider, provider_id)
        if p is None:
            raise not_found("搜索源")
        c.detail = {"name": p.name}
        await c.session.delete(p)
    return Ok()


@router.post("/{provider_id}/test")
async def test_search_provider(provider_id: int, body: SearchTestIn) -> SearchTestResult:
    """Run one real query against this provider only (counts toward its monthly limit)."""
    async with get_session() as s:
        p = await s.get(SearchProvider, provider_id)
        if p is None:
            raise not_found("搜索源")
    info = _info(p)
    started = time.perf_counter()
    if not await reserve_quota(info):
        return SearchTestResult(ok=False, latency_ms=0, error="本月额度已用完")
    try:
        hits = await search_with(info, body.query.strip(), n=5)
    except SearchBackendError as e:
        return SearchTestResult(ok=False, latency_ms=int((time.perf_counter() - started) * 1000), error=str(e))
    return SearchTestResult(
        ok=True,
        latency_ms=int((time.perf_counter() - started) * 1000),
        hits=[SearchTestHit(title=h.title, url=h.url, snippet=h.snippet) for h in hits],
    )


@router.get("/{provider_id}/balance")
async def get_search_provider_balance(provider_id: int) -> BalanceOut:
    """Account balance of a DeepSeek search provider (always read from api.deepseek.com)."""
    async with get_session() as s:
        p = await s.get(SearchProvider, provider_id)
        if p is None:
            raise not_found("搜索源")
    if p.kind != "deepseek":
        raise unprocessable("只有 DeepSeek 搜索源支持余额查询")
    key = _key(p)
    if p.api_key_enc and key is None:
        raise unprocessable("无法解密已存储的 key")
    try:
        return await deepseek_balance(key)
    except BalanceUnsupported as e:
        raise unprocessable(str(e)) from e
    except BalanceError as e:
        raise unavailable(str(e)) from e

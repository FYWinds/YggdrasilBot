"""Model capabilities from the models.dev catalog.

Gateways like new-api only list model ids, so capabilities (image/PDF input, tool calling, context
size) are looked up in https://models.dev/api.json. Provider model ids are matched by stripping
gateway prefixes (``cvp/claude-sonnet-5-5`` → ``claude-sonnet-5-5``); a per-provider alias maps
ids that don't match (``my-sonnet`` → ``anthropic/claude-sonnet-5-5``).
"""

import time
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel

from yggdrasil.config import APP_CONFIG

CATALOG_URL = "https://models.dev/api.json"
_TTL_S = 3600
# Same model listed by several providers: prefer the vendor's own entry.
_FIRST_PARTY = ("anthropic", "openai", "google", "deepseek", "xai", "mistral", "alibaba", "moonshotai", "zhipuai")

_cache: tuple[float, "Catalog"] | None = None


class ModelCaps(BaseModel):
    catalog_id: str  # models.dev "provider/model"
    vision: bool
    pdf: bool
    tools: bool
    reasoning: bool
    context: int | None = None
    output: int | None = None


class Catalog:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.full: dict[str, ModelCaps] = {}
        self.bare: dict[str, ModelCaps] = {}
        rank: dict[str, int] = {}
        for prov, p in data.items():
            if not isinstance(p, dict):
                continue
            for mid, m in (p.get("models") or {}).items():
                if not isinstance(m, dict):
                    continue
                caps = _caps(f"{prov}/{mid}", m)
                self.full[caps.catalog_id.lower()] = caps
                key = mid.lower()
                r = _FIRST_PARTY.index(prov) if prov in _FIRST_PARTY else len(_FIRST_PARTY)
                if key not in self.bare or r < rank[key]:
                    self.bare[key], rank[key] = caps, r

    def lookup(self, model_id: str) -> ModelCaps | None:
        """Exact ``provider/model`` first, then the id with leading path segments stripped."""
        parts = model_id.strip().lower().split("/")
        for i in range(len(parts)):
            cand = "/".join(parts[i:])
            if hit := self.full.get(cand) or self.bare.get(cand):
                return hit
        return None

    def resolve(self, models: Iterable[str], aliases: Mapping[str, str]) -> dict[str, dict[str, Any]]:
        """Capabilities per provider model id (JSON-ready); unmatched ids are left out."""
        out: dict[str, dict[str, Any]] = {}
        for mid in sorted({*models, *aliases}):
            alias = aliases.get(mid, "").strip()
            if caps := self.lookup(alias or mid):
                out[mid] = caps.model_dump()
        return out


def _caps(catalog_id: str, m: Mapping[str, Any]) -> ModelCaps:
    inputs = (m.get("modalities") or {}).get("input") or []
    limit = m.get("limit") or {}
    return ModelCaps(
        catalog_id=catalog_id,
        vision="image" in inputs,
        pdf="pdf" in inputs,
        tools=bool(m.get("tool_call")),
        reasoning=bool(m.get("reasoning")),
        context=limit.get("context") or None,
        output=limit.get("output") or None,
    )


async def load_catalog() -> Catalog:
    """Fetched at most hourly per process; raises httpx errors when models.dev is unreachable."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < _TTL_S:
        return _cache[1]
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True) as client:
        resp = await client.get(CATALOG_URL, headers={"User-Agent": APP_CONFIG.llm_user_agent})
        resp.raise_for_status()
    catalog = Catalog(resp.json())
    _cache = (time.monotonic(), catalog)
    return catalog

"""Model capabilities from the models.dev catalog or the provider's own model list.

Gateways like new-api only list model ids, so capabilities (image/PDF input, tool calling, context
size) are looked up in https://models.dev/api.json. Provider model ids are matched by stripping
gateway prefixes (``cvp/claude-sonnet-5-5`` → ``claude-sonnet-5-5``); a per-provider alias maps
ids that don't match (``my-sonnet`` → ``anthropic/claude-sonnet-5-5``). Providers whose ``/models``
describes each model (OpenRouter's ``architecture`` / ``supported_parameters``) are trusted for
their unaliased ids instead.
"""

import time
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel

from yggdrasil.config import APP_CONFIG

CATALOG_URL = "https://models.dev/api.json"
_TTL_S = 3600
# ``ModelCaps.catalog_id`` prefix for capabilities reported by the provider's own /models.
UPSTREAM_PREFIX = "upstream:"
# Same model listed by several providers: prefer the vendor's own entry.
_FIRST_PARTY = ("anthropic", "openai", "google", "deepseek", "xai", "mistral", "alibaba", "moonshotai", "zhipuai")

_cache: tuple[float, "Catalog"] | None = None


class ModelCaps(BaseModel):
    catalog_id: str  # models.dev "provider/model", or UPSTREAM_PREFIX + the provider's model id
    vision: bool
    pdf: bool
    tools: bool
    reasoning: bool
    context: int | None = None
    output: int | None = None
    image_output: bool = False  # generates images (the `image` task)


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

    def resolve(
        self,
        models: Iterable[str],
        aliases: Mapping[str, str],
        upstream: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Capabilities per provider model id (JSON-ready); unmatched ids are left out.

        An alias always wins (looked up here); otherwise the provider's own ``upstream`` caps, then
        a lookup of the id itself.
        """
        upstream = upstream or {}
        out: dict[str, dict[str, Any]] = {}
        for mid in sorted({*models, *aliases}):
            if alias := aliases.get(mid, "").strip():
                caps = self.lookup(alias)
            elif mid in upstream:
                out[mid] = dict(upstream[mid])
                continue
            else:
                caps = self.lookup(mid)
            if caps:
                out[mid] = caps.model_dump()
        return out


def _caps(catalog_id: str, m: Mapping[str, Any]) -> ModelCaps:
    modalities = m.get("modalities") or {}
    inputs = modalities.get("input") or []
    limit = m.get("limit") or {}
    return ModelCaps(
        catalog_id=catalog_id,
        vision="image" in inputs,
        pdf="pdf" in inputs,
        tools=bool(m.get("tool_call")),
        reasoning=bool(m.get("reasoning")),
        context=limit.get("context") or None,
        output=limit.get("output") or None,
        image_output="image" in (modalities.get("output") or []),
    )


def upstream_caps(model_id: str, m: Mapping[str, Any]) -> ModelCaps | None:
    """Capabilities from an OpenRouter-style /models item; None when it carries no ``architecture``."""
    arch = m.get("architecture")
    if not isinstance(arch, dict):
        return None
    inputs = _strs(arch.get("input_modalities"))
    params = _strs(m.get("supported_parameters"))
    top = m.get("top_provider")
    return ModelCaps(
        catalog_id=f"{UPSTREAM_PREFIX}{model_id}",
        vision="image" in inputs,
        pdf="file" in inputs,
        tools="tools" in params,
        reasoning="reasoning" in params or "include_reasoning" in params,
        context=_positive_int(m.get("context_length")),
        output=_positive_int(top.get("max_completion_tokens")) if isinstance(top, dict) else None,
        image_output="image" in _strs(arch.get("output_modalities")),
    )


def _strs(v: object) -> set[str]:
    return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def _positive_int(v: object) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else None


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

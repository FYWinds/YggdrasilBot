"""Task → model resolution against the runtime config, plus embedding/rerank clients.

Chat models use the provider's ``api_format``: OpenAI Chat Completions or the Anthropic
Messages API (new-api serves both; its Claude channels only support tool calling via the
latter). Embeddings, rerank and model listing always use OpenAI-compatible routes.
"""

from dataclasses import dataclass
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.metrics import LLM_REQUESTS, LLM_TOKENS
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.llm.streaming import MeteredModel
from yggdrasil.runtime import ProviderInfo, Route, Snapshot, snapshot

TASKS = (
    "chat",
    "tldr",
    "quick_ack",
    "ops",
    "vision",
    "moderation",
    "embedding",
    "rerank",
    "memory_topic",
    "memory_glossary",
    "memory_profile",
    "memory_digest",
)
CHAT_TASKS = tuple(t for t in TASKS if t not in ("moderation", "embedding", "rerank"))
# Capabilities a route's models must have; checked on save when the catalog knows the model.
# Agents with tools or structured output (pydantic-ai output tools) need tool calling.
TASK_NEEDS: dict[str, tuple[str, ...]] = {
    "vision": ("vision",),
    **dict.fromkeys(
        ("chat", "tldr", "ops", "memory_topic", "memory_glossary", "memory_profile", "memory_digest"), ("tools",)
    ),
}

_SETTING_KEYS = {
    "temperature",
    "max_tokens",
    "top_p",
    "timeout",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "parallel_tool_calls",
    "extra_body",
    "extra_headers",
    # pydantic-ai's provider-neutral level (minimal … xhigh); validated in admin/routes.py.
    "thinking",
}
_EMBED_BATCH = 8


class TaskNotConfigured(RuntimeError):
    def __init__(self, task: str) -> None:
        super().__init__(f"no model route configured for task '{task}'")
        self.task = task


_clients: dict[tuple[int, str, str | None, str], AsyncOpenAI] = {}
_anthropic_clients: dict[tuple[int, str, str | None, str], AsyncAnthropic] = {}


def _client_key(p: ProviderInfo) -> tuple[int, str, str | None, str]:
    return (p.id, p.base_url, p.api_key, repr(sorted(p.headers.items())))


def openai_client(p: ProviderInfo) -> AsyncOpenAI:
    key = _client_key(p)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=p.base_url,
            api_key=p.api_key or "unused",
            max_retries=1,
            timeout=180.0,
            default_headers={"User-Agent": APP_CONFIG.llm_user_agent, **p.headers},
        )
        _clients[key] = client
    return client


def anthropic_client(p: ProviderInfo) -> AsyncAnthropic:
    key = _client_key(p)
    client = _anthropic_clients.get(key)
    if client is None:
        # The SDK appends /v1/messages itself; providers store base URLs including /v1.
        base = p.base_url.removesuffix("/v1")
        client = AsyncAnthropic(
            base_url=base,
            api_key=p.api_key or "unused",
            max_retries=1,
            timeout=180.0,
            default_headers={"User-Agent": APP_CONFIG.llm_user_agent, **p.headers},
        )
        _anthropic_clients[key] = client
    return client


def _provider(snap: Snapshot, provider_id: int, task: str) -> ProviderInfo:
    p = snap.providers.get(provider_id)
    if p is None or not p.enabled:
        raise TaskNotConfigured(task)
    return p


def route(task: str, snap: Snapshot | None = None) -> Route:
    snap = snap or snapshot()
    r = snap.routes.get(task)
    if r is None:
        raise TaskNotConfigured(task)
    return r


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    task: str
    model: Model
    settings: ModelSettings
    limits: UsageLimits
    label: str  # "provider/model" of the primary


# Newer Claude models (and some gateways) reject a forced tool_choice, which pydantic-ai sends for
# structured output. Without it the model gets tool_choice=auto and pydantic-ai retries if it answers
# in plain text. Route param ``forced_tool_choice: true`` restores forcing.
_NO_FORCED_TOOL_CHOICE = ModelProfile(supports_forced_tool_choice=False)


# models.dev provider ids → how the vendor writes its name.
_VENDORS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "mistral": "Mistral",
    "alibaba": "Alibaba",
    "moonshotai": "Moonshot AI",
    "zhipuai": "Zhipu AI",
}


def display_name(p: ProviderInfo, model: str) -> str:
    """The model as users may see it: the models.dev match, never our provider or gateway prefixes."""
    if caps := p.caps.get(model):
        vendor, _, name = caps.catalog_id.partition("/")
        return f"{_VENDORS.get(vendor, vendor.capitalize())}/{name}"
    return model.rsplit("/", 1)[-1]


def _build_model(task: str, name: str, p: ProviderInfo, forced_tool_choice: bool) -> Model:
    profile = None if forced_tool_choice else _NO_FORCED_TOOL_CHOICE
    if p.api_format == "anthropic":
        inner: Model = AnthropicModel(
            name, provider=AnthropicProvider(anthropic_client=anthropic_client(p)), profile=profile
        )
    else:
        inner = OpenAIChatModel(name, provider=OpenAIProvider(openai_client=openai_client(p)), profile=profile)
    return MeteredModel(inner, task=task, label=f"{p.name}/{name}", display=display_name(p, name))


def chat_model(task: str, snap: Snapshot | None = None, *, default_limits: UsageLimits | None = None) -> ResolvedModel:
    snap = snap or snapshot()
    r = route(task, snap)
    primary_p = _provider(snap, r.provider_id, task)
    forced = bool(r.params.get("forced_tool_choice", False))
    primary = _build_model(task, r.model, primary_p, forced)
    model = primary
    if r.fallback_provider_id and r.fallback_model:
        fb_p = snap.providers.get(r.fallback_provider_id)
        if fb_p and fb_p.enabled:
            model = FallbackModel(primary, _build_model(task, r.fallback_model, fb_p, forced))
    settings: dict[str, Any] = {k: v for k, v in r.params.items() if k in _SETTING_KEYS}
    limits = default_limits or UsageLimits()
    if r.limits:
        limits = UsageLimits(
            request_limit=r.limits.get("request_limit", limits.request_limit),
            tool_calls_limit=r.limits.get("tool_calls_limit", limits.tool_calls_limit),
            total_tokens_limit=r.limits.get("total_tokens_limit", limits.total_tokens_limit),
        )
    return ResolvedModel(task, model, ModelSettings(**settings), limits, f"{primary_p.name}/{r.model}")


def route_caps(task: str, snap: Snapshot | None = None) -> list[ModelCaps | None]:
    """Capabilities of the route's primary and (enabled) fallback model; None where unknown."""
    snap = snap or snapshot()
    r = snap.routes.get(task)
    if r is None:
        return []
    pairs = [(r.provider_id, r.model)]
    if r.fallback_provider_id and r.fallback_model:
        fb = snap.providers.get(r.fallback_provider_id)
        if fb and fb.enabled:
            pairs.append((r.fallback_provider_id, r.fallback_model))
    return [p.caps.get(m) if (p := snap.providers.get(pid)) else None for pid, m in pairs]


def sees_images(task: str, snap: Snapshot | None = None) -> bool:
    """Whether every model that may serve ``task`` is known to accept image input."""
    caps = route_caps(task, snap)
    return bool(caps) and all(c is not None and c.vision for c in caps)


def record_usage(task: str, model: str, *, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
    attrs = {"task": task, "model": model}
    LLM_REQUESTS.add(1, attrs)
    LLM_TOKENS.add(input_tokens, {**attrs, "direction": "input"})
    LLM_TOKENS.add(output_tokens, {**attrs, "direction": "output"})
    if cached:
        LLM_TOKENS.add(cached, {**attrs, "direction": "cached"})


async def embed(
    texts: list[str], *, snap: Snapshot | None = None, provider_id: int | None = None, model: str | None = None
) -> list[list[float]]:
    """Embed with the active embedding space (or an explicit provider/model during re-embedding)."""
    snap = snap or snapshot()
    if provider_id is None or model is None:
        if snap.embed_space is not None:
            provider_id, model = snap.embed_space.provider_id, snap.embed_space.model
        else:
            r = route("embedding", snap)
            provider_id, model = r.provider_id, r.model
    p = _provider(snap, provider_id, "embedding")
    out: list[list[float]] = []
    # CPU embedding runs ~220 tok/s and texts reach ~1900 tokens (EMBED_MAX_CHARS): 8 per request stays
    # well inside the client's 180 s timeout; 32 took ~270 s, timed out and was retried forever.
    for i in range(0, len(texts), _EMBED_BATCH):
        resp = await openai_client(p).embeddings.create(model=model, input=texts[i : i + _EMBED_BATCH])
        out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        if resp.usage:
            record_usage("embedding", model, input_tokens=resp.usage.prompt_tokens, output_tokens=0)
    return out


async def rerank(
    query: str, documents: list[str], *, top_n: int | None = None, snap: Snapshot | None = None
) -> list[tuple[int, float]]:
    """Jina/Cohere-style ``POST {base}/rerank`` (llama.cpp ``--rerank`` and new-api both speak it)."""
    if not documents:
        return []
    snap = snap or snapshot()
    r = route("rerank", snap)
    p = _provider(snap, r.provider_id, "rerank")
    headers = {"User-Agent": APP_CONFIG.llm_user_agent, **p.headers}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{p.base_url}/rerank",
            json={"model": r.model, "query": query, "documents": documents, "top_n": top_n or len(documents)},
            headers=headers,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    LLM_REQUESTS.add(1, {"task": "rerank", "model": r.model})
    return sorted(((int(x["index"]), float(x["relevance_score"])) for x in results), key=lambda t: -t[1])

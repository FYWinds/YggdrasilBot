"""Task → model resolution against the runtime config, plus embedding/rerank clients.

All providers speak the OpenAI-compatible API (new-api, TEI, llama.cpp server).
"""

from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.metrics import LLM_REQUESTS, LLM_TOKENS
from yggdrasil.runtime import ProviderInfo, Route, Snapshot, snapshot

TASKS = (
    "chat",
    "tldr",
    "quick_ack",
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
}


class TaskNotConfigured(RuntimeError):
    def __init__(self, task: str) -> None:
        super().__init__(f"no model route configured for task '{task}'")
        self.task = task


_clients: dict[tuple[int, str, str | None, str], AsyncOpenAI] = {}


def openai_client(p: ProviderInfo) -> AsyncOpenAI:
    key = (p.id, p.base_url, p.api_key, repr(sorted(p.headers.items())))
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=p.base_url,
            api_key=p.api_key or "unused",
            max_retries=1,
            timeout=httpx.Timeout(180, connect=10),
            default_headers={"User-Agent": APP_CONFIG.llm_user_agent, **p.headers},
        )
        _clients[key] = client
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


def chat_model(task: str, snap: Snapshot | None = None, *, default_limits: UsageLimits | None = None) -> ResolvedModel:
    snap = snap or snapshot()
    r = route(task, snap)
    primary_p = _provider(snap, r.provider_id, task)
    primary: Model = OpenAIChatModel(r.model, provider=OpenAIProvider(openai_client=openai_client(primary_p)))
    model = primary
    if r.fallback_provider_id and r.fallback_model:
        fb_p = snap.providers.get(r.fallback_provider_id)
        if fb_p and fb_p.enabled:
            model = FallbackModel(
                primary, OpenAIChatModel(r.fallback_model, provider=OpenAIProvider(openai_client=openai_client(fb_p)))
            )
    settings: dict[str, Any] = {k: v for k, v in r.params.items() if k in _SETTING_KEYS}
    limits = default_limits or UsageLimits()
    if r.limits:
        limits = UsageLimits(
            request_limit=r.limits.get("request_limit", limits.request_limit),
            tool_calls_limit=r.limits.get("tool_calls_limit", limits.tool_calls_limit),
            total_tokens_limit=r.limits.get("total_tokens_limit", limits.total_tokens_limit),
        )
    return ResolvedModel(task, model, ModelSettings(**settings), limits, f"{primary_p.name}/{r.model}")


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
    for i in range(0, len(texts), 32):
        resp = await openai_client(p).embeddings.create(model=model, input=texts[i : i + 32])
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

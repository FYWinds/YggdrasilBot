"""Scoped relay tokens, their per-job usage, and the models a token may be granted.

Each background agent job gets one random token. Code inside the workspace can read it, so the real
controls are the grant: model allowlist, input/output token budget and expiry. Redis keeps only the
token's sha256 (``ygg:relay:token:<sha256>`` → grant JSON, expiring with the grant), the job's current
token (``ygg:relay:job:<job id>`` → sha256, for revocation), the job's usage hash
(``ygg:relay:usage:<job id>``, kept a week after the last request so the report can read it) and its
in-flight output reservations (``ygg:relay:pending:<job id>``: request id → ``"<tokens>:<expires epoch>"``).

Concurrent requests can't overrun the output budget: each one reserves its ``max_tokens`` (clamped to what
is left) atomically with the budget check and releases it when its usage is recorded. A reservation whose
request never released it (process killed mid-stream) lapses after ``_RESERVATION_S``.
"""

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass

from yggdrasil.config import APP_CONFIG
from yggdrasil.kv import redis_client
from yggdrasil.llm.registry import display_name
from yggdrasil.runtime import ProviderInfo, Snapshot, snapshot

_PREFIX = "ygg:relay"
_USAGE_KEEP_S = 7 * 86400
# Longer than any one generation can stream.
_RESERVATION_S = 3600
# Model metadata for omp when models.dev doesn't know the model.
_DEFAULT_CONTEXT = 200_000
_DEFAULT_MAX_TOKENS = 32_000
ROUTE_TASK = "chat"


@dataclass(frozen=True)
class RelayModel:
    id: str  # model id omp requests, e.g. "claude-opus-5-5"
    name: str
    context_window: int
    max_tokens: int
    reasoning: bool
    vision: bool


@dataclass(frozen=True)
class RelayUsage:
    requests: int
    input_tokens: int  # cache reads and writes included
    cached_tokens: int  # cache reads
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Grant:
    group_id: int
    job_id: str
    models: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    expires_at: float  # epoch seconds


@dataclass(frozen=True, slots=True)
class Target:
    """One upstream a relay model id is served by."""

    provider: ProviderInfo
    model: str  # upstream model id

    @property
    def label(self) -> str:
        return f"{self.provider.name}/{self.model}"


def token_key(digest: str) -> str:
    return f"{_PREFIX}:token:{digest}"


def job_key(job_id: str) -> str:
    return f"{_PREFIX}:job:{job_id}"


def usage_key(job_id: str) -> str:
    return f"{_PREFIX}:usage:{job_id}"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending_key(job_id: str) -> str:
    return f"{_PREFIX}:pending:{job_id}"


_serving = False


def configured_url() -> str | None:
    """``YGG_RELAY_URL`` normalized; whether the relay should be served."""
    url = (APP_CONFIG.relay_url or "").strip().rstrip("/")
    return url or None


def set_serving(up: bool) -> None:
    global _serving
    _serving = up


def relay_url() -> str | None:
    """Base URL (no ``/v1``) sandboxes reach the relay at; None when it is not configured or this process
    isn't serving it (not started, or the port couldn't be bound)."""
    return configured_url() if _serving else None


def route_targets(snap: Snapshot | None = None) -> dict[str, list[Target]]:
    """Relay model id → upstreams to try in order.

    The chat route's primary and fallback, restricted to enabled Anthropic-format providers. A request
    for the primary falls through to the fallback like the bot's own FallbackModel does; the fallback
    model can also be requested directly.
    """
    snap = snap or snapshot()
    r = snap.routes.get(ROUTE_TASK)
    if r is None:
        return {}
    pairs = [(r.provider_id, r.model)]
    if r.fallback_provider_id and r.fallback_model:
        pairs.append((r.fallback_provider_id, r.fallback_model))
    entries: list[Target] = []
    for pid, model in pairs:
        p = snap.providers.get(pid)
        if p is None or not p.enabled or p.api_format != "anthropic":
            continue
        if not any(t.provider.id == pid and t.model == model for t in entries):
            entries.append(Target(p, model))
    out: dict[str, list[Target]] = {}
    for i, t in enumerate(entries):
        out.setdefault(t.model, entries[i:])
    return out


async def relay_models() -> list[RelayModel]:
    """Models a grant may allow, primary first."""
    out: list[RelayModel] = []
    for model_id, targets in route_targets().items():
        p = targets[0].provider
        caps = p.caps.get(model_id)
        out.append(
            RelayModel(
                id=model_id,
                name=display_name(p, model_id),
                context_window=(caps.context if caps else None) or _DEFAULT_CONTEXT,
                max_tokens=(caps.output if caps else None) or _DEFAULT_MAX_TOKENS,
                reasoning=bool(caps and caps.reasoning),
                vision=bool(caps and caps.vision),
            )
        )
    return out


async def issue_token(
    *, group_id: int, job_id: str, models: list[str], max_input_tokens: int, max_output_tokens: int, ttl_s: int
) -> str:
    """A fresh token for ``job_id``; a token the job already had stops working. Usage keeps accumulating."""
    if not models:
        raise ValueError("a relay grant needs at least one model")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    token = secrets.token_urlsafe(32)
    key = digest(token)
    grant = Grant(
        group_id=group_id,
        job_id=job_id,
        models=tuple(models),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        expires_at=time.time() + ttl_s,
    )
    redis = redis_client()
    old = await redis.get(job_key(job_id))
    async with redis.pipeline(transaction=True) as pipe:
        if old:
            pipe.delete(token_key(str(old)))
        pipe.set(token_key(key), json.dumps(asdict(grant)), ex=ttl_s)
        pipe.set(job_key(job_id), key, ex=ttl_s)
        await pipe.execute()
    return token


async def revoke_token(job_id: str) -> None:
    redis = redis_client()
    key = await redis.getdel(job_key(job_id))
    await redis.delete(pending_key(job_id), *([token_key(str(key))] if key else []))


async def relay_usage(job_id: str) -> RelayUsage:
    raw = await redis_client().hgetall(usage_key(job_id))
    return RelayUsage(
        requests=int(raw.get("requests", 0)),
        input_tokens=int(raw.get("input_tokens", 0)),
        cached_tokens=int(raw.get("cached_tokens", 0)),
        output_tokens=int(raw.get("output_tokens", 0)),
    )


async def authenticate(token: str) -> Grant | None:
    """The live grant behind ``token``; None when unknown, revoked or expired."""
    raw = await redis_client().get(token_key(digest(token)))
    if not raw:
        return None
    data = json.loads(raw)
    grant = Grant(**{**data, "models": tuple(data["models"])})
    return grant if grant.expires_at > time.time() else None


@dataclass(frozen=True, slots=True)
class Budget:
    granted: int  # output tokens reserved for the request; 0 = refused
    input_used: int
    output_used: int
    output_pending: int  # other requests' reservations


# KEYS: usage hash, pending hash. ARGV: now, max input, max output, wanted output, request id, reservation
# expiry. Drops lapsed reservations, refuses when the input budget is used up or no output is left,
# otherwise reserves min(wanted, left).
_RESERVE = """
local now = tonumber(ARGV[1])
local used_in = tonumber(redis.call('HGET', KEYS[1], 'input_tokens') or '0')
local used_out = tonumber(redis.call('HGET', KEYS[1], 'output_tokens') or '0')
local pending = 0
local entries = redis.call('HGETALL', KEYS[2])
for i = 1, #entries, 2 do
  local tokens, expires = string.match(entries[i + 1], '^(%d+):(%d+)$')
  if tokens == nil or tonumber(expires) <= now then
    redis.call('HDEL', KEYS[2], entries[i])
  else
    pending = pending + tonumber(tokens)
  end
end
local left = tonumber(ARGV[3]) - used_out - pending
if used_in >= tonumber(ARGV[2]) or left <= 0 then
  return {0, used_in, used_out, pending}
end
local granted = math.min(tonumber(ARGV[4]), left)
redis.call('HSET', KEYS[2], ARGV[5], granted .. ':' .. ARGV[6])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[6]) - now)
return {granted, used_in, used_out, pending}
"""


async def reserve_output(grant: Grant, request_id: str, wanted: int) -> Budget:
    """Check the budget and reserve up to ``wanted`` output tokens for one request."""
    now = int(time.time())
    res = await redis_client().eval(  # pyright: ignore[reportGeneralTypeIssues]
        _RESERVE,
        2,
        usage_key(grant.job_id),
        pending_key(grant.job_id),
        now,
        grant.max_input_tokens,
        grant.max_output_tokens,
        max(wanted, 1),
        request_id,
        now + _RESERVATION_S,
    )
    granted, used_in, used_out, pending = (int(x) for x in res)
    return Budget(granted, used_in, used_out, pending)


async def release(grant: Grant, request_id: str) -> None:
    await redis_client().hdel(pending_key(grant.job_id), request_id)  # pyright: ignore[reportGeneralTypeIssues]


async def add_usage(
    grant: Grant, request_id: str, *, input_tokens: int, cached_tokens: int, output_tokens: int
) -> None:
    """Record one request's usage and drop its reservation."""
    key = usage_key(grant.job_id)
    keep = max(grant.expires_at - time.time(), 0) + _USAGE_KEEP_S
    async with redis_client().pipeline(transaction=True) as pipe:
        pipe.hincrby(key, "requests", 1)
        pipe.hincrby(key, "input_tokens", input_tokens)
        pipe.hincrby(key, "cached_tokens", cached_tokens)
        pipe.hincrby(key, "output_tokens", output_tokens)
        pipe.expire(key, int(keep))
        pipe.hdel(pending_key(grant.job_id), request_id)
        await pipe.execute()

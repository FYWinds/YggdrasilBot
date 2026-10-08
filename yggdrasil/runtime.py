"""Hot-reloadable runtime configuration.

Readers call :func:`snapshot` (cheap, immutable). Writers (admin API, CLI) change
the database and call :func:`publish_change`; every process listening on the
Redis channel reloads its snapshot.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import (
    Admin,
    CommandTrigger,
    EmbedSpace,
    Group,
    Prompt,
    PromptVersion,
    Provider,
    SettingsDoc,
    TaskRoute,
)
from yggdrasil.db.session import get_session
from yggdrasil.kv import redis_client
from yggdrasil.settings import Settings, effective_settings

CHANNEL = "ygg:config"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: int
    name: str
    base_url: str
    api_key: str | None
    headers: dict[str, str]
    enabled: bool
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Route:
    task: str
    provider_id: int
    model: str
    params: dict[str, Any]
    fallback_provider_id: int | None
    fallback_model: str | None
    limits: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GroupInfo:
    group_id: int
    name: str
    enabled: bool
    settings_doc: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TriggerRule:
    id: int
    handler: str
    scope: str
    group_id: int | None
    match_type: str
    patterns: tuple[str, ...]
    arg_template: str | None
    require_mention: bool
    target: str
    priority: int
    cooldown_s: int
    allowed_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EmbedSpaceInfo:
    id: int
    provider_id: int
    model: str
    dim: int


@dataclass(frozen=True)
class Snapshot:
    version: int = 0
    providers: dict[int, ProviderInfo] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    groups: dict[int, GroupInfo] = field(default_factory=dict)
    global_settings_doc: dict[str, Any] = field(default_factory=dict)
    triggers: tuple[TriggerRule, ...] = ()
    prompts: dict[str, str] = field(default_factory=dict)
    superusers: frozenset[int] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    embed_space: EmbedSpaceInfo | None = None
    _settings_cache: dict[int | None, Settings] = field(default_factory=dict, repr=False)

    def settings(self, group_id: int | None = None) -> Settings:
        cached = self._settings_cache.get(group_id)
        if cached is None:
            group = self.groups.get(group_id) if group_id is not None else None
            cached = effective_settings(self.global_settings_doc, group.settings_doc if group else None)
            self._settings_cache[group_id] = cached
        return cached

    def group_enabled(self, group_id: int) -> bool:
        group = self.groups.get(group_id)
        return group is not None and group.enabled


_snapshot = Snapshot()
_listeners: list[Callable[[Snapshot], Awaitable[None] | None]] = []


def snapshot() -> Snapshot:
    return _snapshot


def on_reload(fn: Callable[[Snapshot], Awaitable[None] | None]) -> None:
    """Register a callback run after every reload (e.g. recompiling the trigger index)."""
    _listeners.append(fn)


async def load() -> Snapshot:
    global _snapshot
    async with get_session() as session:
        providers = {}
        for p in (await session.scalars(select(Provider))).all():
            key = None
            if p.api_key_enc:
                try:
                    key = decrypt(p.api_key_enc)
                except Exception:
                    LOGGER.error("cannot decrypt api key of provider {} (YGG_SECRET_KEY changed?)", p.name)
            providers[p.id] = ProviderInfo(
                p.id, p.name, p.base_url.rstrip("/"), key, dict(p.headers), p.enabled, tuple(p.models or ())
            )
        routes = {
            r.task: Route(
                r.task, r.provider_id, r.model, dict(r.params), r.fallback_provider_id, r.fallback_model, dict(r.limits)
            )
            for r in (await session.scalars(select(TaskRoute))).all()
        }
        groups = {
            g.group_id: GroupInfo(g.group_id, g.name, g.enabled, dict(g.settings))
            for g in (await session.scalars(select(Group))).all()
        }
        doc = await session.get(SettingsDoc, "global")
        triggers = tuple(
            TriggerRule(
                t.id,
                t.handler,
                t.scope,
                t.group_id,
                t.match_type,
                tuple(t.patterns),
                t.arg_template,
                t.require_mention,
                t.target,
                t.priority,
                t.cooldown_s,
                tuple(t.allowed_roles),
            )
            for t in (
                await session.scalars(select(CommandTrigger).where(CommandTrigger.enabled).order_by(CommandTrigger.id))
            ).all()
        )
        prompt_rows = await session.execute(
            select(Prompt.key, PromptVersion.body).join(PromptVersion, PromptVersion.id == Prompt.active_version_id)
        )
        prompts = dict(prompt_rows.all())
        admins = (await session.scalars(select(Admin))).all()
        space = await session.scalar(select(EmbedSpace).where(EmbedSpace.active))

    superusers = {int(a.value) for a in admins if a.kind == "qq"} | set(APP_CONFIG.bootstrap_superusers)
    emails = {a.value.lower() for a in admins if a.kind == "email"} | {
        e.lower() for e in APP_CONFIG.bootstrap_admin_emails
    }
    _snapshot = Snapshot(
        version=_snapshot.version + 1,
        providers=providers,
        routes=routes,
        groups=groups,
        global_settings_doc=dict(doc.value) if doc else {},
        triggers=triggers,
        prompts=prompts,
        superusers=frozenset(superusers),
        admin_emails=frozenset(emails),
        embed_space=EmbedSpaceInfo(space.id, space.provider_id, space.model, space.dim) if space else None,
    )
    for fn in _listeners:
        result = fn(_snapshot)
        if asyncio.iscoroutine(result):
            await result
    LOGGER.info(
        "runtime config v{} loaded: {} providers, {} routes, {} groups, {} triggers",
        _snapshot.version,
        len(providers),
        len(routes),
        len(groups),
        len(triggers),
    )
    return _snapshot


async def publish_change(reason: str) -> None:
    await redis_client().publish(CHANNEL, reason)


async def listen_forever() -> None:
    """Reload on every published change. Reconnects with backoff."""
    delay = 1.0
    while True:
        try:
            pubsub = aioredis.Redis.from_pool(redis_client().connection_pool).pubsub()
            await pubsub.subscribe(CHANNEL)
            delay = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                LOGGER.info("config change: {}", message.get("data"))
                try:
                    await load()
                except Exception:
                    LOGGER.exception("config reload failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("config listener error; retrying in {}s", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

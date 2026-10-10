"""Runtime configuration edited from the dashboard and hot-reloaded via Redis pub/sub."""

from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, BigInteger, ForeignKey, Identity, Index, LargeBinary, PrimaryKeyConstraint, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base, TimestampMixin


class Provider(Base, TimestampMixin):
    """An OpenAI-compatible endpoint (new-api, TEI, llama.cpp, ...)."""

    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    # Wire format for chat: openai (Chat Completions) | anthropic (Messages API). Embeddings, rerank
    # and /models always use the OpenAI-compatible routes.
    api_format: Mapped[str] = mapped_column(Text, default="openai", server_default="openai")
    base_url: Mapped[str] = mapped_column(Text)  # including /v1
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    models: Mapped[list[Any]] = mapped_column(default=list)  # cached /v1/models ids
    models_fetched_at: Mapped[datetime | None]
    # model id -> models.dev id, for ids the automatic prefix-stripping match can't resolve
    model_aliases: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    # model id -> yggdrasil.llm.catalog.ModelCaps, refreshed with the model list / on alias edits
    model_caps: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class TaskRoute(Base, TimestampMixin):
    """Which model serves a task (chat, tldr, quick_ack, embedding, moderation, ...)."""

    __tablename__ = "task_routes"

    task: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"))
    model: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)  # temperature, max_tokens, ...
    fallback_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id", ondelete="SET NULL"))
    fallback_model: Mapped[str | None] = mapped_column(Text)
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)  # request_limit, tool_calls_limit


class Group(Base, TimestampMixin):
    __tablename__ = "groups"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=False)  # whitelist
    # Deep-merged over the global settings document (see yggdrasil.settings.Settings).
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    backfilled_at: Mapped[datetime | None]


class CommandTrigger(Base, TimestampMixin):
    __tablename__ = "command_triggers"
    __table_args__ = (Index("ix_command_triggers_scope", "scope", "group_id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    handler: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="global")  # global | group | private
    group_id: Mapped[int | None]
    match_type: Mapped[str] = mapped_column(Text)
    patterns: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    arg_template: Mapped[str | None] = mapped_column(Text)
    require_mention: Mapped[bool] = mapped_column(default=False)
    target: Mapped[str] = mapped_column(Text, default="quoted_or_self")
    priority: Mapped[int] = mapped_column(default=100)
    enabled: Mapped[bool] = mapped_column(default=True)
    cooldown_s: Mapped[int] = mapped_column(default=0)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    note: Mapped[str] = mapped_column(Text, default="")


class Prompt(Base, TimestampMixin):
    __tablename__ = "prompts"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    active_version_id: Mapped[int | None]


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (Index("ix_prompt_versions_key", "key", "id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Admin(Base):
    """``kind``: qq (superuser: reviews, admin commands) | email (dashboard access)."""

    __tablename__ = "admins"
    __table_args__ = (PrimaryKeyConstraint("kind", "value"),)

    kind: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SettingsDoc(Base, TimestampMixin):
    """Global settings document (key ``global``), validated by yggdrasil.settings.Settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)


class EmbedSpace(Base, TimestampMixin):
    __tablename__ = "embed_spaces"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"))
    model: Mapped[str] = mapped_column(Text)
    dim: Mapped[int]
    active: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="backfilling")  # backfilling | ready


class PipelineState(Base, TimestampMixin):
    """Per-group watermarks so memory pipelines are resumable and replayable."""

    __tablename__ = "pipeline_state"
    __table_args__ = (PrimaryKeyConstraint("pipeline", "group_id"),)

    pipeline: Mapped[str] = mapped_column(Text)
    group_id: Mapped[int]
    last_msg_id: Mapped[int] = mapped_column(default=0)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ConfigAudit(Base):
    __tablename__ = "config_audit"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

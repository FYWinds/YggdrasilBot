"""baseline

Revision ID: 3eacd73e5f16
Revises:
Create Date: 2026-10-07 23:03:05.033448
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from yggdrasil.db.partitions import months_to_ensure, partition_ddl

revision: str = "3eacd73e5f16"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
    op.create_table(
        "admins",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("kind", "value", name=op.f("pk_admins")),
    )
    op.create_table(
        "command_triggers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("handler", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("match_type", sa.Text(), nullable=False),
        sa.Column("patterns", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("arg_template", sa.Text(), nullable=True),
        sa.Column("require_mention", sa.Boolean(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("priority", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cooldown_s", sa.BigInteger(), nullable=False),
        sa.Column("allowed_roles", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_triggers")),
    )
    op.create_index("ix_command_triggers_scope", "command_triggers", ["scope", "group_id"], unique=False)
    op.create_table(
        "config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tags", sa.ARRAY(sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_audit")),
    )
    op.create_table(
        "daily_digests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("topic_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_digests")),
        sa.UniqueConstraint("group_id", "day", name=op.f("uq_daily_digests_group_id_day")),
    )
    op.create_table(
        "glossary_candidates",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("example_msg_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "term", name=op.f("pk_glossary_candidates")),
    )
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("term", sa.Text(), nullable=False),
        sa.Column("aliases", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hits", sa.BigInteger(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_glossary_terms")),
        sa.UniqueConstraint("group_id", "term", name=op.f("uq_glossary_terms_group_id_term")),
    )
    op.create_table(
        "groups",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("group_id", name=op.f("pk_groups")),
    )
    op.create_table(
        "invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("handler", sa.Text(), nullable=False),
        sa.Column("trigger_qq_message_id", sa.BigInteger(), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False),
        sa.Column("tokens_cached", sa.BigInteger(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ack_kind", sa.Text(), nullable=True),
        sa.Column("ack_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invocations")),
    )
    op.create_index("ix_invocations_created", "invocations", ["created_at"], unique=False)
    op.create_index("ix_invocations_group_created", "invocations", ["group_id", "created_at"], unique=False)
    op.create_table(
        "message_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("first_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("msg_count", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_chunks")),
    )
    op.create_index("ix_message_chunks_group_end", "message_chunks", ["group_id", "end_at"], unique=False)
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("qq_message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_seq", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_qq_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_name", sa.Text(), nullable=False),
        sa.Column("plain_text", sa.Text(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_self", sa.Boolean(), nullable=False),
        sa.Column("recalled", sa.Boolean(), nullable=False),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", "sent_at", name=op.f("pk_messages")),
        postgresql_partition_by="RANGE (sent_at)",
    )
    op.create_index("ix_messages_group_qq", "messages", ["group_id", "qq_message_id"], unique=False)
    op.create_index("ix_messages_group_sent", "messages", ["group_id", sa.literal_column("sent_at DESC")], unique=False)
    op.create_index(
        "ix_messages_group_user_sent",
        "messages",
        ["group_id", "user_id", sa.literal_column("sent_at DESC")],
        unique=False,
    )
    op.create_table(
        "pipeline_state",
        sa.Column("pipeline", sa.Text(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("pipeline", "group_id", name=op.f("pk_pipeline_state")),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
    )
    op.create_index("ix_prompt_versions_key", "prompt_versions", ["key", "id"], unique=False)
    op.create_table(
        "prompts",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("active_version_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_prompts")),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("models_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_providers_name")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "stickers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("emoji_id", sa.Text(), nullable=False),
        sa.Column("package_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("seen_count", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stickers")),
        sa.UniqueConstraint("package_id", "emoji_id", name=op.f("uq_stickers_package_id_emoji_id")),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("keywords", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("participants", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("last_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("msg_count", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
    )
    op.create_index("ix_topics_group_status", "topics", ["group_id", "status", "end_at"], unique=False)
    op.create_table(
        "user_aliases",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id", "name", name=op.f("pk_user_aliases")),
    )
    op.create_table(
        "user_profile_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("fact", sa.Text(), nullable=False),
        sa.Column("source_msg_ids", sa.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profile_facts")),
    )
    op.create_index("ix_user_profile_facts_user", "user_profile_facts", ["group_id", "user_id"], unique=False)
    op.create_table(
        "user_profiles",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("interests", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("traits", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("opted_out", sa.Boolean(), nullable=False),
        sa.Column("last_topic_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_user_profiles")),
    )
    op.create_table(
        "embed_spaces",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.BigInteger(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["providers.id"], name=op.f("fk_embed_spaces_provider_id_providers"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embed_spaces")),
    )
    op.create_table(
        "moderation_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("categories", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_moderation_results_invocation_id_invocations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_moderation_results")),
    )
    op.create_index("ix_moderation_results_created", "moderation_results", ["created_at"], unique=False)
    op.create_table(
        "review_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_qq_message_id", sa.BigInteger(), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("reply", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.id"],
            name=op.f("fk_review_items_invocation_id_invocations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_items")),
    )
    op.create_index("ix_review_items_status", "review_items", ["status", "expires_at"], unique=False)
    op.create_table(
        "task_routes",
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fallback_provider_id", sa.BigInteger(), nullable=True),
        sa.Column("fallback_model", sa.Text(), nullable=True),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["fallback_provider_id"],
            ["providers.id"],
            name=op.f("fk_task_routes_fallback_provider_id_providers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["providers.id"], name=op.f("fk_task_routes_provider_id_providers"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("task", name=op.f("pk_task_routes")),
    )
    op.create_table(
        "topic_messages",
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["topic_id"], ["topics.id"], name=op.f("fk_topic_messages_topic_id_topics"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("topic_id", "message_id", name=op.f("pk_topic_messages")),
    )
    op.create_index("ix_topic_messages_message", "topic_messages", ["message_id"], unique=False)
    op.create_table(
        "memory_vectors",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.BigInteger(), nullable=False),
        sa.Column("space_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.halfvec.HALFVEC(dim=1024), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"], ["embed_spaces.id"], name=op.f("fk_memory_vectors_space_id_embed_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("kind", "ref_id", "space_id", name=op.f("pk_memory_vectors")),
    )
    op.create_index(
        "ix_memory_vectors_hnsw",
        "memory_vectors",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )
    op.create_index("ix_memory_vectors_scope", "memory_vectors", ["group_id", "space_id", "kind"], unique=False)

    # Monthly partitions around "now"; the worker keeps three months of runway afterwards.
    for month in months_to_ensure(datetime.now(UTC).date(), back=1, ahead=3):
        op.execute(partition_ddl(month))
    # BM25 over message text (jieba tokenizer) with group/recall/time as filterable fast fields.
    # Defined on the parent: Postgres creates it on every current and future partition.
    op.execute(
        "CREATE INDEX messages_bm25 ON messages USING bm25 "
        "(id, (plain_text::pdb.jieba), group_id, user_id, sent_at, recalled, is_self)"
    )
    op.execute(
        "CREATE INDEX topics_bm25 ON topics USING bm25 (id, (title::pdb.jieba), (summary::pdb.jieba), group_id, end_at)"
    )


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_memory_vectors_scope", table_name="memory_vectors")
    op.drop_index(
        "ix_memory_vectors_hnsw",
        table_name="memory_vectors",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )
    op.drop_table("memory_vectors")
    op.drop_index("ix_topic_messages_message", table_name="topic_messages")
    op.drop_table("topic_messages")
    op.drop_table("task_routes")
    op.drop_index("ix_review_items_status", table_name="review_items")
    op.drop_table("review_items")
    op.drop_index("ix_moderation_results_created", table_name="moderation_results")
    op.drop_table("moderation_results")
    op.drop_table("embed_spaces")
    op.drop_table("user_profiles")
    op.drop_index("ix_user_profile_facts_user", table_name="user_profile_facts")
    op.drop_table("user_profile_facts")
    op.drop_table("user_aliases")
    op.drop_index("ix_topics_group_status", table_name="topics")
    op.drop_table("topics")
    op.drop_table("stickers")
    op.drop_table("settings")
    op.drop_table("providers")
    op.drop_table("prompts")
    op.drop_index("ix_prompt_versions_key", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_table("pipeline_state")
    op.drop_index("ix_messages_group_user_sent", table_name="messages")
    op.drop_index("ix_messages_group_sent", table_name="messages")
    op.drop_index("ix_messages_group_qq", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_message_chunks_group_end", table_name="message_chunks")
    op.drop_table("message_chunks")
    op.drop_index("ix_invocations_group_created", table_name="invocations")
    op.drop_index("ix_invocations_created", table_name="invocations")
    op.drop_table("invocations")
    op.drop_table("groups")
    op.drop_table("glossary_terms")
    op.drop_table("glossary_candidates")
    op.drop_table("daily_digests")
    op.drop_table("config_audit")
    op.drop_index("ix_command_triggers_scope", table_name="command_triggers")
    op.drop_table("command_triggers")
    op.drop_table("admins")
    # ### end Alembic commands ###

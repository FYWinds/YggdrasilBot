"""search providers

Web search backends move from the YGG_SEARXNG_URL env var into a dashboard-managed pool. The
existing SearXNG becomes its first entry, read from that env var one last time (set it on the
migration job for this upgrade).

Revision ID: fefe8a002515
Revises: d4e9a7c1b3f2
Create Date: 2026-10-10 12:00:00.000000
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fefe8a002515"
down_revision: str | Sequence[str] | None = "d4e9a7c1b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_providers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.BigInteger(), nullable=False),
        sa.Column("monthly_limit", sa.BigInteger(), nullable=True),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_providers")),
        sa.UniqueConstraint("name", name=op.f("uq_search_providers_name")),
    )
    searxng_url = os.environ.get("YGG_SEARXNG_URL", "").strip().rstrip("/")
    if searxng_url:
        op.execute(
            sa.text(
                "INSERT INTO search_providers (name, kind, base_url, enabled, priority, params) "
                "VALUES ('searxng', 'searxng', :url, true, 1000, '{\"language\": \"zh-CN\"}')"
            ).bindparams(url=searxng_url)
        )


def downgrade() -> None:
    op.drop_table("search_providers")

"""sandbox job archive

Background jobs lived only in Redis with a 7-day TTL, and their event log was capped at 40 entries. Finished jobs
are now archived for good in ``sandbox_jobs`` with their whole event log.

Revision ID: d7a2f4c91b05
Revises: c3e8a1f2d4b7
Create Date: 2026-10-25 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a2f4c91b05"
down_revision: str | Sequence[str] | None = "c3e8a1f2d4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("events", postgresql.JSONB(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sandbox_jobs_created", "sandbox_jobs", [sa.text("created_at DESC")])
    op.create_index("ix_sandbox_jobs_group_created", "sandbox_jobs", ["group_id", sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_table("sandbox_jobs")

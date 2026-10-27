"""transcripts

The console shows each invocation and background job as its whole conversation (system prompt, user turn, model
turns with thinking and tool calls, tool results, images). ``transcripts`` holds one normalized conversation per
invocation or job; ``transcript_blobs`` the images, once per content hash.

Revision ID: e4b9c2d7a1f3
Revises: d7a2f4c91b05
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b9c2d7a1f3"
down_revision: str | Sequence[str] | None = "d7a2f4c91b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.Uuid(),
            sa.ForeignKey("invocations.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column("job_id", sa.Text(), nullable=True, unique=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "transcript_blobs",
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("transcript_blobs")
    op.drop_table("transcripts")

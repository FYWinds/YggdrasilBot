"""invocation sessions

A reply to one of the bot's answers continues that answer's agent conversation instead of starting over:
``invocation_sessions`` keeps the conversation of each delivered answer for a short while.

Revision ID: a3d8e6b1c470
Revises: c9e4a7f1d2b8
Create Date: 2026-11-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3d8e6b1c470"
down_revision: str | Sequence[str] | None = "c9e4a7f1d2b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invocation_sessions",
        sa.Column("invocation_id", sa.Uuid(), sa.ForeignKey("invocations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("messages", postgresql.JSONB(), nullable=False),
        sa.Column("handles", postgresql.JSONB(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("seen_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_invocation_sessions_created", "invocation_sessions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_invocation_sessions_created", table_name="invocation_sessions")
    op.drop_table("invocation_sessions")

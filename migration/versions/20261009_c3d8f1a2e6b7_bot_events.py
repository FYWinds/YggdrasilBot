"""bot events

Revision ID: c3d8f1a2e6b7
Revises: b7e2c91f4a3d
Create Date: 2026-10-09 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d8f1a2e6b7"
down_revision: str | Sequence[str] | None = "b7e2c91f4a3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("operator_id", sa.BigInteger(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("handled_by", sa.Text(), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_events")),
    )
    op.create_index("ix_bot_events_at", "bot_events", ["at"])
    op.create_index("ix_bot_events_status", "bot_events", ["status", "at"])


def downgrade() -> None:
    op.drop_index("ix_bot_events_status", table_name="bot_events")
    op.drop_index("ix_bot_events_at", table_name="bot_events")
    op.drop_table("bot_events")

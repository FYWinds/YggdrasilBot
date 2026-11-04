"""invocation session setup

A continued answer must reach the provider with the same instructions, tools and route as the answer did, or its
prompt cache misses: ``invocation_sessions`` now keeps them. Rows kept without them can't be continued that way and
are dropped (they are at most an hour's worth).

Revision ID: b7f2c9d4e815
Revises: a3d8e6b1c470
Create Date: 2026-11-04 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7f2c9d4e815"
down_revision: str | Sequence[str] | None = "a3d8e6b1c470"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM invocation_sessions")
    op.add_column("invocation_sessions", sa.Column("handler", sa.Text(), nullable=False))
    op.add_column("invocation_sessions", sa.Column("instructions", sa.Text(), nullable=False))
    op.add_column("invocation_sessions", sa.Column("tools", postgresql.JSONB(), nullable=False))
    op.add_column("invocation_sessions", sa.Column("sandbox", sa.Boolean(), nullable=False))


def downgrade() -> None:
    for column in ("sandbox", "tools", "instructions", "handler"):
        op.drop_column("invocation_sessions", column)

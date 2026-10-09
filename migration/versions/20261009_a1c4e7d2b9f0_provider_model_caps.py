"""provider model aliases and capabilities

Revision ID: a1c4e7d2b9f0
Revises: 976d790a324b
Create Date: 2026-10-09 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7d2b9f0"
down_revision: str | Sequence[str] | None = "976d790a324b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("model_aliases", postgresql.JSONB(), server_default="{}", nullable=False))
    op.add_column("providers", sa.Column("model_caps", postgresql.JSONB(), server_default="{}", nullable=False))


def downgrade() -> None:
    op.drop_column("providers", "model_caps")
    op.drop_column("providers", "model_aliases")

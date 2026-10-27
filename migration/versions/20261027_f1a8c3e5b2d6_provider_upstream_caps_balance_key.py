"""provider upstream caps and balance key

``upstream_caps``: capabilities a provider's own /models reports (OpenRouter's architecture and supported
parameters), so image models are known without models.dev. ``balance_key_enc``: an optional extra key used only to
read the account balance (OpenRouter's management key).

Revision ID: f1a8c3e5b2d6
Revises: e4b9c2d7a1f3
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1a8c3e5b2d6"
down_revision: str | Sequence[str] | None = "e4b9c2d7a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("upstream_caps", postgresql.JSONB(), server_default="{}", nullable=False))
    op.add_column("providers", sa.Column("balance_key_enc", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("providers", "balance_key_enc")
    op.drop_column("providers", "upstream_caps")

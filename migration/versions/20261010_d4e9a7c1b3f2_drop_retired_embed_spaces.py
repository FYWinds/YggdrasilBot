"""drop retired embed spaces

Re-embedding now deletes the old space instead of marking it retired; retired rows only kept
their providers from being deleted (their vectors were already gone).

Revision ID: d4e9a7c1b3f2
Revises: c3d8f1a2e6b7
Create Date: 2026-10-10 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e9a7c1b3f2"
down_revision: str | Sequence[str] | None = "c3d8f1a2e6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM embed_spaces WHERE status = 'retired'")


def downgrade() -> None:
    pass  # the rows held no vectors; nothing to restore

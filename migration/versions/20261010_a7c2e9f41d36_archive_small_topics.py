"""archive small topics

Finished topics with fewer than 20 messages and fewer than 3 participants are archived:
hidden from the agent's history search, still listed in the dashboard. New topics are
archived when they close; this applies the default thresholds to the existing ones.

Revision ID: a7c2e9f41d36
Revises: fefe8a002515
Create Date: 2026-10-10 20:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7c2e9f41d36"
down_revision: str | Sequence[str] | None = "fefe8a002515"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE topics SET status = 'archived'"
        " WHERE status = 'closed' AND msg_count < 20 AND cardinality(participants) < 3"
    )


def downgrade() -> None:
    op.execute("UPDATE topics SET status = 'closed' WHERE status = 'archived'")

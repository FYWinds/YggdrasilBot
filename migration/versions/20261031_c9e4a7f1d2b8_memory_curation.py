"""memory curation

The hourly curation job scores closed topics and profile facts: ``importance`` (1–5) from the model, ``weight``
from importance plus messages/participants (topics) or supporting messages (facts), ``scored_at`` when it was
rated. Low-value facts are archived instead of deleted (``archived_at``, ``archive_reason``) and restorable
from the console; ``locked`` marks facts an admin restored, which curation never archives again. Existing rows
start unscored and active; the job works through them over a few runs.

Revision ID: c9e4a7f1d2b8
Revises: f1a8c3e5b2d6
Create Date: 2026-10-31 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e4a7f1d2b8"
down_revision: str | Sequence[str] | None = "f1a8c3e5b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("importance", sa.SmallInteger(), nullable=True))
    op.add_column("topics", sa.Column("importance_reason", sa.Text(), nullable=True))
    op.add_column("topics", sa.Column("weight", sa.Float(), nullable=True))
    op.add_column("topics", sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_profile_facts", sa.Column("importance", sa.SmallInteger(), nullable=True))
    op.add_column("user_profile_facts", sa.Column("weight", sa.Float(), nullable=True))
    op.add_column("user_profile_facts", sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_profile_facts", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_profile_facts", sa.Column("archive_reason", sa.Text(), nullable=True))
    op.add_column("user_profile_facts", sa.Column("locked", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    for column in ("locked", "archive_reason", "archived_at", "scored_at", "weight", "importance"):
        op.drop_column("user_profile_facts", column)
    for column in ("scored_at", "weight", "importance_reason", "importance"):
        op.drop_column("topics", column)

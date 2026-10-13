"""profile close-order watermark

Profiles consumed closed topics in id order, but topics close in a different order than
they start: a topic still open when a newer one was consumed was skipped for good. Topics
now record ``closed_at`` and profiles keep a ``(last_closed_at, last_topic_id)`` watermark.

Existing finished topics take ``updated_at`` as their close time (the close is normally their
last write). Existing profiles take the close time of their last consumed topic, so topics
that closed after it and were skipped are picked up by the next run.

Revision ID: b5d18f3c9e27
Revises: a7c2e9f41d36
Create Date: 2026-10-13 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d18f3c9e27"
down_revision: str | Sequence[str] | None = "a7c2e9f41d36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_profiles", sa.Column("last_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE topics SET closed_at = updated_at WHERE status <> 'open'")
    # A deleted watermark topic (all its messages recalled) falls back to the latest close at or below its id.
    op.execute(
        "UPDATE user_profiles p SET last_closed_at = COALESCE("
        " (SELECT t.closed_at FROM topics t WHERE t.id = p.last_topic_id),"
        " (SELECT max(t.closed_at) FROM topics t WHERE t.group_id = p.group_id AND t.id <= p.last_topic_id))"
        " WHERE p.last_topic_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "last_closed_at")
    op.drop_column("topics", "closed_at")

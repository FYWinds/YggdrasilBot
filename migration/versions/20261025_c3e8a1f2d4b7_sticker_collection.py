"""sticker collection

The sticker pool held QQ market faces only, and our groups send none: they send custom image stickers. ``stickers``
now holds both kinds (``kind`` + natural key ``ref``), with the image in ``sticker_blobs``, vision tags and moderation
on the row, and per-group counts in ``sticker_usage``. Existing market faces become ``kind = 'mface'`` rows.

Revision ID: c3e8a1f2d4b7
Revises: b5d18f3c9e27
Create Date: 2026-10-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1f2d4b7"
down_revision: str | Sequence[str] | None = "b5d18f3c9e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("stickers", sa.Column("kind", sa.Text(), nullable=False, server_default="mface"))
    op.add_column("stickers", sa.Column("ref", sa.Text(), nullable=True))
    op.execute("UPDATE stickers SET ref = package_id || ':' || emoji_id")
    op.alter_column("stickers", "ref", nullable=False)
    op.alter_column("stickers", "kind", server_default=None)
    op.alter_column("stickers", "emoji_id", nullable=True)
    op.alter_column("stickers", "package_id", nullable=True)
    op.drop_constraint("uq_stickers_package_id_emoji_id", "stickers", type_="unique")
    op.create_unique_constraint("uq_stickers_kind_ref", "stickers", ["kind", "ref"])
    for name, type_, default in (
        ("status", sa.Text(), "'ready'"),
        ("error", sa.Text(), "''"),
        ("mime", sa.Text(), "''"),
        ("description", sa.Text(), "''"),
        ("usage", sa.Text(), "''"),
        ("ocr_text", sa.Text(), "''"),
        ("emotions", sa.ARRAY(sa.Text()), "'{}'"),
        ("unsafe_category", sa.Text(), "''"),
        ("tag_attempts", sa.BigInteger(), "0"),
    ):
        op.add_column("stickers", sa.Column(name, type_, nullable=False, server_default=sa.text(default)))
    for name in ("size_bytes", "width", "height", "frames"):
        op.add_column("stickers", sa.Column(name, sa.BigInteger(), nullable=True))
    op.add_column("stickers", sa.Column("tagged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "stickers", sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )
    op.execute("UPDATE stickers SET first_seen = last_seen")
    op.create_index("ix_stickers_status", "stickers", ["status"])

    op.create_table(
        "sticker_blobs",
        sa.Column("sticker_id", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name="fk_sticker_blobs_sticker_id_stickers", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("sticker_id", name="pk_sticker_blobs"),
    )
    op.create_table(
        "sticker_usage",
        sa.Column("sticker_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["sticker_id"], ["stickers.id"], name="fk_sticker_usage_sticker_id_stickers", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("sticker_id", "group_id", name="pk_sticker_usage"),
    )


def downgrade() -> None:
    op.drop_table("sticker_usage")
    op.drop_table("sticker_blobs")
    op.execute("DELETE FROM stickers WHERE kind <> 'mface'")
    op.drop_index("ix_stickers_status", table_name="stickers")
    for name in (
        "first_seen",
        "tagged_at",
        "size_bytes",
        "width",
        "height",
        "frames",
        "status",
        "error",
        "mime",
        "description",
        "usage",
        "ocr_text",
        "emotions",
        "unsafe_category",
        "tag_attempts",
    ):
        op.drop_column("stickers", name)
    op.drop_constraint("uq_stickers_kind_ref", "stickers", type_="unique")
    op.create_unique_constraint("uq_stickers_package_id_emoji_id", "stickers", ["package_id", "emoji_id"])
    op.alter_column("stickers", "package_id", nullable=False)
    op.alter_column("stickers", "emoji_id", nullable=False)
    op.drop_column("stickers", "ref")
    op.drop_column("stickers", "kind")

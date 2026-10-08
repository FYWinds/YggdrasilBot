"""Raw chat history. ``messages`` is range-partitioned by month on ``sent_at``.

Partitions are created by the migration baseline and kept ahead by the
``maintain_partitions`` worker job; autogenerate ignores them (see migration/env.py).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Identity, Index, PrimaryKeyConstraint, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        PrimaryKeyConstraint("id", "sent_at"),
        Index("ix_messages_group_sent", "group_id", text("sent_at DESC")),
        Index("ix_messages_group_qq", "group_id", "qq_message_id"),
        Index("ix_messages_group_user_sent", "group_id", "user_id", text("sent_at DESC")),
        {"postgresql_partition_by": "RANGE (sent_at)"},
    )

    id: Mapped[int] = mapped_column(Identity(always=True))
    sent_at: Mapped[datetime]
    group_id: Mapped[int]
    user_id: Mapped[int]
    # NapCat message ids are 32-bit hashes and repeat over time; only use them for recent lookups.
    qq_message_id: Mapped[int]
    message_seq: Mapped[int | None]
    reply_to_qq_id: Mapped[int | None]
    sender_name: Mapped[str] = mapped_column(Text, default="")
    plain_text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list[Any]]
    is_self: Mapped[bool] = mapped_column(default=False)
    recalled: Mapped[bool] = mapped_column(default=False)
    recalled_at: Mapped[datetime | None]


class UserAlias(Base):
    """Nickname / group card history, so old messages can be attributed after renames."""

    __tablename__ = "user_aliases"
    __table_args__ = (PrimaryKeyConstraint("group_id", "user_id", "name"),)

    group_id: Mapped[int]
    user_id: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class Sticker(Base):
    """Market faces seen in groups; the quick-ack model may pick one by summary."""

    __tablename__ = "stickers"
    __table_args__ = (UniqueConstraint("package_id", "emoji_id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    emoji_id: Mapped[str] = mapped_column(Text)
    package_id: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    seen_count: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())

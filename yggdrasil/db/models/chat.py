"""Raw chat history. ``messages`` is range-partitioned by month on ``sent_at``.

Partitions are created by the migration baseline and kept ahead by the
``maintain_partitions`` worker job; autogenerate ignores them (see migration/env.py).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    """A sticker seen in groups, one row per distinct sticker.

    ``kind = 'image'``: a custom sticker (an image segment with ``sub_type`` 1). ``ref`` is the md5 QQ uses as its file
    name; the image is saved on first sight (QQ links expire within hours) and the ``sticker`` task describes, tags and
    moderates it. ``kind = 'mface'``: a QQ market face, ``ref`` = ``"<package_id>:<emoji_id>"``, sent by id.
    """

    __tablename__ = "stickers"
    __table_args__ = (UniqueConstraint("kind", "ref"), Index("ix_stickers_status", "status"))

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # image | mface
    ref: Mapped[str] = mapped_column(Text)
    emoji_id: Mapped[str | None] = mapped_column(Text)  # mface only
    package_id: Mapped[str | None] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")  # QQ's own label: the market face name, "动画表情"
    # fetching -> untagged -> ready, or failed (see ``error``); market faces are ready at once.
    status: Mapped[str] = mapped_column(Text, default="ready")
    error: Mapped[str] = mapped_column(Text, default="")
    mime: Mapped[str] = mapped_column(Text, default="")
    size_bytes: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    frames: Mapped[int | None]  # > 1: animated
    description: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[str] = mapped_column(Text, default="")  # when it fits to send
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    emotions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    unsafe_category: Mapped[str] = mapped_column(Text, default="")  # set when moderation disabled it
    tag_attempts: Mapped[int] = mapped_column(default=0)
    tagged_at: Mapped[datetime | None]
    seen_count: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


class StickerBlob(Base):
    """The original image of a custom sticker (kept as sent, GIFs included); separate so listings stay light."""

    __tablename__ = "sticker_blobs"

    sticker_id: Mapped[int] = mapped_column(ForeignKey("stickers.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class StickerUsage(Base):
    """How often each group sends a sticker."""

    __tablename__ = "sticker_usage"
    __table_args__ = (PrimaryKeyConstraint("sticker_id", "group_id"),)

    sticker_id: Mapped[int] = mapped_column(ForeignKey("stickers.id", ondelete="CASCADE"))
    group_id: Mapped[int]
    count: Mapped[int] = mapped_column(default=1)
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())

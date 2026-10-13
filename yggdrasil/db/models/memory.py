from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Float,
    ForeignKey,
    Identity,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import EMBED_DIM, Base, TimestampMixin


class MessageChunk(Base, TimestampMixin):
    """A conversation window (time-gap / reply aware), the unit of vector search."""

    __tablename__ = "message_chunks"
    __table_args__ = (Index("ix_message_chunks_group_end", "group_id", "end_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    group_id: Mapped[int]
    first_msg_id: Mapped[int]
    last_msg_id: Mapped[int]
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    msg_count: Mapped[int]
    text: Mapped[str] = mapped_column(Text)


class Topic(Base, TimestampMixin):
    __tablename__ = "topics"
    __table_args__ = (Index("ix_topics_group_status", "group_id", "status", "end_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    group_id: Mapped[int]
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    participants: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    first_msg_id: Mapped[int]
    last_msg_id: Mapped[int]
    msg_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(Text, default="open")  # open | closed | archived (hidden from the agent)
    dirty: Mapped[bool] = mapped_column(default=False)  # a member message was recalled; recompute
    # Set when the topic closes and again when an admin restores it from archived: profiles consume
    # closed topics in (closed_at, id) order, since topics close in a different order than they start.
    closed_at: Mapped[datetime | None]


class TopicMessage(Base):
    __tablename__ = "topic_messages"
    __table_args__ = (
        PrimaryKeyConstraint("topic_id", "message_id"),
        Index("ix_topic_messages_message", "message_id"),
    )

    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    message_id: Mapped[int]


class DailyDigest(Base, TimestampMixin):
    __tablename__ = "daily_digests"
    __table_args__ = (UniqueConstraint("group_id", "day"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    group_id: Mapped[int]
    day: Mapped[date]
    summary: Mapped[str] = mapped_column(Text)
    topic_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class GlossaryTerm(Base, TimestampMixin):
    __tablename__ = "glossary_terms"
    __table_args__ = (UniqueConstraint("group_id", "term"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    group_id: Mapped[int]
    term: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    definition: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[Any]] = mapped_column(default=list)  # [{msg_id, text}]
    first_seen: Mapped[datetime | None]
    last_seen: Mapped[datetime | None]
    hits: Mapped[int] = mapped_column(default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    locked: Mapped[bool] = mapped_column(default=False)  # admin-owned; pipelines never overwrite
    source: Mapped[str] = mapped_column(Text, default="auto")  # auto | admin


class GlossaryCandidate(Base, TimestampMixin):
    __tablename__ = "glossary_candidates"
    __table_args__ = (PrimaryKeyConstraint("group_id", "term"),)

    group_id: Mapped[int]
    term: Mapped[str] = mapped_column(Text)
    count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending | accepted | rejected
    example_msg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class UserProfile(Base, TimestampMixin):
    __tablename__ = "user_profiles"
    __table_args__ = (PrimaryKeyConstraint("group_id", "user_id"),)

    group_id: Mapped[int]
    user_id: Mapped[int]
    display_name: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    interests: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    traits: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    opted_out: Mapped[bool] = mapped_column(default=False)
    # Profile watermark: the last consumed closed topic in (closed_at, id) order.
    last_closed_at: Mapped[datetime | None]
    last_topic_id: Mapped[int | None]


class UserProfileFact(Base, TimestampMixin):
    __tablename__ = "user_profile_facts"
    __table_args__ = (Index("ix_user_profile_facts_user", "group_id", "user_id"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    group_id: Mapped[int]
    user_id: Mapped[int]
    fact: Mapped[str] = mapped_column(Text)
    source_msg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class MemoryVector(Base):
    """One embedding per (kind, ref, space). ``kind``: chunk | topic | glossary | digest."""

    __tablename__ = "memory_vectors"
    __table_args__ = (
        PrimaryKeyConstraint("kind", "ref_id", "space_id"),
        Index("ix_memory_vectors_scope", "group_id", "space_id", "kind"),
        Index(
            "ix_memory_vectors_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[int]
    space_id: Mapped[int] = mapped_column(ForeignKey("embed_spaces.id", ondelete="CASCADE"))
    group_id: Mapped[int]
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBED_DIM))

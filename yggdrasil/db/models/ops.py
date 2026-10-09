from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, Float, ForeignKey, Identity, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from yggdrasil.db.base import Base


class Invocation(Base):
    """One triggered bot run, from match to delivery. Linked to its trace."""

    __tablename__ = "invocations"
    __table_args__ = (
        Index("ix_invocations_created", "created_at"),
        Index("ix_invocations_group_created", "group_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    group_id: Mapped[int | None]
    user_id: Mapped[int]
    handler: Mapped[str] = mapped_column(Text)
    trigger_qq_message_id: Mapped[int | None]
    # The bot message that answered (forward, or the reviewed text): who asked for a bot message,
    # e.g. to let the asker recall it. QQ ids repeat, so only look up recent rows.
    reply_qq_message_id: Mapped[int | None]
    request_text: Mapped[str] = mapped_column(Text, default="")
    # ok | done | silent | blocked_input | review | rejected | failed | ratelimited | cancelled
    # (ops runs: done | reply | handoff)
    status: Mapped[str] = mapped_column(Text, default="running")
    models: Mapped[list[Any]] = mapped_column(default=list)
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    tokens_cached: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[dict[str, Any]] = mapped_column(default=dict)  # {tool: count}
    ack_kind: Mapped[str | None] = mapped_column(Text)
    ack_latency_ms: Mapped[int | None]
    duration_ms: Mapped[int | None]
    input_verdict: Mapped[dict[str, Any] | None]
    output_verdict: Mapped[dict[str, Any] | None]
    reply: Mapped[dict[str, Any] | None]
    trace_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class ModerationResult(Base):
    __tablename__ = "moderation_results"
    __table_args__ = (Index("ix_moderation_results_created", "created_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("invocations.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(Text)  # input | ack | output
    verdict: Mapped[str] = mapped_column(Text)  # Safe | Controversial | Unsafe
    score: Mapped[float] = mapped_column(Float)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    action: Mapped[str] = mapped_column(Text)  # pass | review | drop
    model: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(default=0)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_status", "status", "expires_at"),)

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)  # shown as R<id>
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("invocations.id", ondelete="SET NULL"))
    group_id: Mapped[int]
    user_id: Mapped[int]
    trigger_qq_message_id: Mapped[int | None]
    request_text: Mapped[str] = mapped_column(Text, default="")
    reply: Mapped[dict[str, Any]]  # serialized BotReply
    verdict: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending | approved | rejected | expired
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]


class BotEvent(Base):
    """A non-message OneBot event worth keeping: requests (with their decision) and notices about
    the bot itself or members of enabled groups. Shown as E<id>."""

    __tablename__ = "bot_events"
    __table_args__ = (
        Index("ix_bot_events_at", "at"),
        Index("ix_bot_events_status", "status", "at"),
    )

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    # request.group.invite | request.group.add | request.friend | notice.group_decrease.kick_me | ...
    kind: Mapped[str] = mapped_column(Text)
    group_id: Mapped[int | None]
    user_id: Mapped[int | None]  # who it is about (requester, member, the bot)
    operator_id: Mapped[int | None]  # who did it (inviter, admin who kicked or banned)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # raw event (requests keep their flag here)
    # requests only: pending | approved | rejected | expired | failed
    status: Mapped[str | None] = mapped_column(Text)
    handled_by: Mapped[str | None] = mapped_column(Text)  # qq:<id> | email | auto
    handled_at: Mapped[datetime | None]

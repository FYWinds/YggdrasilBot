"""The normalized conversation format, shared by pydantic-ai runs and omp jobs.

A transcript is a list of :class:`Entry` (oldest first). Entries carry content only in ``full`` mode; in ``metadata``
mode (``privacy.capture_llm_content`` off) texts and arguments are replaced by their size and images keep only
their size. ``display`` holds what a tool result *showed* to the model in a form the console can render (QQ
messages, a forward, a page), alongside the text the model actually read.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

VERSION = 1


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str | None = None  # None in metadata mode
    chars: int = 0


class ThinkingPart(BaseModel):
    type: Literal["thinking"] = "thinking"
    text: str | None = None
    chars: int = 0
    redacted: bool = False  # the provider returned it encrypted


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    blob: str | None = None  # sha256 in transcript_blobs; None in metadata mode or for URL images
    url: str | None = None
    mime: str = ""
    bytes: int = 0


class ToolCallPart(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, Any] | None = None  # None in metadata mode
    chars: int = 0


class OutputPart(BaseModel):
    """The run's structured output when the model returned it as text (native or prompted output mode) instead of
    calling ``final_result``. Not stored: the admin API sets it when it reads a transcript."""

    type: Literal["output"] = "output"
    value: dict[str, Any]
    chars: int = 0


Part = Annotated[TextPart | ThinkingPart | ImagePart | ToolCallPart | OutputPart, Field(discriminator="type")]


class Usage(BaseModel):
    input: int = 0
    output: int = 0
    cached: int = 0


class QQMessage(BaseModel):
    """A QQ message as it was when the run used it. ``segments`` are raw OneBot segments; the API adds ``src`` to
    image segment data when it can serve the picture."""

    id: int | None = None  # messages.id; None for forward nodes and messages that were never stored
    qq_message_id: int | None = None
    user_id: int
    sender_name: str = ""
    sent_at: datetime | None = None
    segments: list[dict[str, Any]] = []
    is_self: bool = False
    recalled: bool = False


class QQContextDisplay(BaseModel):
    """The user turn of an invocation: the group chat the prompt included, the quoted message and the trigger."""

    kind: Literal["qq_context"] = "qq_context"
    recent: list[int] = []  # message ids, oldest first
    quoted: QQMessage | None = None
    trigger: QQMessage | None = None
    blocks: list[str] = []  # names of the prompt blocks, in order (最近群聊, 被引用的消息, 请求, ...)
    # What the quoted message itself replies to, oldest first (up to two levels): QQ messages from the history
    chain: list[QQMessage] = []


class QQMessagesDisplay(BaseModel):
    kind: Literal["qq_messages"] = "qq_messages"
    message_ids: list[int] = []


class ForwardNode(BaseModel):
    depth: int = 0
    user_id: int = 0
    sender_name: str = ""
    sent_at: datetime | None = None
    segments: list[dict[str, Any]] = []


class QQForwardDisplay(BaseModel):
    kind: Literal["qq_forward"] = "qq_forward"
    nodes: list[ForwardNode] = []


class PageDisplay(BaseModel):
    kind: Literal["page"] = "page"
    url: str
    final_url: str = ""
    title: str = ""
    via: str = ""
    page: int = 1
    pages: int = 1


class Hit(BaseModel):
    title: str = ""
    url: str = ""
    snippet: str = ""


class HitsDisplay(BaseModel):
    kind: Literal["hits"] = "hits"
    query: str = ""
    hits: list[Hit] = []


class ShownImage(BaseModel):
    blob: str
    caption: str = ""


class ImagesDisplay(BaseModel):
    """Images a tool produced or posted (render_*, sandbox show) rather than returned to the model."""

    kind: Literal["images"] = "images"
    images: list[ShownImage] = []


Display = Annotated[
    QQContextDisplay | QQMessagesDisplay | QQForwardDisplay | PageDisplay | HitsDisplay | ImagesDisplay,
    Field(discriminator="kind"),
]

Outcome = Literal["ok", "error", "retry", "cancelled"]


class Entry(BaseModel):
    """One turn. Fields beyond ``kind``/``at``/``parts`` apply to some kinds only (noted per field)."""

    kind: Literal["system", "user", "assistant", "tool", "event"]
    at: datetime | None = None
    parts: list[Part] = []
    display: Display | None = None  # user (qq_context) and tool entries
    # assistant
    model: str | None = None
    duration_ms: int | None = None  # assistant and tool entries
    usage: Usage | None = None
    finish: str | None = None
    error: str | None = None  # assistant (provider error) and tool entries
    # tool
    call_id: str | None = None
    name: str | None = None
    outcome: Outcome | None = None
    # event
    text: str | None = None
    level: Literal["info", "warn", "error"] | None = None

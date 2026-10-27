"""What a run's tools showed the model, recorded for the console while the run happens.

Tools call into the run's :class:`TraceNotes` (``deps.notes``) with their ``ctx.tool_call_id``. Only references are
kept during the run (message ids, image bytes); messages are snapshotted once when the transcript is saved. With
``full=False`` (``privacy.capture_llm_content`` off) nothing is collected.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from yggdrasil.transcript.schema import Display, ImagesDisplay, QQContextDisplay, QQMessagesDisplay, ShownImage


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    mime: str


def image_key(data: dict[str, Any]) -> str | None:
    """The identity of an OneBot image segment, the same one ``Handles.add`` de-duplicates on."""
    key = data.get("file_id") or data.get("file") or data.get("url")
    return str(key) if key else None


def blob_id(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class TraceNotes:
    full: bool = True
    displays: dict[str, Display] = field(default_factory=dict)  # by tool call id
    message_ids: set[int] = field(default_factory=set)  # every stored message a display or the context names
    image_keys: dict[str, str] = field(default_factory=dict)  # image segment key -> sha256 of the bytes we loaded
    blobs: dict[str, Blob] = field(default_factory=dict)  # images to store, by sha256
    errors: set[str] = field(default_factory=set)  # tool calls whose tool turned an exception into error text
    context: QQContextDisplay | None = None
    bot: tuple[int, str] | None = None  # (QQ number, persona name): how the console labels the bot's own messages

    def show(self, call_id: str | None, display: Display) -> None:
        if self.full and call_id:
            self.displays[call_id] = display

    def messages(self, call_id: str | None, ids: list[int]) -> None:
        """A tool returned these stored messages (recent chat, context around a message, history hits)."""
        if not self.full or not call_id or not ids:
            return
        self.message_ids.update(ids)
        self.displays[call_id] = QQMessagesDisplay(message_ids=ids)

    def image(self, data: bytes, mime: str, *, segment: dict[str, Any] | None = None) -> str | None:
        """Keep an image the run used; ``segment`` is the OneBot image data it came from, so QQ messages showing
        that image can be rendered with these bytes after the QQ link has expired."""
        if not self.full or not data:
            return None
        sha = blob_id(data)
        self.blobs.setdefault(sha, Blob(data, mime))
        key = image_key(segment) if segment else None
        if key:
            self.image_keys[key] = sha
        return sha

    def shown(self, call_id: str | None, images: list[tuple[bytes, str]], mime: str = "image/png") -> None:
        """Images a tool produced or posted for the reply (``render_*``, ``show``): ``(bytes, caption)``. Several
        calls for the same tool call add up."""
        if not self.full or not call_id or not images:
            return
        shown = [ShownImage(blob=sha, caption=caption) for data, caption in images if (sha := self.image(data, mime))]
        current = self.displays.get(call_id)
        if isinstance(current, ImagesDisplay):
            current.images += shown
        else:
            self.displays[call_id] = ImagesDisplay(images=shown)

    def failed(self, call_id: str | None) -> None:
        if call_id:
            self.errors.add(call_id)

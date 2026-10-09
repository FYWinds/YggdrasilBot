from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.settings import Settings

_PREFIX = {"image": "img", "file": "file", "forward": "fwd", "card": "card", "url": "url"}


@dataclass(slots=True)
class Handle:
    key: str
    kind: str  # image | file | forward | card | url | record | video | mface
    data: dict[str, Any]
    source_qq_msg: int | None = None


@dataclass
class Handles:
    """Short handles (img#1, file#1, fwd#1, url#1, ...) the model uses instead of raw QQ ids."""

    items: dict[str, Handle] = field(default_factory=dict)
    _counters: Counter[str] = field(default_factory=Counter)

    def add(self, kind: str, data: dict[str, Any], source_qq_msg: int | None = None) -> str | None:
        prefix = _PREFIX.get(kind)
        if prefix is None:
            return None
        # De-duplicate identical attachments (same file id / url).
        ident = data.get("file_id") or data.get("file") or data.get("id") or data.get("url")
        for h in self.items.values():
            if (
                h.kind == kind
                and ident
                and (h.data.get("file_id") or h.data.get("file") or h.data.get("id") or h.data.get("url")) == ident
            ):
                return h.key
        self._counters[prefix] += 1
        key = f"{prefix}#{self._counters[prefix]}"
        self.items[key] = Handle(key, kind, data, source_qq_msg)
        return key

    def get(self, key: str) -> Handle | None:
        return self.items.get(key.strip())


@dataclass
class Deps:
    api: NapCatAPI
    group_id: int | None
    user_id: int
    settings: Settings
    handles: Handles
    tool_counts: Counter[str] = field(default_factory=Counter)
    vision_tokens: list[tuple[str, int, int]] = field(default_factory=list)  # (model, in, out) of sub-calls
    images: list[tuple[bytes, str]] = field(default_factory=list)  # generated (png, caption), e.g. diagrams
    on_slow_tool: Callable[[], Awaitable[None]] | None = None  # called before each tools.slow_tools call

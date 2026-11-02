"""Types and limits shared by the ``read_url`` pipeline and its site adapters, and the capped body reader."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from yggdrasil.tools.errors import ToolError

# Official JSON APIs (Bilibili in particular) answer 412 to browser user agents without a full
# cookie fingerprint, but serve honest non-browser clients normally.
API_USER_AGENT = "yggdrasil-bot/0.1"
MB = 1024 * 1024
MAX_HTML_BYTES = 5 * MB


@dataclass(slots=True)
class PageResult:
    url: str
    final_url: str
    title: str
    text: str
    page: int
    pages: int
    truncated: bool
    """More content exists beyond this page (later pages, or the source hit a size cap)."""
    via: str
    """``http`` / ``browser`` / ``document:<kind>`` / adapter name (``bilibili``, ``github``, ``hackernews``, ...)."""
    images: list[str] = field(default_factory=list)
    """Image URLs from the source (adapters only); the caller shows them with page 1."""


@dataclass(slots=True)
class Extracted:
    """Whole-document result of one fetch, before pagination."""

    final_url: str
    title: str
    text: str
    via: str
    capped: bool = False
    images: list[str] = field(default_factory=list)
    """Image URLs that belong to the content (third-party: download them through the SSRF guard)."""
    html: str = field(default="", repr=False)
    """Source HTML (generic and browser tiers), for :func:`read_url` to find the URL's ``#fragment``; never cached."""


class Unreadable(ToolError):
    """The page could not be read from here (wall, block page, HTTP error, network failure, nothing rendered);
    unlike other tool errors (not a web page, file too large, gone per an official API), a remote fetcher may
    still get it."""


type RemoteFetch = Callable[[str], Awaitable[Extracted | None]]
"""Fetch a URL from elsewhere (``yggdrasil.tools.remote_fetch``); ``None`` when it has nothing either."""


type AdapterResult = Extracted | str | None
"""An adapter returns the content, a rewritten URL to restart with, or ``None`` to use the generic path."""
type AdapterFn = Callable[[httpx.AsyncClient, httpx.URL], Awaitable[AdapterResult]]


async def read_capped(chunks: AsyncIterator[bytes], limit: int) -> tuple[bytes, bool]:
    """The bytes of ``chunks`` up to ``limit``, and whether there were more (the rest is not read)."""
    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > limit:
            return bytes(buf[:limit]), True
    return bytes(buf), False

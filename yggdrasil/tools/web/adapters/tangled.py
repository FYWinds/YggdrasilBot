"""Tangled (git hosting on AT Protocol): code pages as raw files."""

import re

import httpx

from yggdrasil.tools.web.adapters.common import raw_file
from yggdrasil.tools.web.adapters.registry import site_adapter
from yggdrasil.tools.web.model import API_USER_AGENT, AdapterResult

_TANGLED_BLOB_RE = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$")


@site_adapter("tangled.org", "tangled.sh")
async def blob(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """A code page on Tangled (git hosting on AT Protocol): the raw file, so lines and indentation survive."""
    match = _TANGLED_BLOB_RE.match(url.path)
    if match is None:
        return None
    owner, repo, ref, path = match.groups()
    raw = f"https://tangled.org/{owner}/{repo}/raw/{ref}/{path}"
    # Tangled turns away browser user agents without a browser TLS fingerprint, but serves honest clients.
    return await raw_file(client, raw, f"{owner}/{repo}: {path}", "tangled", {"User-Agent": API_USER_AGENT})

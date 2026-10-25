"""Remote page fetch for ``read_url``: the DeepSeek Fetch API, tried when the local tiers cannot read a page.

DeepSeek fetches from its own crawler (often serving a cached copy) and returns Markdown; it reaches CN platforms
our datacenter IP cannot. The key comes from the dashboard's search providers (kind ``deepseek``); every call
counts toward that provider's monthly limit. Docs: https://api-docs.deepseek.com/guides/search/
"""

import re
from collections.abc import Sequence

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.search import reserve_quota
from yggdrasil.tools.web import Extracted, RemoteFetch

_TIMEOUT = 30.0
# DeepSeek backslash-escapes ASCII punctuation all over its Markdown (`\:` `\-` `\.`): noise for the model.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")


def remote_fetcher(providers: Sequence[SearchProviderInfo]) -> RemoteFetch | None:
    """The fetcher for the highest-priority enabled DeepSeek provider with a key, or ``None`` when there is none."""
    provider = next((p for p in providers if p.kind == "deepseek" and p.enabled and p.api_key), None)
    if provider is None:
        return None

    async def fetch(url: str) -> Extracted | None:
        if not await reserve_quota(provider):
            return None
        return await deepseek_fetch(url, provider)

    return fetch


async def deepseek_fetch(
    url: str, provider: SearchProviderInfo, *, client: httpx.AsyncClient | None = None
) -> Extracted | None:
    """Fetch ``url`` through DeepSeek; ``None`` when it has no usable content (or the call failed)."""
    request = httpx.Request(
        "POST",
        provider.base_url + "/v1/tools/fetch",
        json={"url": url},
        headers={"Authorization": f"Bearer {provider.api_key or ''}"},
    )
    request.extensions["timeout"] = httpx.Timeout(_TIMEOUT).as_dict()
    try:
        if client is None:
            async with httpx.AsyncClient() as own:
                resp = await own.send(request)
        else:
            resp = await client.send(request)
    except httpx.HTTPError as exc:
        LOGGER.warning("deepseek fetch failed for {}: {!r}", httpx.URL(url).host, exc)
        return None
    if resp.status_code != 200:
        # 404: no usable content, 403: refused by policy; anything else (quota, auth, outage) is worth a log line.
        if resp.status_code not in (403, 404):
            LOGGER.warning("deepseek fetch HTTP {} for {}: {}", resp.status_code, httpx.URL(url).host, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    content = _MD_ESCAPE_RE.sub(r"\1", str(data.get("content") or "")).strip()
    if not content:
        return None
    return Extracted(url, str(data.get("title") or ""), content, "remote:deepseek")

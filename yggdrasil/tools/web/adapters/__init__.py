"""Site adapters: official APIs and raw sources for sites whose pages read badly. :func:`adapter_for` finds the one
for a URL."""

import re

import httpx

from yggdrasil.tools import x
from yggdrasil.tools.web.adapters import (
    bilibili,
    github,
    hackernews,
    huggingface,
    lobsters,
    msn,
    statuspage,
    tangled,
    telegram,
    ubuntu,
    x_post,
    youtube,
)
from yggdrasil.tools.web.model import AdapterFn

# Exact host names, lower-case.
_BY_HOST: dict[str, AdapterFn] = {
    **dict.fromkeys(("b23.tv", "www.b23.tv", "bili2233.cn"), bilibili.short_link),
    **dict.fromkeys(("www.bilibili.com", "bilibili.com", "m.bilibili.com"), bilibili.video),
    **dict.fromkeys(x.HOSTS, x_post.post),
    **dict.fromkeys(("github.com", "www.github.com"), github.page),
    **dict.fromkeys(("tangled.org", "tangled.sh"), tangled.blob),
    "news.ycombinator.com": hackernews.item,
    **dict.fromkeys(("lobste.rs", "www.lobste.rs"), lobsters.story),
    **dict.fromkeys(("t.me", "telegram.me", "www.t.me"), telegram.post),
    **dict.fromkeys(("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"), youtube.video),
    **dict.fromkeys(("ubuntu.com", "www.ubuntu.com"), ubuntu.cve),
    **dict.fromkeys(("huggingface.co", "www.huggingface.co", "hf.co"), huggingface.repo),
    **dict.fromkeys(("www.msn.com", "msn.com"), msn.article),
}
# URL paths of hosted platforms, which serve their customers' own domains; tried on hosts without a host adapter.
_BY_PATH: list[tuple[re.Pattern[str], AdapterFn]] = [(statuspage.INCIDENT_PATH_RE, statuspage.incident)]


def adapter_for(url: httpx.URL) -> AdapterFn | None:
    """The adapter that reads ``url``, or ``None`` for the generic path."""
    if (adapter := _BY_HOST.get(url.host.lower())) is not None:
        return adapter
    return next((fn for pattern, fn in _BY_PATH if pattern.match(url.path)), None)


__all__ = ["adapter_for"]

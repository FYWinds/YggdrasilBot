"""Which site adapter reads a URL: registered for exact hosts, or for URL paths on any host."""

import re
from collections.abc import Callable

import httpx

from yggdrasil.tools.web.model import AdapterFn

_ADAPTERS: dict[str, AdapterFn] = {}
_PATH_ADAPTERS: list[tuple[re.Pattern[str], AdapterFn]] = []


def site_adapter(*hosts: str) -> Callable[[AdapterFn], AdapterFn]:
    """Register an adapter for exact host names (lower-case)."""

    def register(fn: AdapterFn) -> AdapterFn:
        for host in hosts:
            _ADAPTERS[host] = fn
        return fn

    return register


def path_adapter(pattern: re.Pattern[str]) -> Callable[[AdapterFn], AdapterFn]:
    """Register an adapter for URL paths matching ``pattern`` on hosts without a host adapter (hosted platforms
    such as Statuspage serve their customers' own domains)."""

    def register(fn: AdapterFn) -> AdapterFn:
        _PATH_ADAPTERS.append((pattern, fn))
        return fn

    return register


def adapter_for(url: httpx.URL) -> AdapterFn | None:
    if (adapter := _ADAPTERS.get(url.host.lower())) is not None:
        return adapter
    return next((fn for pattern, fn in _PATH_ADAPTERS if pattern.match(url.path)), None)

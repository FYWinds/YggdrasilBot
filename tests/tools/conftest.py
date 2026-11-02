"""Fixtures for ``read_url`` tests: the only file that names the internals of its fetch tiers.

``web_page`` answers every request a read makes from a :class:`Routes`: the site adapters' and the browser service's
httpx requests, and the plain-HTTP tier's impersonated GET. ``local_tier`` replaces all the local tiers with one
outcome. Neither looks up DNS, and every test starts with an empty page cache."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.ssrf import GuardedResponse
from yggdrasil.tools.web import Extracted


@pytest.fixture(autouse=True)
def _empty_page_cache() -> None:
    web._cache.clear()


def _skip_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """``check_url`` only drops the fragment: test hosts are never looked up."""

    async def check(url: str) -> str:
        return url.partition("#")[0]

    monkeypatch.setattr(web, "check_url", check)


@pytest.fixture
def web_page(monkeypatch: pytest.MonkeyPatch) -> Routes:
    """The web as ``read_url`` sees it: add answers (by URL or host) to the returned routes. Unrouted requests get
    a 404, and ``seen`` holds every request, the plain-HTTP tier's GETs included."""
    _skip_dns(monkeypatch)
    routes = Routes()
    routes.install(monkeypatch)

    @asynccontextmanager
    async def impersonated_stream(
        url: str, *, headers: dict[str, str] | None = None, **_: object
    ) -> AsyncGenerator[GuardedResponse]:
        resp = await routes.handle(httpx.Request("GET", url, headers=headers))
        lowered = {k.lower(): v for k, v in resp.headers.items()}
        yield GuardedResponse(url, resp.status_code, lowered, resp.charset_encoding, resp.aiter_bytes())

    monkeypatch.setattr(web, "impersonated_stream", impersonated_stream)
    return routes


@pytest.fixture
def local_tier(monkeypatch: pytest.MonkeyPatch) -> Callable[[Extracted | Exception], list[str]]:
    """``local_tier(outcome)`` makes the local tiers (adapters, plain HTTP, browser) return or raise ``outcome``,
    and returns the list of URLs they are asked to read."""
    _skip_dns(monkeypatch)

    def install(outcome: Extracted | Exception) -> list[str]:
        reads: list[str] = []

        async def fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
            reads.append(url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(web, "_fetch", fetch)
        return reads

    return install

"""HTTP API and render engines.

``POST /render`` ``{"url", "engine", "timeout"}`` -> ``{"final_url", "html"}``; ``400 {"error"}`` when the URL
itself is not allowed (SSRF guard), ``502 {"error"}`` when the engine failed. ``GET /healthz``.

Engines:

* ``patchright``: one warm persistent Chrome profile (cookies such as anti-bot clearances survive between reads),
  a new tab per request.
* ``nodriver``: a fresh Chrome per request driven over raw CDP, without the Playwright driver; ``read_url``
  escalates to it when patchright gets a block page or a JS challenge.

Every request either engine makes is SSRF-checked per origin (verdicts cached briefly); audio/video is skipped.
Chrome's own UA, client hints and timezone are left alone so the fingerprint stays coherent.
"""

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncGenerator
from typing import Any, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import check_url

LOGGER = logging.getLogger("yggdrasil.browser")

# No GPU in the pod: without SwiftShader Chrome has no WebGL at all, a strong bot signal. Linux Chrome ignores
# --lang; the UI language and Accept-Language come from $LANGUAGE (set in the image).
CHROME_ARGS = ["--disable-dev-shm-usage", "--window-size=1920,1080", "--enable-unsafe-swiftshader"]
# Audio/video only: blocked images and fonts are themselves detectable (broken-image dimensions, font probing).
_SKIPPED_RESOURCES = frozenset({"media"})
_PASSTHROUGH_SCHEMES = ("data:", "blob:", "about:")
_MAX_TIMEOUT = 60.0


class Engine(Protocol):
    async def render(self, url: str, timeout_s: float) -> tuple[str, str]:
        """Load ``url`` and return ``(final_url, html)``."""
        ...

    async def close(self) -> None: ...


class Verdicts:
    """SSRF verdicts per origin, cached for ``ttl`` seconds (DNS may change, so not forever)."""

    def __init__(self, ttl: float = 60) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, bool]] = {}

    async def allowed(self, url: str) -> bool:
        if url.startswith(_PASSTHROUGH_SCHEMES):
            return True
        if not url.startswith(("http://", "https://")):
            return False  # file:, chrome:, ftp: ...
        origin = str(httpx.URL(url).copy_with(path="/", query=None, fragment=None))
        hit = self._cache.get(origin)
        if hit is not None and time.monotonic() - hit[0] < self._ttl:
            return hit[1]
        try:
            await check_url(origin)
            verdict = True
        except ToolError:
            verdict = False
        self._cache[origin] = (time.monotonic(), verdict)
        return verdict


class PatchrightEngine:
    def __init__(self, verdicts: Verdicts, profile_dir: str, max_pages: int = 4) -> None:
        self._verdicts = verdicts
        self._profile_dir = profile_dir
        self._pages = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._context: Any = None

    async def _ensure_context(self) -> Any:
        async with self._lock:
            if self._context is None:
                from patchright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                # Service workers would fetch outside route interception, i.e. outside the SSRF guard.
                context = await self._playwright.chromium.launch_persistent_context(
                    self._profile_dir,
                    channel="chrome",
                    headless=False,
                    no_viewport=True,
                    args=CHROME_ARGS,
                    service_workers="block",
                )
                await context.route("**/*", self._guard)
                context.on("close", lambda _: self._forget(context))
                self._context = context
            return self._context

    def _forget(self, context: Any) -> None:
        if self._context is context:
            self._context = None  # Chrome died or was closed: relaunch on the next request

    async def _guard(self, route: Any) -> None:
        # A raising route handler is an unhandled task error; the page may already be gone.
        with contextlib.suppress(Exception):
            request = route.request
            if request.resource_type in _SKIPPED_RESOURCES or not await self._verdicts.allowed(request.url):
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

    async def render(self, url: str, timeout_s: float) -> tuple[str, str]:
        from patchright.async_api import TimeoutError as PlaywrightTimeout

        deadline_ms = timeout_s * 1000
        async with self._pages:
            context = await self._ensure_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=deadline_ms)
                with contextlib.suppress(PlaywrightTimeout):  # long-polling pages never go idle
                    await page.wait_for_load_state("networkidle", timeout=min(deadline_ms / 2, 8000))
                return page.url, await page.content()
            finally:
                await page.close()

    async def close(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()


class NodriverEngine:
    def __init__(self, verdicts: Verdicts, chrome_path: str, max_browsers: int = 1) -> None:
        self._verdicts = verdicts
        self._chrome_path = chrome_path
        self._browsers = asyncio.Semaphore(max_browsers)

    async def render(self, url: str, timeout_s: float) -> tuple[str, str]:
        import nodriver
        from nodriver import cdp

        async with self._browsers:
            profile = tempfile.mkdtemp(prefix="nodriver-")
            browser = await nodriver.start(
                headless=False,
                browser_executable_path=self._chrome_path,
                user_data_dir=profile,
                sandbox=False,
                browser_args=CHROME_ARGS,
                lang="zh-CN",
            )
            try:
                tab = browser.main_tab
                skipped = {cdp.network.ResourceType(t.capitalize()) for t in _SKIPPED_RESOURCES}

                async def on_paused(event: cdp.fetch.RequestPaused) -> None:
                    with contextlib.suppress(Exception):
                        if event.resource_type in skipped or not await self._verdicts.allowed(event.request.url):
                            reason = cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                            await tab.send(cdp.fetch.fail_request(event.request_id, reason))
                        else:
                            await tab.send(cdp.fetch.continue_request(event.request_id))

                tab.add_handler(cdp.fetch.RequestPaused, on_paused)
                await tab.send(cdp.fetch.enable(patterns=[cdp.fetch.RequestPattern(url_pattern="*")]))
                async with asyncio.timeout(timeout_s):
                    # Not tab.get(): it re-attaches after navigating, and the new session drops Fetch interception
                    # (paused requests could then never be continued).
                    await tab.send(cdp.page.navigate(url))
                    await _wait_ready_state(tab, ("interactive", "complete"))
                # No networkidle over raw CDP: give challenge scripts and late XHRs a moment. Pages that stream
                # (video, long polling) never reach "complete", so this is capped.
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(3):
                        await _wait_ready_state(tab, ("complete",))
                await asyncio.sleep(1)
                return str(await tab.evaluate("location.href")), str(await tab.get_content())
            finally:
                browser.stop()
                shutil.rmtree(profile, ignore_errors=True)

    async def close(self) -> None:
        return None


async def _wait_ready_state(tab: Any, states: tuple[str, ...]) -> None:
    # Page.navigate returns once navigation starts; poll instead of wiring lifecycle events. Callers bound the wait.
    while True:
        if await tab.evaluate("document.readyState") in states:
            return
        await asyncio.sleep(0.25)


def create_app(engines: dict[str, Engine]) -> Starlette:
    async def render(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            url, engine = str(body["url"]), str(body.get("engine", "patchright"))
            timeout_s = min(max(float(body.get("timeout", 20)), 1.0), _MAX_TIMEOUT)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=422)
        if engine not in engines:
            return JSONResponse({"error": f"unknown engine {engine}"}, status_code=422)
        try:
            target = await check_url(url)
        except ToolError as exc:
            return JSONResponse({"error": exc.message}, status_code=400)
        try:
            final_url, html = await engines[engine].render(target, timeout_s)
        except Exception as exc:
            LOGGER.warning("%s render failed for %s: %r", engine, httpx.URL(target).host, exc)
            return JSONResponse({"error": repr(exc)[:300]}, status_code=502)
        return JSONResponse({"final_url": final_url, "html": html})

    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        yield
        for engine in engines.values():
            with contextlib.suppress(Exception):
                await engine.close()

    return Starlette(
        routes=[Route("/render", render, methods=["POST"]), Route("/healthz", healthz)],
        lifespan=lifespan,
    )

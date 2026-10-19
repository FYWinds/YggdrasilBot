"""One persistent Playwright Chromium per kernel: `await browser()`, `await shot()`, `await snapshot()`.

Playwright is imported on first use only (the kernel preloads `ygg`). Chromium is Playwright's bundled full
build (new headless mode via channel "chromium"); gVisor and a non-root user rule out its sandbox.
"""

# asyncio is imported inside the functions: this module loads with `ygg`, which every CLI call imports too.
from typing import Any

# Chromium's crashpad handlers daemonize out of the kernel's process tree, so the end-of-call reap kills them;
# the browser keeps working without them (--disable-crashpad-for-testing would avoid them but breaks navigation).
CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
WIDTH, HEIGHT = 1280, 800
LOCALE = "zh-CN"
TIMEZONE = "Asia/Shanghai"


class Text(str):
    """A string that displays as itself (not as an escaped repr) when it is a cell's last expression."""

    def __repr__(self) -> str:
        return str(self)


class _State:
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    loop: Any = None


_state = _State()


async def browser() -> Any:
    """The persistent page (Playwright async `Page`); reopened if it was closed, relaunched if Chromium died."""
    import asyncio

    s = _state
    if s.browser is not None and s.browser.is_connected() and s.loop is asyncio.get_running_loop():
        if s.page is None or s.page.is_closed():
            s.page = await s.context.new_page()
        return s.page
    await close_browser()
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        chromium = await playwright.chromium.launch(channel="chromium", args=CHROMIUM_ARGS)
        context = await chromium.new_context(
            viewport={"width": WIDTH, "height": HEIGHT}, locale=LOCALE, timezone_id=TIMEZONE
        )
        page = await context.new_page()
    except BaseException:
        await playwright.stop()
        raise
    s.playwright, s.browser, s.context, s.page = playwright, chromium, context, page
    s.loop = asyncio.get_running_loop()
    return page


async def shot(page: Any = None, full_page: bool = False) -> None:
    """Screenshot the page (default: the persistent one) and display it, so it comes back as an image."""
    from IPython import get_ipython
    from IPython.display import Image, display

    if get_ipython() is None:
        raise RuntimeError("shot() 只能在 py 工具里用；脚本里请用 page.screenshot(path=...) 存成文件")
    page = page or await browser()
    data = await page.screenshot(full_page=full_page, type="png")
    display(Image(data=data, format="png"))


async def snapshot(page: Any = None) -> Text:
    """The page's accessibility tree as text (reads structure without looking at a screenshot)."""
    page = page or await browser()
    return Text(await page.locator("body").aria_snapshot())


async def close_browser() -> None:
    s = _state
    playwright, chromium = s.playwright, s.browser
    s.playwright = s.browser = s.context = s.page = s.loop = None
    if chromium is not None:
        try:
            await chromium.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


def close_from_thread(timeout: float) -> None:
    """Close the browser from another thread (the idle watchdog) on the loop that owns it."""
    loop = _state.loop
    if loop is None or loop.is_closed():
        return
    try:
        import asyncio

        asyncio.run_coroutine_threadsafe(close_browser(), loop).result(timeout)
    except Exception:
        pass

"""Render Mermaid / Markdown / HTML to PNG with the shared remote Playwright browser.

Pages are built from model-written content, so the page may only load the pinned CDN
scripts and inline data; every other request is aborted.
"""

from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route, async_playwright

from yggdrasil.tools.errors import ToolError

CDN_HOST = "cdn.jsdelivr.net"
MERMAID_URL = f"https://{CDN_HOST}/npm/mermaid@11/dist/mermaid.min.js"
MARKED_URL = f"https://{CDN_HOST}/npm/marked@15/marked.min.js"
GITHUB_CSS_URL = f"https://{CDN_HOST}/npm/github-markdown-css@5/github-markdown-light.min.css"
MAX_SOURCE_CHARS = 20000
_FONTS = "'Noto Sans CJK SC','WenQuanYi Zen Hei','Microsoft YaHei',sans-serif"

_MERMAID_PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 16px; background: #ffffff; display: inline-block; }}
  #out svg {{ max-width: 1600px; height: auto; }}
</style>
<script src="{MERMAID_URL}"></script>
</head><body><div id="out"></div></body></html>
"""

_MERMAID_JS = f"""async (code) => {{
  mermaid.initialize({{ startOnLoad: false, securityLevel: "strict", theme: "default", fontFamily: "{_FONTS}" }});
  try {{
    await mermaid.parse(code);
    const {{ svg }} = await mermaid.render("ygg-diagram", code);
    document.getElementById("out").innerHTML = svg;
    return null;
  }} catch (e) {{
    return String(e && e.message ? e.message : e);
  }}
}}"""

_DOC_PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{GITHUB_CSS_URL}">
<script src="{MARKED_URL}"></script>
<style>
  body {{ margin: 0; background: #ffffff; }}
  #out {{ box-sizing: border-box; width: 820px; padding: 28px 32px; font-family: {_FONTS}; font-size: 15px; }}
  #out table {{ display: table; width: 100%; }}
</style>
</head><body><article id="out" class="markdown-body"></article></body></html>
"""

_MARKDOWN_JS = """(md) => {
  document.getElementById("out").innerHTML = marked.parse(md, { gfm: true, breaks: true });
}"""

_HTML_JS = """(html) => {
  const out = document.getElementById("out");
  out.innerHTML = html;
  // No script execution from model-written HTML.
  out.querySelectorAll("script").forEach((s) => s.remove());
}"""


async def _allow_cdn_only(route: Route) -> None:
    url = route.request.url
    if url.startswith("data:") or urlsplit(url).hostname == CDN_HOST:
        await route.continue_()
    else:
        await route.abort()


async def _render(page_html: str, js: str, arg: str, selector: str, *, browser_ws_url: str, timeout_s: float) -> bytes:
    if not arg.strip():
        raise ToolError("要渲染的内容是空的")
    if len(arg) > MAX_SOURCE_CHARS:
        raise ToolError(f"内容太长（超过 {MAX_SOURCE_CHARS} 字符）")
    ms = timeout_s * 1000
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect(browser_ws_url, timeout=ms)
            try:
                page = await browser.new_page(device_scale_factor=2, viewport={"width": 900, "height": 600})
                await page.route("**/*", _allow_cdn_only)
                await page.set_content(page_html, wait_until="load", timeout=ms)
                error = await page.evaluate(js, arg)
                if error:
                    raise ToolError(f"语法错误：{str(error)[:300]}")
                await page.wait_for_load_state("networkidle", timeout=ms)
                return await page.locator(selector).screenshot(type="png", timeout=ms)
            finally:
                await browser.close()
    except PlaywrightError as e:
        raise ToolError(f"渲染失败：{str(e).splitlines()[0][:200]}") from e


async def render_mermaid(code: str, *, browser_ws_url: str, timeout_s: float = 20) -> bytes:
    """Syntax errors raise ToolError with Mermaid's message so the model can fix and retry."""
    return await _render(
        _MERMAID_PAGE, _MERMAID_JS, code.strip(), "#out svg", browser_ws_url=browser_ws_url, timeout_s=timeout_s
    )


async def render_markdown(markdown: str, *, browser_ws_url: str, timeout_s: float = 20) -> bytes:
    """GitHub-styled Markdown (tables, code blocks, lists) as an image."""
    return await _render(_DOC_PAGE, _MARKDOWN_JS, markdown, "#out", browser_ws_url=browser_ws_url, timeout_s=timeout_s)


async def render_html(fragment: str, *, browser_ws_url: str, timeout_s: float = 20) -> bytes:
    """An HTML fragment (inline styles allowed, scripts stripped, external resources blocked) as an image."""
    return await _render(_DOC_PAGE, _HTML_JS, fragment, "#out", browser_ws_url=browser_ws_url, timeout_s=timeout_s)

"""Render Mermaid diagrams to PNG with the shared remote Playwright browser."""

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from yggdrasil.tools.errors import ToolError

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
MAX_CODE_CHARS = 8000

_PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 16px; background: #ffffff; display: inline-block; }}
  #out svg {{ max-width: 1600px; height: auto; }}
</style>
<script src="{MERMAID_URL}"></script>
</head><body><div id="out"></div></body></html>
"""

_RENDER_JS = """async (code) => {
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default",
                       fontFamily: "'Noto Sans CJK SC','WenQuanYi Zen Hei',sans-serif" });
  try {
    await mermaid.parse(code);
    const { svg } = await mermaid.render("ygg-diagram", code);
    document.getElementById("out").innerHTML = svg;
    return null;
  } catch (e) {
    return String(e && e.message ? e.message : e);
  }
}"""


async def render_mermaid(code: str, *, browser_ws_url: str, render_timeout: float = 20) -> bytes:
    """PNG bytes of the diagram. Syntax errors raise ToolError with Mermaid's message so the model can fix them."""
    code = code.strip()
    if not code:
        raise ToolError("图表代码是空的")
    if len(code) > MAX_CODE_CHARS:
        raise ToolError(f"图表代码太长（超过 {MAX_CODE_CHARS} 字符）")
    ms = render_timeout * 1000
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect(browser_ws_url, timeout=ms)
            try:
                page = await browser.new_page(device_scale_factor=2, viewport={"width": 1280, "height": 800})
                await page.set_content(_PAGE, wait_until="load", timeout=ms)
                error = await page.evaluate(_RENDER_JS, code)
                if error:
                    raise ToolError(f"Mermaid 语法错误：{error[:300]}")
                svg = page.locator("#out svg")
                return await svg.screenshot(type="png", timeout=ms)
            finally:
                await browser.close()
    except PlaywrightError as e:
        raise ToolError(f"渲染失败：{str(e).splitlines()[0][:200]}") from e

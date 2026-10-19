"""Client for the render service (Mermaid / Markdown / HTML → PNG pages).

The service owns the browser, bundled assets and request sandboxing.
"""

import base64
from typing import Literal

import httpx

from yggdrasil.tools.errors import ToolError

Kind = Literal["markdown", "mermaid", "html"]
MAX_SOURCE_CHARS = 20000
TIMEOUT_S = 30  # service renders within 15 s plus queueing
_UNAVAILABLE = "渲染服务暂时不可用，稍后再试或者直接用文字回答"


async def render(
    kind: Kind,
    source: str,
    *,
    base_url: str,
    max_images: int,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[bytes], list[str]]:
    """Render ``source`` into 1..max_images PNGs (reading order) plus the service's warnings.

    Content problems raise ToolError with the service's message; warnings are non-fatal (e.g. an inline Mermaid
    block shown as code, an external image dropped) and should reach the model so it can fix and retry.
    """
    if not source.strip():
        raise ToolError("要渲染的内容是空的")
    if len(source) > MAX_SOURCE_CHARS:
        raise ToolError(f"内容太长（超过 {MAX_SOURCE_CHARS} 字符）")
    body = {"kind": kind, "source": source, "max_images": max_images}
    url = base_url.rstrip("/") + "/render"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as own:
                resp = await own.post(url, json=body)
        else:
            resp = await client.post(url, json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        raise ToolError(_UNAVAILABLE) from e
    if resp.status_code in (422, 503):
        try:
            message = str(resp.json()["error"])
        except (ValueError, KeyError, TypeError):
            message = _UNAVAILABLE if resp.status_code == 503 else "渲染失败"
        raise ToolError(message)
    if resp.status_code != 200:
        raise ToolError(_UNAVAILABLE)
    try:
        data = resp.json()
        pngs = [base64.b64decode(img["png"]) for img in data["images"]]
        warnings = [str(w) for w in data.get("warnings") or []]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ToolError(_UNAVAILABLE) from e
    return pngs, warnings

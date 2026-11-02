"""MSN: articles from the content API that MSN's own script pages load."""

import re

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import cn_datetime, fragment_text
from yggdrasil.tools.web.model import AdapterResult, Extracted

_MSN_CONTENT_RE = re.compile(r"/(?:ar|vi|gm|ss)-([A-Za-z0-9]{6,})/?$")
_MSN_LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$", re.IGNORECASE)


async def article(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    """An MSN article (or video / slideshow summary) from the content API its own pages load: the page is a script
    shell that shows a cookie-consent wall first."""
    match = _MSN_CONTENT_RE.search(url.path)
    if match is None:
        return None
    first = url.path.split("/")[1]
    locale = first.lower() if _MSN_LOCALE_RE.match(first) else "en-us"
    api = f"https://assets.msn.com/content/view/v2/Detail/{locale}/{match.group(1)}"
    resp = await client.get(api, headers={"Accept": "application/json"})
    if resp.status_code in (404, 410):
        raise ToolError("MSN 上的这篇内容不存在或已下架")
    try:
        data = resp.json() if resp.status_code == 200 else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("title") or not (text := format_msn(data)):
        return None  # the generic path gets the consent wall and says so
    return Extracted(str(url.copy_with(query=None)), str(data["title"]), text, "msn")


def format_msn(data: dict) -> str:
    """Source, time and the article text (the abstract when there is no body); empty when there is neither."""
    body = fragment_text(str(data.get("body") or ""))
    abstract = str(data.get("abstract") or "").strip()
    if not body and not abstract:
        return ""
    provider = raw if isinstance(raw := data.get("provider"), dict) else {}
    authors = [str(a["name"]) for a in data.get("authors") or [] if isinstance(a, dict) and a.get("name")]
    source = [f"来源：{provider['name']}"] if provider.get("name") else []
    if authors:
        source.append("作者：" + "、".join(authors))
    lines = [" · ".join(source)] if source else []
    if published := data.get("publishedDateTime"):
        lines.append(f"发布时间：{cn_datetime(published)}")
    if original := data.get("sourceHref"):
        lines.append(f"原文：{original}")
    lines += ["", body] if body else ["", f"摘要：{abstract}"]
    return "\n".join(lines)

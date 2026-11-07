"""Lobsters: a story and its whole comment thread from the site's own ``.json`` view.

The HTML page comes back empty to plain HTTP clients, and the browser fallback's extraction keeps only the story
text, so a discussion thread arrived without a single comment.
"""

import re

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import cn_datetime
from yggdrasil.tools.web.model import API_USER_AGENT, AdapterResult, Extracted

_STORY_PATH = re.compile(r"/s/([a-z0-9]+)(?:/[^/]*)?/?")


async def story(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    m = _STORY_PATH.fullmatch(url.path)
    if m is None:
        return None
    resp = await client.get(f"https://lobste.rs/s/{m[1]}.json", headers={"User-Agent": API_USER_AGENT})
    if resp.status_code == 404:
        raise ToolError("Lobsters 帖子不存在")
    if resp.status_code != 200:
        return None
    data = resp.json()
    return Extracted(
        final_url=data.get("comments_url") or f"https://lobste.rs/s/{m[1]}",
        title=data.get("title") or "Lobsters 帖子",
        text=format_lobsters(data),
        via="lobsters",
    )


def format_lobsters(data: dict) -> str:
    """The story header, its text and the comment thread, indented by depth."""
    facts = [f"作者 {data.get('submitter_user') or '未知'}"]
    if data.get("score") is not None:
        facts.append(f"{data['score']} 分")
    facts += [f"{data.get('comment_count') or 0} 条评论", cn_datetime(data.get("created_at"))]
    lines = [" · ".join(facts)]
    if tags := data.get("tags"):
        lines.append(f"标签：{', '.join(tags)}")
    if link := data.get("url"):
        lines.append(f"链接：{link}")
    if body := _plain(data.get("description_plain")):
        lines += ["", body]
    comments = data.get("comments") or []
    if comments:
        lines += ["", "评论："]
    for i, c in enumerate(comments):
        depth = int(c.get("depth") or 0)
        removed = c.get("is_deleted") or c.get("is_moderated")
        has_replies = i + 1 < len(comments) and int(comments[i + 1].get("depth") or 0) > depth
        if removed and not has_replies:
            continue  # removed leaf
        if depth == 0:
            lines.append("")  # blank lines only between threads: pagination strips paragraph indents
        body = "[已删除]" if removed else _plain(c.get("comment_plain"))
        pad = "  " * depth
        head, *rest = [line for line in body.splitlines() if line.strip()] or [""]
        lines.append(f"{pad}- {'[已删除]' if removed else c.get('commenting_user') or '未知'}：{head}")
        lines.extend(f"{pad}  {line}" for line in rest)
    return "\n".join(lines)


def _plain(text: object) -> str:
    """The author's Markdown source as Lobsters stores it (CRLF line ends)."""
    return re.sub(r"\n{3,}", "\n\n", str(text or "").replace("\r\n", "\n")).strip()

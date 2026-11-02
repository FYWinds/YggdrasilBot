"""Hacker News: an item and its whole comment tree from the Algolia API."""

from datetime import UTC, datetime

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import CN_TZ, fragment_text
from yggdrasil.tools.web.model import API_USER_AGENT, AdapterResult, Extracted


def _cn_time(epoch: int) -> str:
    return f"{datetime.fromtimestamp(epoch, UTC).astimezone(CN_TZ):%Y-%m-%d %H:%M}"


async def item(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    item_id = url.params.get("id", "")
    if url.path != "/item" or not item_id.isdigit():
        return None
    resp = await client.get(f"https://hn.algolia.com/api/v1/items/{item_id}", headers={"User-Agent": API_USER_AGENT})
    if resp.status_code == 404:
        raise ToolError("Hacker News 条目不存在")
    if resp.status_code != 200:
        return None
    item = resp.json()
    return Extracted(
        final_url=f"https://news.ycombinator.com/item?id={item_id}",
        title=item.get("title") or f"Hacker News 评论（{item.get('author') or '未知'}）",
        text=format_hacker_news(item),
        via="hackernews",
    )


def format_hacker_news(item: dict) -> str:
    """The item header, its text and the whole comment tree, indented by depth."""
    comments = _hn_count(item) - 1
    facts = [f"作者 {item.get('author') or '未知'}"]
    if item.get("points") is not None:
        facts.append(f"{item['points']} 分")
    facts.append(f"{comments} 条评论")
    if created := item.get("created_at_i"):
        facts.append(_cn_time(int(created)))
    lines = [" · ".join(facts)]
    if link := item.get("url"):
        lines.append(f"链接：{link}")
    if body := item.get("text"):
        lines += ["", fragment_text(body)]
    if comments:
        lines += ["", "评论："]
        for child in item.get("children") or []:
            thread: list[str] = []
            _hn_comment_lines(child, 0, thread)
            if thread:
                lines += ["", *thread]  # blank lines only between threads: pagination strips paragraph indents
    return "\n".join(lines)


def _hn_count(node: dict) -> int:
    return 1 + sum(_hn_count(child) for child in node.get("children") or [])


def _hn_comment_lines(node: dict, depth: int, out: list[str]) -> None:
    children = node.get("children") or []
    text = node.get("text")
    if text is None and not children:
        return  # deleted leaf
    body = fragment_text(text) if text is not None else "[已删除]"
    pad = "  " * depth
    head, *rest = [line for line in body.splitlines() if line.strip()] or [""]
    out.append(f"{pad}- {node.get('author') or '[已删除]'}：{head}")
    out.extend(f"{pad}  {line}" for line in rest)
    for child in children:
        _hn_comment_lines(child, depth + 1, out)

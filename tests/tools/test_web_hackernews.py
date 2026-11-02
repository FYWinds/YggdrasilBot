"""The Hacker News adapter of ``read_url``: an item and its whole comment tree from the Algolia API."""

import httpx

from tests.support.routes import Routes
from yggdrasil.tools.web import Extracted, paginate
from yggdrasil.tools.web.adapters import hackernews

HN_ITEM = {
    "id": 1,
    "title": "Show HN: a thing",
    "author": "op",
    "points": 42,
    "created_at_i": 1792774748,
    "url": "https://example.com/thing",
    "text": None,
    "children": [
        {
            "author": "alice",
            "text": "First paragraph.<p>Second paragraph.",
            "children": [{"author": "bob", "text": "Reply &amp; more", "children": []}],
        },
        {"author": None, "text": None, "children": []},
        {"author": None, "text": None, "children": [{"author": "carol", "text": "orphan reply", "children": []}]},
    ],
}


def test_hacker_news_thread_keeps_tree_shape_through_pagination() -> None:
    text = hackernews.format_hacker_news(HN_ITEM)
    assert text.startswith("作者 op · 42 分 · 5 条评论 · 2026-10-24 00:59\n链接：https://example.com/thing")
    thread = "- alice：First paragraph.\n  Second paragraph.\n  - bob：Reply & more"
    orphan = "- [已删除]：[已删除]\n  - carol：orphan reply"
    # Deleted leaves vanish; deleted parents stay so their replies keep context. Indents survive pagination.
    assert paginate(text, 10_000) == [text]
    assert f"\n\n{thread}\n\n{orphan}" in paginate(text, 10_000)[0]


async def test_hacker_news_item_uses_algolia_and_other_pages_fall_back() -> None:
    routes = Routes({"hn.algolia.com": httpx.Response(200, json=HN_ITEM)})
    async with routes.client() as client:
        result = await hackernews.item(client, httpx.URL("https://news.ycombinator.com/item?id=1"))
        assert await hackernews.item(client, httpx.URL("https://news.ycombinator.com/newest")) is None
    assert [str(r.url) for r in routes.seen] == ["https://hn.algolia.com/api/v1/items/1"]
    assert isinstance(result, Extracted)
    assert (result.title, result.final_url) == ("Show HN: a thing", "https://news.ycombinator.com/item?id=1")
    assert result.via == "hackernews"

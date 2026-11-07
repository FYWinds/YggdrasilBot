"""The Lobsters adapter of ``read_url``: a story and its whole comment thread from the ``.json`` view."""

import httpx

from tests.support.routes import Routes
from yggdrasil.tools.web import Extracted, paginate
from yggdrasil.tools.web.adapters import lobsters


def comment(user: str, text: str, depth: int, *, removed: bool = False) -> dict:
    return {"commenting_user": user, "comment_plain": text, "depth": depth, "is_deleted": removed}


STORY = {
    "title": "What are you doing this weekend?",
    "url": "",
    "score": 24,
    "comment_count": 6,
    "created_at": "2026-11-06T03:15:23.253-06:00",
    "submitter_user": "op",
    "tags": ["ask", "programming"],
    "description_plain": "Tell us your plans.\r\n\r\nDoing nothing is fine too.",
    "comments_url": "https://lobste.rs/s/abc123/what_are_you_doing_this_weekend",
    "comments": [
        comment("alice", "* Moving to NetBSD.\r\n* Exercising.", 0),
        comment("bob", "> Moving\r\n\r\nGood luck!", 1),
        comment("gone", "", 0, removed=True),
        comment("gone", "", 0, removed=True),
        comment("carol", "replying to a removed comment", 1),
    ],
}


def test_lobsters_thread_keeps_tree_shape_through_pagination() -> None:
    text = lobsters.format_lobsters(STORY)
    assert text.startswith(
        "作者 op · 24 分 · 6 条评论 · 2026-11-06 17:15（北京时间）\n标签：ask, programming\n\n"
        "Tell us your plans.\n\nDoing nothing is fine too.\n\n评论："
    )
    thread = "- alice：* Moving to NetBSD.\n  * Exercising.\n  - bob：> Moving\n    Good luck!"
    orphan = "- [已删除]：[已删除]\n  - carol：replying to a removed comment"
    # Removed leaves vanish; removed parents stay so their replies keep context. Indents survive pagination.
    assert paginate(text, 10_000) == [text]
    assert text.endswith(f"\n\n{thread}\n\n{orphan}")


async def test_lobsters_story_uses_the_json_view_and_other_pages_fall_back() -> None:
    routes = Routes({"lobste.rs": httpx.Response(200, json=STORY)})
    async with routes.client() as client:
        result = await lobsters.story(client, httpx.URL("https://lobste.rs/s/abc123/what_are_you_doing_this_weekend"))
        assert await lobsters.story(client, httpx.URL("https://lobste.rs/t/programming")) is None
    assert [str(r.url) for r in routes.seen] == ["https://lobste.rs/s/abc123.json"]
    assert isinstance(result, Extracted)
    assert (result.title, result.final_url, result.via) == (
        "What are you doing this weekend?",
        "https://lobste.rs/s/abc123/what_are_you_doing_this_weekend",
        "lobsters",
    )

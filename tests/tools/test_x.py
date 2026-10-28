from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from yggdrasil.agent.deps import Handle
from yggdrasil.agent.media import media_bytes
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools import web, x
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.search import web_search

type Handler = Callable[[httpx.Request], httpx.Response]

PID = "100000000000000003"
API = SearchProviderInfo(9, "x", "x", "https://api.x.com", "tok", True, 1, 300, {})

FX_THREAD: dict[str, Any] = {
    "code": 200,
    "status": {
        "type": "status",
        "id": PID,
        "text": "Here is the whole long post, see https://example.com/report",
        "created_timestamp": 1793201980,
        "likes": 12345,
        "reposts": 3,
        "quotes": 1,
        "replies": 2,
        "views": 1533,
        "author": {"name": "Alice", "screen_name": "alice"},
        "replying_to": {"screen_name": "bob", "status": "100000000000000002"},
        "community_note": {"text": "The chart is from 2019.", "facets": []},
        "poll": {
            "choices": [{"label": "Yes", "count": 7, "percentage": 70}, {"label": "No", "count": 3, "percentage": 30}],
            "total_votes": 10,
            "ends_at": "2026-10-29T00:00:00Z",
            "time_left_en": "1 day left",
        },
        "media": {
            "all": [
                {"type": "photo", "url": "https://pbs.twimg.com/media/AAA.jpg?name=orig", "altText": "a chart"},
                {
                    "type": "video",
                    "url": "https://video.twimg.com/v.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/v.jpg",
                    "duration": 83.2,
                },
            ]
        },
        "quote": {
            "type": "status",
            "id": "100000000000000001",
            "text": "the quoted claim",
            "author": {"name": "Carol", "screen_name": "carol"},
            "media": {"photos": [{"type": "photo", "url": "https://pbs.twimg.com/media/BBB.png"}]},
            "quote": {"type": "status", "id": "100000000000000000", "text": "nested quote", "author": {}},
        },
    },
    "thread": [
        {
            "type": "status",
            "id": "100000000000000002",
            "text": "@alice what do you\nknow?",
            "author": {"name": "Bob", "screen_name": "bob"},
        },
        {"type": "status", "id": PID, "text": "focal again", "author": {"name": "Alice", "screen_name": "alice"}},
    ],
}

API_POST: dict[str, Any] = {
    "data": {
        "id": PID,
        "text": "short version https://t.co/aaa https://t.co/pic",
        "note_tweet": {
            "text": "full long text with https://t.co/aaa",
            "entities": {"urls": [{"url": "https://t.co/aaa", "expanded_url": "https://example.com/full"}]},
        },
        "author_id": "11",
        "in_reply_to_user_id": "12",
        "created_at": "2026-10-28T15:39:40.000Z",
        "public_metrics": {"like_count": 5, "retweet_count": 4, "quote_count": 3, "reply_count": 2},
        "attachments": {"media_keys": ["3_1", "7_2"]},
        "referenced_tweets": [{"type": "quoted", "id": "100000000000000001"}],
    },
    "includes": {
        "users": [
            {"id": "11", "name": "Alice", "username": "alice"},
            {"id": "12", "name": "Bob", "username": "bob"},
            {"id": "13", "name": "Carol", "username": "carol"},
        ],
        "media": [
            {"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/CCC.jpg"},
            {
                "media_key": "7_2",
                "type": "video",
                "preview_image_url": "https://pbs.twimg.com/v.jpg",
                "duration_ms": 61000,
            },
        ],
        "tweets": [{"id": "100000000000000001", "text": "quoted", "author_id": "13"}],
    },
}

NEWS: dict[str, Any] = {
    "data": [
        {
            "id": "200",
            "name": "Chip export rules change",
            "summary": "New rules were announced on Tuesday.",
            "category": "News",
            "updated_at": "2026-10-28T02:58:22.000Z",
            "contexts": {"topics": ["Semiconductors"]},
            "cluster_posts_results": [{"post_id": "301"}, {"post_id": "302"}, {"post_id": "303"}, {"post_id": "304"}],
        },
        {"id": "201", "summary": "no name: skipped"},
    ],
    "meta": {"result_count": 2},
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    x._cache.clear()


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Quota:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls = 0

    async def __call__(self, _: SearchProviderInfo) -> bool:
        self.calls += 1
        return self.allow


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"https://x.com/alice/status/{PID}?s=46&t=abc", PID),
        (f"https://mobile.twitter.com/alice/status/{PID}/photo/1", PID),
        (f"https://x.com/i/web/status/{PID}", PID),
        (f"fxtwitter.com/alice/statuses/{PID}", PID),
        (f"  {PID} ", PID),
        ("https://x.com/alice", None),
        (f"https://example.com/alice/status/{PID}", None),
        ("https://x.com/alice/status/notanid", None),
    ],
)
def test_post_id(ref: str, expected: str | None) -> None:
    assert x.post_id(ref) == expected


def test_fx_thread_formats_post_quote_poll_note_and_conversation() -> None:
    post = x.parse_fx_thread(FX_THREAD)
    assert post is not None
    text = x.format_post(post)

    assert "作者：Alice（@alice）" in text
    assert "发布时间：2026-10-28 23:39" in text
    assert "数据：赞 1.2万 · 转发 3 · 引用 1 · 回复 2 · 浏览 1533" in text
    assert "回复给：@bob" in text
    assert "see https://example.com/report" in text
    assert "投票（共 10 票）：\n- Yes：7\n- No：3" in text
    assert "社区笔记：The chart is from 2019." in text
    # Images are numbered across the post and its quote, in Post.images() order.
    assert "- 图 1：图片（a chart）" in text
    assert "- 图 2：视频封面，时长 1:23" in text
    assert "> 作者：Carol（@carol）" in text
    assert "> - 图 3：图片" in text
    assert [m.url for m in post.images()] == [
        "https://pbs.twimg.com/media/AAA.jpg?name=large",
        "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/v.jpg",
        "https://pbs.twimg.com/media/BBB.png?name=large",
    ]
    assert "nested quote" not in text  # one level of quotes only
    # The conversation lists the other posts only, on one line each.
    assert "同一对话里的其他帖子（共 1 条，按时间）：\n- Bob（@bob）：@alice what do you know?" in text
    assert "focal again" not in text


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"code": 404, "status": None, "thread": None}, "帖子不存在或已删除"),
        ({"code": 200, "status": {"type": "tombstone", "reason": "private"}}, "作者设为仅关注者可见"),
    ],
)
def test_fx_unavailable_post_is_gone(payload: dict[str, Any], reason: str) -> None:
    with pytest.raises(x.PostGone, match=reason):
        x.parse_fx_thread(payload)


def test_fx_deleted_quote_is_named() -> None:
    payload = {**FX_THREAD, "status": {**FX_THREAD["status"], "quote": {"type": "tombstone", "reason": "deleted"}}}
    post = x.parse_fx_thread(payload)
    assert post is not None
    assert "引用的帖子：帖子已删除" in x.format_post(post)


async def test_read_post_falls_back_to_api_when_fxtwitter_fails() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.fxtwitter.com":
            return httpx.Response(503, text="<html>Just a moment...</html>")
        return httpx.Response(200, json=API_POST)

    quota = Quota()
    async with _client(handler) as client:
        post = await x.read_post(PID, API, client=client, reserve=quota)

    api_request = seen[1]
    assert seen[0].headers["user-agent"].startswith("yggdrasil-bot/")
    assert (api_request.url.host, api_request.url.path) == ("api.x.com", f"/2/tweets/{PID}")
    assert api_request.headers["authorization"] == "Bearer tok"
    assert "note_tweet" in api_request.url.params["tweet.fields"]
    assert quota.calls == 1
    assert post.via == "x-api"


async def test_read_post_trusts_fxtwitter_gone_answer() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(404, json={"code": 404, "status": None, "thread": None})

    quota = Quota()
    async with _client(handler) as client:
        with pytest.raises(x.PostGone):
            await x.read_post(PID, API, client=client, reserve=quota)
    assert seen == ["api.fxtwitter.com"]
    assert quota.calls == 0


@pytest.mark.parametrize(("api", "allow", "message"), [(None, True, "没有配置 X API"), (API, False, "额度已用完")])
async def test_read_post_without_api_budget_reports_why(
    api: SearchProviderInfo | None, allow: bool, message: str
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(500)

    async with _client(handler) as client:
        with pytest.raises(ToolError, match=message):
            await x.read_post(PID, api, client=client, reserve=Quota(allow))
    assert seen == ["api.fxtwitter.com"]


async def test_read_post_is_cached() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=FX_THREAD)

    async with _client(handler) as client:
        first = await x.read_post(PID, None, client=client)
        second = await x.read_post(PID, None, client=client)
    assert calls == 1
    assert second is first


def test_api_post_uses_long_text_expands_links_and_resolves_includes() -> None:
    post = x.parse_api_post(API_POST)
    assert post.text == "full long text with https://example.com/full"
    assert (post.author, post.handle, post.replying_to) == ("Alice", "alice", "bob")
    assert (post.likes, post.reposts, post.quotes, post.replies, post.views) == (5, 4, 3, 2, None)
    assert [(m.kind, m.url, m.duration_s) for m in post.media] == [
        ("photo", "https://pbs.twimg.com/media/CCC.jpg?name=large", None),
        ("video", "https://pbs.twimg.com/v.jpg", 61.0),
    ]
    assert post.quote is not None
    assert (post.quote.handle, post.quote.text) == ("carol", "quoted")


def test_api_short_post_drops_media_links() -> None:
    data = {
        "id": PID,
        "text": "look https://t.co/pic",
        "entities": {
            "urls": [{"url": "https://t.co/pic", "expanded_url": "https://x.com/a/photo/1", "media_key": "3_1"}]
        },
    }
    assert x.parse_api_post({"data": data}).text == "look"


def test_api_missing_post_is_gone_with_api_detail() -> None:
    payload = {"errors": [{"title": "Not Found Error", "detail": "Could not find tweet with id: [1]."}]}
    with pytest.raises(x.PostGone, match="Could not find tweet"):
        x.parse_api_post(payload)


async def test_search_news_request_parse_and_cache() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=NEWS)

    quota = Quota()
    async with _client(handler) as client:
        stories = await x.search_news(API, "chips", hours=9999, n=5, client=client, reserve=quota)
        again = await x.search_news(API, "chips", hours=9999, n=5, client=client, reserve=quota)

    params = seen[0].url.params
    assert (seen[0].url.path, params["query"], params["max_results"]) == ("/2/news/search", "chips", "5")
    assert params["max_age_hours"] == str(x.NEWS_MAX_HOURS)
    assert "cluster_posts_results" in params["news.fields"]
    assert (len(seen), quota.calls, again) == (1, 1, stories)
    assert [s.name for s in stories] == ["Chip export rules change"]
    text = x.format_news(stories)
    assert "1. Chip export rules change（更新于 10-28 10:58）" in text
    assert "话题：Semiconductors" in text
    # Only the first three cluster posts are linked.
    assert (
        "相关帖子（共 4 条）：https://x.com/i/status/301 https://x.com/i/status/302 https://x.com/i/status/303" in text
    )
    assert "304" not in text


@pytest.mark.parametrize(("hours", "windowed"), [(6, True), (x.POSTS_MAX_HOURS, False)])
async def test_search_posts_excludes_reposts_and_limits_window(hours: int, windowed: bool) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = {
            "data": [{"id": PID, "text": "hello", "author_id": "11", "public_metrics": {"like_count": 1}}],
            "includes": {"users": [{"id": "11", "name": "Alice", "username": "alice"}]},
        }
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        posts = await x.search_posts(API, "from:alice", hours=hours, client=client)

    params = seen[0].url.params
    assert params["query"] == "from:alice -is:retweet"
    assert ("start_time" in params) is windowed
    assert params["max_results"] == "10"
    assert "1. Alice（@alice）" in x.format_posts(posts)
    assert f"https://x.com/alice/status/{PID}" in x.format_posts(posts)


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (429, {}, "请求太频繁"),
        (401, {"title": "Unauthorized", "detail": "Unauthorized"}, "HTTP 401：Unauthorized"),
        (402, {}, "余额不足"),
        (503, {}, "HTTP 503"),
    ],
)
async def test_api_errors_are_tool_errors(status: int, body: dict[str, Any], message: str) -> None:
    async with _client(lambda _: httpx.Response(status, json=body)) as client:
        with pytest.raises(ToolError, match=message):
            await x.api_get(API, "/2/news/search", {"query": "a"}, client=client)


async def test_web_search_skips_x_provider() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(200, json={"results": [{"url": "https://example.com/", "title": "t", "content": "c"}]})

    searxng = SearchProviderInfo(2, "s", "searxng", "http://searxng:8080", None, True, 2, None, {})
    async with _client(handler) as client:
        hits = await web_search("q", [API, searxng], client=client, reserve=Quota())
    assert seen == ["searxng"]
    assert [h.url for h in hits] == ["https://example.com/"]


async def test_read_url_adapter_returns_post_with_images() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/2/thread/{PID}"
        return httpx.Response(200, json=FX_THREAD)

    async with _client(handler) as client:
        result = await web._x_post(client, httpx.URL(f"https://twitter.com/alice/status/{PID}?s=20"))

    assert isinstance(result, web.Extracted)
    assert (result.final_url, result.title, result.via) == (
        f"https://x.com/alice/status/{PID}",
        "Alice（@alice） 在 X 上的帖子",
        "fxtwitter",
    )
    assert len(result.images) == 3
    assert "the quoted claim" in result.text


async def test_third_party_image_handle_goes_through_ssrf_guard() -> None:
    handle = Handle("img#1", "image", {"url": "http://127.0.0.1:9/a.png", "source": "x"})
    with pytest.raises(ToolError, match="内网"):
        await media_bytes(cast(Any, None), None, handle, 1024)

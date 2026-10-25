from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import GuardedResponse
from yggdrasil.tools.web import Extracted, detect_block, detect_wall, paginate, read_url

type Handler = Callable[[httpx.Request], httpx.Response]

ARTICLE = "<html><head><title>测试文章</title></head><body><article>{}</article></body></html>".format(
    "".join(
        f"<p>这是第 {i} 段正文，内容足够长以便抽取器认为它是正文的一部分，而不是导航或者页脚。</p>" for i in range(20)
    )
)


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Route the generic path's impersonated fetch to ``handler``."""

    def install(handler: Handler) -> None:
        @asynccontextmanager
        async def fake(url: str, **_: object) -> AsyncGenerator[GuardedResponse]:
            resp = handler(httpx.Request("GET", url))

            async def chunks() -> AsyncIterator[bytes]:
                yield resp.content

            lowered = {k.lower(): v for k, v in resp.headers.items()}
            yield GuardedResponse(url, resp.status_code, lowered, resp.charset_encoding, chunks())

        monkeypatch.setattr(web, "impersonated_stream", fake)

    return install


# --------------------------------------------------------------------------- pagination


def test_short_text_is_one_page() -> None:
    assert paginate("  hello\n\nworld  ", 100) == ["hello\n\nworld"]


def test_empty_text_is_one_empty_page() -> None:
    assert paginate("\n\n  \n", 100) == [""]


def test_paragraphs_pack_up_to_exact_limit() -> None:
    text = "a" * 50 + "\n\n" + "b" * 50
    assert paginate(text, 102) == [text]
    assert paginate(text, 101) == ["a" * 50, "b" * 50]


def test_oversized_paragraph_splits_on_lines_then_hard() -> None:
    para = "x" * 30 + "\n" + "y" * 250
    pages = paginate(para, 100)
    assert pages == ["x" * 30, "y" * 100, "y" * 100, "y" * 50]


def test_pages_never_exceed_limit_and_keep_content() -> None:
    text = "\n\n".join(f"段落{i}" * (i % 7 + 1) for i in range(200))
    pages = paginate(text, 64)
    assert all(0 < len(p) <= 64 for p in pages)
    assert "".join("".join(pages).split()) == "".join(text.split())


# --------------------------------------------------------------------------- walls


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("", "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证"),
        ("安全验证 - 知乎", ""),
        ("Just a moment...", "Checking if the site connection is secure"),
        ("", "Please complete the CAPTCHA to continue"),
        ("登录", "请登录后查看完整内容"),
    ],
)
def test_detects_walls(title: str, text: str) -> None:
    assert detect_wall(title, text) is not None


def test_long_article_mentioning_keywords_is_not_a_wall() -> None:
    text = "本文介绍短信验证码的实现原理。" + "正文内容。" * 500
    assert detect_wall("短信验证的实现", text) is None


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("Reddit - Dive into anything", "You've been blocked by network security.\nTo continue, log in."),
        ("Pardon Our Interruption", ""),
        ("", "Something went wrong. Try reloading."),
    ],
)
def test_detects_block_pages(title: str, text: str) -> None:
    assert detect_block(title, text) is not None


def test_long_article_mentioning_access_denied_is_not_a_block() -> None:
    text = "How we debugged an access denied error in S3. " + "Body text. " * 300
    assert detect_block("S3 permissions", text) is None


# --------------------------------------------------------------------------- read_url


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def check(url: str) -> str:
        return url

    async def fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
        calls.append(url)
        return Extracted(url, "标题", "\n\n".join(["甲" * 40, "乙" * 40, "丙" * 40]), "http")

    monkeypatch.setattr(web, "check_url", check)
    monkeypatch.setattr(web, "_fetch", fetch)
    web._cache.clear()
    return calls


async def test_read_url_selects_page_and_caches(fake_fetch: list[str]) -> None:
    first = await read_url("https://example.com/a", max_chars=50)
    last = await read_url("https://example.com/a", max_chars=50, page=3)
    assert (first.page, first.pages, first.truncated, first.text) == (1, 3, True, "甲" * 40)
    assert (last.page, last.truncated, last.text) == (3, False, "丙" * 40)
    assert fake_fetch == ["https://example.com/a"]


async def test_read_url_page_out_of_range(fake_fetch: list[str]) -> None:
    with pytest.raises(ToolError, match="共 3 页"):
        await read_url("https://example.com/a", max_chars=50, page=4)


# --------------------------------------------------------------------------- generic fetch


async def test_html_article_is_extracted(serve: Callable[[Handler], None]) -> None:
    serve(lambda r: httpx.Response(200, html=ARTICLE))
    result = await web._fetch_generic("https://example.com/post", None, 5)
    assert result.via == "http"
    assert result.title == "测试文章"
    assert "这是第 3 段正文" in result.text


async def test_gbk_page_without_header_charset_uses_meta(serve: Callable[[Handler], None]) -> None:
    html = ARTICLE.replace("<head>", '<head><meta charset="gbk">').encode("gbk")
    serve(lambda r: httpx.Response(200, content=html, headers={"content-type": "text/html"}))
    result = await web._fetch_generic("https://example.com/post", None, 5)
    assert "这是第 3 段正文" in result.text


async def test_verification_page_raises(serve: Callable[[Handler], None]) -> None:
    wall = (
        "<html><head><title>微信公众平台</title></head><body>"
        "<p>环境异常</p><p>当前环境异常，完成验证后即可继续访问。</p></body></html>"
    )
    serve(lambda r: httpx.Response(200, html=wall))
    with pytest.raises(ToolError, match="验证"):
        await web._fetch_generic("https://mp.weixin.qq.com/s/abc", None, 5)


BLOCK_PAGE = (
    "<html><head><title>Reddit - Dive into anything</title></head><body>"
    "<p>You've been blocked by network security.</p><p>To continue, log in to your Reddit account.</p></body></html>"
)


async def test_block_page_without_browser_raises(serve: Callable[[Handler], None]) -> None:
    serve(lambda r: httpx.Response(200, html=BLOCK_PAGE))
    with pytest.raises(ToolError, match="拒绝"):
        await web._fetch_generic("https://www.reddit.com/r/x/comments/1/y/", None, 5)


async def test_block_page_escalates_to_browser_without_keeping_it(
    serve: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Extracted | None] = []

    async def render(url: str, ws_url: str, timeout_s: float, fallback: Extracted | None) -> Extracted:
        seen.append(fallback)
        return Extracted(url, "thread", "rendered", "browser")

    monkeypatch.setattr(web, "_render_or_raise", render)
    serve(lambda r: httpx.Response(200, html=BLOCK_PAGE))
    result = await web._fetch_generic("https://www.reddit.com/r/x/comments/1/y/", "ws://browser", 5)
    assert result.via == "browser"
    assert seen == [None]  # the block page must never come back as the plain-HTTP fallback


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


@pytest.fixture
def engines(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str | Exception]], list[str]]:
    """Answer ``render_page`` per engine with HTML (or raise); returns the engines asked, in order."""

    def install(answers: dict[str, str | Exception]) -> list[str]:
        asked: list[str] = []

        async def render_page(url: str, browser_url: str, engine: str, **_: object) -> tuple[str, str]:
            asked.append(engine)
            answer = answers[engine]
            if isinstance(answer, Exception):
                raise answer
            return url, answer

        monkeypatch.setattr(web, "render_page", render_page)
        return asked

    return install


async def test_browser_tier_escalates_block_page_to_nodriver(engines: Callable[..., list[str]]) -> None:
    asked = engines({"patchright": BLOCK_PAGE, "nodriver": ARTICLE})
    result = await web._render_or_raise("https://www.reddit.com/r/x/", "http://browser", 5, None)
    assert asked == ["patchright", "nodriver"]
    assert (result.via, result.title) == ("browser:nodriver", "测试文章")


async def test_browser_tier_stops_at_captcha_wall(engines: Callable[..., list[str]]) -> None:
    asked = engines({"patchright": _page("安全验证", "<p>请完成下方验证码</p>"), "nodriver": ARTICLE})
    with pytest.raises(ToolError, match="验证"):
        await web._render_or_raise("https://example.com/a", "http://browser", 5, None)
    assert asked == ["patchright"]  # no captcha fighting: the second engine is not tried


async def test_browser_tier_reports_challenge_both_engines_saw(engines: Callable[..., list[str]]) -> None:
    challenge = _page("Just a moment...", "<p>Checking if the site connection is secure</p>")
    asked = engines({"patchright": challenge, "nodriver": challenge})
    with pytest.raises(ToolError, match="just a moment"):
        await web._render_or_raise("https://example.com/a", "http://browser", 5, None)
    assert asked == ["patchright", "nodriver"]


async def test_browser_tier_failures_keep_plain_http_fallback(engines: Callable[..., list[str]]) -> None:
    engines({"patchright": RuntimeError("chrome crashed"), "nodriver": RuntimeError("no display")})
    fallback = Extracted("https://example.com/a", "t", "short plain text", "http")
    assert await web._render_or_raise("https://example.com/a", "http://browser", 5, fallback) is fallback
    with pytest.raises(ToolError, match="渲染失败"):
        await web._render_or_raise("https://example.com/a", "http://browser", 5, None)


@pytest.mark.parametrize(
    ("html", "renders"),
    [
        (_page("Just a moment...", "<p>Checking if the site connection is secure</p>"), True),
        (_page("安全验证", "<p>请完成下方验证码</p>"), False),
    ],
)
async def test_plain_http_sends_js_challenges_but_not_captchas_to_the_browser(
    serve: Callable[[Handler], None], monkeypatch: pytest.MonkeyPatch, html: str, renders: bool
) -> None:
    async def render(url: str, browser_url: str, timeout_s: float, fallback: Extracted | None) -> Extracted:
        return Extracted(url, "t", "rendered", "browser")

    monkeypatch.setattr(web, "_render_or_raise", render)
    serve(lambda r: httpx.Response(200, html=html))
    if renders:
        assert (await web._fetch_generic("https://example.com/a", "http://browser", 5)).via == "browser"
    else:
        with pytest.raises(ToolError, match="验证"):
            await web._fetch_generic("https://example.com/a", "http://browser", 5)


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> Callable[[Extracted | Exception], None]:
    """Make the local tiers (``_fetch``) return or raise the given outcome."""

    def install(outcome: Extracted | Exception) -> None:
        async def fetch(url: str, browser_url: str | None, timeout_s: float) -> Extracted:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(web, "_fetch", fetch)

    return install


def _remote(answer: Extracted | None, calls: list[str]) -> web.RemoteFetch:
    async def fetch(url: str) -> Extracted | None:
        calls.append(url)
        return answer

    return fetch


REMOTE_PAGE = Extracted("https://example.com/a", "远程", "远程正文。" * 100, "remote:deepseek")


async def test_remote_reads_what_the_local_tiers_could_not(local: Callable[..., None]) -> None:
    local(web.Unreadable("网站拒绝了自动访问"))
    calls: list[str] = []
    assert await web._read("https://example.com/a", None, _remote(REMOTE_PAGE, calls), 5) is REMOTE_PAGE
    assert calls == ["https://example.com/a"]


@pytest.mark.parametrize(
    "answer",
    [None, Extracted("https://example.com/a", "安全验证", "请完成验证码", "remote:deepseek")],
)
async def test_remote_without_content_or_with_a_wall_keeps_the_local_error(
    local: Callable[..., None], answer: Extracted | None
) -> None:
    local(web.Unreadable("网站拒绝了自动访问"))
    with pytest.raises(web.Unreadable, match="拒绝"):
        await web._read("https://example.com/a", None, _remote(answer, []), 5)


async def test_definitive_errors_never_go_remote(local: Callable[..., None]) -> None:
    local(ToolError("这是图片链接，不是网页，请用看图工具查看"))
    calls: list[str] = []
    with pytest.raises(ToolError, match="图片"):
        await web._read("https://example.com/a.png", None, _remote(REMOTE_PAGE, calls), 5)
    assert calls == []


async def test_remote_replaces_a_rendered_shell_but_not_a_short_adapter_answer(local: Callable[..., None]) -> None:
    calls: list[str] = []
    local(Extracted("https://example.com/a", "米游社", "Loading...\n\n数据加载中", "browser"))
    assert await web._read("https://example.com/a", None, _remote(REMOTE_PAGE, calls), 5) is REMOTE_PAGE
    post = Extracted("https://t.me/c/1", "频道", "频道：c\n\n短消息", "telegram")
    local(post)
    assert await web._read("https://t.me/c/1", None, _remote(REMOTE_PAGE, calls), 5) is post
    assert calls == ["https://example.com/a"]


@pytest.mark.parametrize(
    ("status", "ctype"),
    [(200, "image/png"), (200, "video/mp4"), (200, "application/x-msdownload"), (404, "text/html")],
)
async def test_non_page_responses_raise(serve: Callable[[Handler], None], status: int, ctype: str) -> None:
    serve(lambda r: httpx.Response(status, content=b"\x00", headers={"content-type": ctype}))
    with pytest.raises(ToolError):
        await web._fetch_generic("https://example.com/x", None, 5)


async def test_pdf_link_is_parsed_as_document(serve: Callable[[Handler], None]) -> None:
    from tests.tools.test_docs import _text_pdf

    pdf = _text_pdf("Linked PDF body")
    serve(lambda r: httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"}))
    result = await web._fetch_generic("https://example.com/files/paper.pdf", None, 5)
    assert result.via == "document:pdf"
    assert result.title == "paper.pdf"
    assert "Linked PDF body" in result.text


# --------------------------------------------------------------------------- adapters


async def test_bilibili_video_uses_public_api() -> None:
    payload = {
        "code": 0,
        "data": {
            "bvid": "BV1xx411c7mD",
            "title": "字幕君交流场所",
            "owner": {"name": "碧诗"},
            "pubdate": 1252458549,
            "duration": 2055,
            "desc": "www",
            "stat": {"view": 5538888, "danmaku": 129000, "like": 277000},
            "pages": [{"page": 1, "part": "P1", "duration": 1000}, {"page": 2, "part": "P2", "duration": 1055}],
        },
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD/?p=1"))
    assert isinstance(result, Extracted)
    assert seen[0].url.params["bvid"] == "BV1xx411c7mD"
    assert "Mozilla" not in seen[0].headers["user-agent"]
    assert result.title == "字幕君交流场所"
    assert result.final_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert "UP主：碧诗" in result.text
    assert "时长：34:15" in result.text
    assert "播放 553.9万" in result.text
    assert "P2 P2（17:35）" in result.text


async def test_bilibili_deleted_video_raises() -> None:
    async with _client(lambda r: httpx.Response(200, json={"code": -404, "message": "啥都木有"})) as client:
        with pytest.raises(ToolError, match="不存在"):
            await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1GJ411x7h7"))


async def test_bilibili_risk_control_falls_back_to_generic() -> None:
    async with _client(lambda r: httpx.Response(412, html="<html>blocked</html>")) as client:
        assert await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1GJ411x7h7")) is None


async def test_b23_short_link_resolves_to_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://www.bilibili.com/video/BV1xx411c7mD?share_source=qq"})

    async with _client(handler) as client:
        target = await web._bilibili_short_link(client, httpx.URL("https://b23.tv/abcd123"))
    assert target == "https://www.bilibili.com/video/BV1xx411c7mD?share_source=qq"


async def test_github_blob_maps_to_raw() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="print('hi')\n")

    async with _client(handler) as client:
        result = await web._github(client, httpx.URL("https://github.com/o/r/blob/main/src/app.py"))
    assert seen == ["https://raw.githubusercontent.com/o/r/main/src/app.py"]
    assert isinstance(result, Extracted)
    assert result.text == "print('hi')"


async def test_github_issue_pages_use_generic_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("adapter should not fetch")

    async with _client(handler) as client:
        assert await web._github(client, httpx.URL("https://github.com/o/r/issues/1")) is None


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
    text = web.format_hacker_news(HN_ITEM)
    assert text.startswith("作者 op · 42 分 · 5 条评论 · 2026-10-24 00:59\n链接：https://example.com/thing")
    thread = "- alice：First paragraph.\n  Second paragraph.\n  - bob：Reply & more"
    orphan = "- [已删除]：[已删除]\n  - carol：orphan reply"
    # Deleted leaves vanish; deleted parents stay so their replies keep context. Indents survive pagination.
    assert paginate(text, 10_000) == [text]
    assert f"\n\n{thread}\n\n{orphan}" in paginate(text, 10_000)[0]


async def test_hacker_news_item_uses_algolia_and_other_pages_fall_back() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=HN_ITEM)

    async with _client(handler) as client:
        result = await web._hacker_news(client, httpx.URL("https://news.ycombinator.com/item?id=1"))
        assert await web._hacker_news(client, httpx.URL("https://news.ycombinator.com/newest")) is None
    assert seen == ["https://hn.algolia.com/api/v1/items/1"]
    assert isinstance(result, Extracted)
    assert (result.title, result.final_url) == ("Show HN: a thing", "https://news.ycombinator.com/item?id=1")
    assert result.via == "hackernews"


TME_POST = """<html><body><div class="tgme_widget_message">
<div class="tgme_widget_message_author"><a class="tgme_widget_message_owner_name"><span>Chan</span></a></div>
<div class="tgme_widget_message_reply"><div class="tgme_widget_message_text js-message_reply_text">quoted</div></div>
<a class="tgme_widget_message_photo_wrap"></a>
<div class="tgme_widget_message_text js-message_text">line one<br/>line <b>two</b> <i class="emoji"><b>🔥</b></i></div>
<span class="tgme_widget_message_views">1.2K</span>
<time datetime="2026-10-21T13:48:45+00:00" class="datetime">Oct 21</time>
</div></body></html>"""


def test_telegram_embed_yields_post_text_not_the_quoted_reply() -> None:
    result = web.parse_telegram_embed(TME_POST, "https://t.me/chan/7")
    assert result is not None
    assert result.text == (
        "频道：Chan\n发布时间：2026-10-21 21:48\n浏览：1.2K\n\nline one  \nline **two** 🔥\n\n附件：图片 1 张"
    )


def test_telegram_embed_error_and_empty_pages() -> None:
    with pytest.raises(ToolError, match="不存在"):
        web.parse_telegram_embed('<div class="tgme_widget_message_error">Post not found</div>', "https://t.me/c/1")
    assert web.parse_telegram_embed("<html><body><p>Telegram</p></body></html>", "https://t.me/c/1") is None


async def test_telegram_adapter_fetches_embed_for_post_links_only() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, html=TME_POST)

    async with _client(handler) as client:
        result = await web._telegram_post(client, httpx.URL("https://t.me/s/chan/7"))
        assert await web._telegram_post(client, httpx.URL("https://t.me/chan")) is None
    assert seen == ["https://t.me/chan/7?embed=1&mode=tme"]
    assert isinstance(result, Extracted)
    assert result.final_url == "https://t.me/chan/7"

"""How ``read_url`` gets a page: pages and the cache, the plain-HTTP tier (HTML, documents, JSON, walls), the browser
service, the remote fetcher and the fetch deadline."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Mapping

import httpx
import pytest

from tests.support.files import text_pdf
from tests.support.routes import Answer, Routes
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted, RemoteFetch, read_url
from yggdrasil.tools.web.model import ImageLink, Unreadable

type LocalTier = Callable[[Extracted | Exception], list[str]]

ARTICLE = "<html><head><title>测试文章</title></head><body><article>{}</article></body></html>".format(
    "".join(
        f"<p>这是第 {i} 段正文，内容足够长以便抽取器认为它是正文的一部分，而不是导航或者页脚。</p>" for i in range(20)
    )
)
REDDIT = "https://www.reddit.com/r/x/comments/1/y/"
BLOCK_PAGE = (
    "<html><head><title>Reddit - Dive into anything</title></head><body>"
    "<p>You've been blocked by network security.</p><p>To continue, log in to your Reddit account.</p></body></html>"
)


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


# --------------------------------------------------------------------------- pages and cache

THREE_PAGES = Extracted("https://example.com/a", "标题", "\n\n".join(["甲" * 40, "乙" * 40, "丙" * 40]), "http")


async def test_read_url_selects_page_and_caches(local_tier: LocalTier) -> None:
    reads = local_tier(THREE_PAGES)
    first = await read_url("https://example.com/a", max_chars=50)
    last = await read_url("https://example.com/a", max_chars=50, page=3)
    assert (first.page, first.pages, first.truncated, first.text) == (1, 3, True, "甲" * 40)
    assert (last.page, last.truncated, last.text) == (3, False, "丙" * 40)
    assert reads == ["https://example.com/a"]


async def test_read_url_page_out_of_range(local_tier: LocalTier) -> None:
    local_tier(THREE_PAGES)
    with pytest.raises(ToolError, match="共 3 页"):
        await read_url("https://example.com/a", max_chars=50, page=4)


# --------------------------------------------------------------------------- plain HTTP


async def test_gbk_page_without_header_charset_uses_meta(web_page: Routes) -> None:
    html = ARTICLE.replace("<head>", '<head><meta charset="gbk">').encode("gbk")
    web_page.answers["example.com"] = httpx.Response(200, content=html, headers={"content-type": "text/html"})
    assert "这是第 3 段正文" in (await read_url("https://example.com/post")).text


async def test_block_page_without_browser_raises(web_page: Routes) -> None:
    web_page.answers["www.reddit.com"] = httpx.Response(200, html=BLOCK_PAGE)
    with pytest.raises(ToolError, match="拒绝"):
        await read_url(REDDIT)


@pytest.mark.parametrize(
    ("status", "ctype"),
    [(200, "image/png"), (200, "video/mp4"), (200, "application/x-msdownload"), (404, "text/html")],
)
async def test_non_page_responses_raise(web_page: Routes, status: int, ctype: str) -> None:
    web_page.answers["example.com"] = httpx.Response(status, content=b"\x00", headers={"content-type": ctype})
    with pytest.raises(ToolError):
        await read_url("https://example.com/x")


async def test_pdf_link_is_parsed_as_document(web_page: Routes) -> None:
    pdf = text_pdf("Linked PDF body")
    web_page.answers["example.com"] = httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
    result = await read_url("https://example.com/files/paper.pdf")
    assert (result.via, result.title) == ("document:pdf", "paper.pdf")
    assert "Linked PDF body" in result.text


async def test_an_image_link_reports_its_final_url(web_page: Routes) -> None:
    web_page.answers["example.com"] = httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
    with pytest.raises(ImageLink) as raised:
        await read_url("https://example.com/pic")
    assert raised.value.url == "https://example.com/pic"


@pytest.mark.parametrize(("status", "remote_calls"), [(404, []), (410, []), (500, ["https://example.com/x"])])
async def test_only_a_gone_page_skips_the_remote_fetcher(
    web_page: Routes, status: int, remote_calls: list[str]
) -> None:
    web_page.answers["example.com"] = httpx.Response(status, html="<p>error</p>")
    calls: list[str] = []
    with pytest.raises(ToolError, match=f"HTTP {status}"):
        await read_url("https://example.com/x", remote=_remote(None, calls))
    assert calls == remote_calls


def _commit(sha: str, message: str) -> dict:
    user = {
        "login": "octocat",
        "id": 10001,
        "node_id": "MDQ6VXNlcjEwMDAx",
        "avatar_url": "https://avatars.example.com/u/10001?v=4",
        "gravatar_id": "",
        "url": "https://api.example.com/users/octocat",
        "html_url": "https://example.com/octocat",
        "followers_url": "https://api.example.com/users/octocat/followers",
        "following_url": "https://api.example.com/users/octocat/following{/other_user}",
        "type": "User",
    }
    return {
        "sha": sha,
        "node_id": "C_kwDOAAAB",
        "commit": {
            "author": {"name": "Octo Cat", "email": "octo@example.com", "date": "2026-01-02T03:04:05Z"},
            "message": message,
            "url": f"https://api.example.com/repos/acme/widget/git/commits/{sha}",
            "comment_count": 0,
            "verification": {
                "verified": True,
                "reason": "valid",
                "signature": "-----BEGIN PGP SIGNATURE-----\n\niHUEABYKAB0WIQ\n-----END PGP SIGNATURE-----",
                "payload": f"tree {sha}\nauthor Octo Cat <octo@example.com> 1767323045 +0000\n\n{message}",
            },
        },
        "html_url": f"https://example.com/acme/widget/commit/{sha}",
        "author": user,
        "parents": [],
        "homepage": None,
    }


async def test_json_api_response_keeps_values_and_drops_noise(web_page: Routes) -> None:
    payload = [_commit("a" * 40, "fix: handle empty input"), _commit("b" * 40, "docs: usage\n\nMore detail.")]
    body = json.dumps(payload, indent=2).encode()
    ctype = "application/vnd.github+json"  # vendor "+json" types are read as JSON too
    web_page.answers["api.example.com"] = httpx.Response(200, content=body, headers={"content-type": ctype})
    result = await read_url("https://api.example.com/repos/acme/widget/commits")
    assert result.via == "document:json"
    note, text = result.text.split("\n\n", 1)
    assert note == "（JSON 数据，已精简：去掉了空值、node_id、头像、API 链接和签名等字段）"
    for kept in (
        "- sha: " + "a" * 40,
        "message: fix: handle empty input",
        'message: "docs: usage\\n\\nMore detail."',
        "verification: {verified: true, reason: valid}",
        "author: {login: octocat, id: 10001, html_url: https://example.com/octocat, type: User}",
        "comment_count: 0",
    ):
        assert kept in text
    for noise in ("node_id", "avatar", "gravatar", "followers_url", "api.example.com", "PGP", "tree aaaa", "null"):
        assert noise not in text
    assert f"\n\n- sha: {'b' * 40}" in text  # one paragraph per item: pages break between items
    assert len(text) < len(body) / 2


async def test_compacted_json_keeps_every_list_element(web_page: Routes) -> None:
    payload = {"t": [1, 2, 3, 4], "v": [10.5, None, 0, 7], "rows": [["a", None], [], {}], "gone": None, "none": []}
    web_page.answers["api.example.com"] = httpx.Response(200, json=payload)
    result = await read_url("https://api.example.com/series")
    text = result.text.split("\n\n", 1)[1]
    assert text == "t: [1, 2, 3, 4]\nv: [10.5, null, 0, 7]\nrows:\n  - [a, null]\n  - []\n  - {}"


# --------------------------------------------------------------------------- browser service

BROWSER = "http://browser.example.com"


def _browser(pages: Mapping[str, str]) -> Answer:
    """The browser service's ``/render``: the HTML each engine gets; engines not listed fail."""

    def render(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)
        if (html := pages.get(asked["engine"])) is None:
            return httpx.Response(500, text="chrome crashed")
        return httpx.Response(200, json={"final_url": asked["url"], "html": html})

    return render


def _engines(routes: Routes) -> list[str]:
    """The engines the browser service was asked to render with, in order."""
    return [json.loads(r.content)["engine"] for r in routes.seen if r.url.host == "browser.example.com"]


async def test_block_page_escalates_to_the_next_browser_engine(web_page: Routes) -> None:
    web_page.answers.update(
        {
            "www.reddit.com": httpx.Response(200, html=BLOCK_PAGE),
            "browser.example.com": _browser({"patchright": BLOCK_PAGE, "nodriver": ARTICLE}),
        }
    )
    result = await read_url(REDDIT, browser_url=BROWSER)
    assert _engines(web_page) == ["patchright", "nodriver"]
    assert (result.via, result.title) == ("browser:nodriver", "测试文章")


async def test_block_page_is_never_kept_when_the_browser_fails(web_page: Routes) -> None:
    web_page.answers.update(
        {"www.reddit.com": httpx.Response(200, html=BLOCK_PAGE), "browser.example.com": _browser({})}
    )
    with pytest.raises(Unreadable, match="渲染失败"):
        await read_url(REDDIT, browser_url=BROWSER)
    assert _engines(web_page) == ["patchright", "nodriver"]


async def test_browser_failures_keep_the_plain_http_text(web_page: Routes) -> None:
    web_page.answers.update(
        {
            "example.com": httpx.Response(200, html=_page("t", "<p>short plain text</p>")),
            "browser.example.com": _browser({}),
        }
    )
    result = await read_url("https://example.com/a", browser_url=BROWSER)
    assert _engines(web_page) == ["patchright", "nodriver"]
    assert (result.via, result.text) == ("http", "short plain text")


async def test_browser_stops_at_a_captcha_wall(web_page: Routes) -> None:
    captcha = _page("安全验证", "<p>请完成下方验证码</p>")
    web_page.answers.update(
        {
            "www.reddit.com": httpx.Response(200, html=BLOCK_PAGE),
            "browser.example.com": _browser({"patchright": captcha, "nodriver": ARTICLE}),
        }
    )
    with pytest.raises(ToolError, match="验证"):
        await read_url(REDDIT, browser_url=BROWSER)
    assert _engines(web_page) == ["patchright"]  # no captcha fighting: the second engine is not tried


async def test_browser_reports_a_challenge_both_engines_saw(web_page: Routes) -> None:
    challenge = _page("Just a moment...", "<p>Checking if the site connection is secure</p>")
    web_page.answers.update(
        {
            "example.com": httpx.Response(200, html=challenge),
            "browser.example.com": _browser({"patchright": challenge, "nodriver": challenge}),
        }
    )
    with pytest.raises(ToolError, match="just a moment"):
        await read_url("https://example.com/a", browser_url=BROWSER)
    assert _engines(web_page) == ["patchright", "nodriver"]


SPA_SHELL = (
    '<html><head><title>Ore</title><script type="module" src="/app.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)


async def test_a_404_app_shell_is_rendered(web_page: Routes) -> None:
    # A single-page app on GitHub Pages: every deep link is the app's 404.html, which renders the page.
    web_page.answers.update(
        {
            "example.com": httpx.Response(404, html=SPA_SHELL),
            "browser.example.com": _browser({"patchright": ARTICLE}),
        }
    )
    result = await read_url("https://example.com/docs/content", browser_url=BROWSER)
    assert (result.via, result.title) == ("browser", "测试文章")
    assert result.text.startswith("（服务器对这个地址回复 HTTP 404")
    assert "这是第 3 段正文" in result.text


@pytest.mark.parametrize(
    ("page", "rendered"),
    [
        (_page("404", "<p>File not found</p><script>track()</script>"), ARTICLE),  # a real error page has text
        (SPA_SHELL, ""),  # the app renders nothing
    ],
)
async def test_a_404_stays_gone_unless_an_app_renders_it(web_page: Routes, page: str, rendered: str) -> None:
    web_page.answers.update(
        {
            "example.com": httpx.Response(404, html=page),
            "browser.example.com": _browser({"patchright": rendered, "nodriver": rendered}),
        }
    )
    with pytest.raises(ToolError, match="不存在或已删除"):
        await read_url("https://example.com/docs/content", browser_url=BROWSER)


@pytest.mark.parametrize(
    ("html", "renders"),
    [
        (_page("Just a moment...", "<p>Checking if the site connection is secure</p>"), True),
        (_page("安全验证", "<p>请完成下方验证码</p>"), False),
    ],
)
async def test_plain_http_sends_js_challenges_but_not_captchas_to_the_browser(
    web_page: Routes, html: str, renders: bool
) -> None:
    web_page.answers.update(
        {"example.com": httpx.Response(200, html=html), "browser.example.com": _browser({"patchright": ARTICLE})}
    )
    if renders:
        assert (await read_url("https://example.com/a", browser_url=BROWSER)).via == "browser"
    else:
        with pytest.raises(ToolError, match="验证"):
            await read_url("https://example.com/a", browser_url=BROWSER)
        assert _engines(web_page) == []


# --------------------------------------------------------------------------- remote fetcher


def _remote(answer: Extracted | None, calls: list[str]) -> RemoteFetch:
    async def fetch(url: str) -> Extracted | None:
        calls.append(url)
        return answer

    return fetch


REMOTE_PAGE = Extracted("https://example.com/a", "远程", "远程正文。" * 100, "remote:deepseek")


async def test_remote_reads_what_the_local_tiers_could_not(local_tier: LocalTier) -> None:
    local_tier(Unreadable("网站拒绝了自动访问"))
    calls: list[str] = []
    result = await read_url("https://example.com/a", remote=_remote(REMOTE_PAGE, calls))
    assert (result.via, result.title, result.text) == ("remote:deepseek", "远程", REMOTE_PAGE.text)
    assert calls == ["https://example.com/a"]


@pytest.mark.parametrize(
    "answer",
    [None, Extracted("https://example.com/a", "安全验证", "请完成验证码", "remote:deepseek")],
)
async def test_remote_without_content_or_with_a_wall_keeps_the_local_error(
    local_tier: LocalTier, answer: Extracted | None
) -> None:
    local_tier(Unreadable("网站拒绝了自动访问"))
    with pytest.raises(Unreadable, match="拒绝"):
        await read_url("https://example.com/a", remote=_remote(answer, []))


async def test_definitive_errors_never_go_remote(local_tier: LocalTier) -> None:
    local_tier(ToolError("这是图片链接，不是网页，请用看图工具查看"))
    calls: list[str] = []
    with pytest.raises(ToolError, match="图片"):
        await read_url("https://example.com/a.png", remote=_remote(REMOTE_PAGE, calls))
    assert calls == []


async def test_remote_replaces_a_rendered_shell_but_not_a_short_adapter_answer(local_tier: LocalTier) -> None:
    calls: list[str] = []
    local_tier(Extracted("https://example.com/a", "米游社", "Loading...\n\n数据加载中", "browser"))
    assert (await read_url("https://example.com/a", remote=_remote(REMOTE_PAGE, calls))).via == "remote:deepseek"
    local_tier(Extracted("https://t.me/c/1", "频道", "频道：c\n\n短消息", "telegram"))
    assert (await read_url("https://t.me/c/1", remote=_remote(REMOTE_PAGE, calls))).via == "telegram"
    assert calls == ["https://example.com/a"]


# --------------------------------------------------------------------------- deadline


async def test_stalled_adapter_or_download_times_out(web_page: Routes) -> None:
    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(504)

    async def trickle() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.001)
            yield b"x"

    web_page.answers.update(
        {
            "status.example.com": stalled,  # the Statuspage adapter's API call
            "example.com": lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=trickle()),
        }
    )
    started = time.perf_counter()
    for url in ("https://status.example.com/incidents/abcd1234efgh", "https://example.com/slow"):
        with pytest.raises(Unreadable, match="网页加载超时"):
            await read_url(url, timeout=0.01)
    assert time.perf_counter() - started < 10

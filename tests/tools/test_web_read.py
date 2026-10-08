from collections.abc import Callable

import httpx
import pytest

from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted, detect_wall, paginate, read_url

type Handler = Callable[[httpx.Request], httpx.Response]

ARTICLE = "<html><head><title>测试文章</title></head><body><article>{}</article></body></html>".format(
    "".join(
        f"<p>这是第 {i} 段正文，内容足够长以便抽取器认为它是正文的一部分，而不是导航或者页脚。</p>" for i in range(20)
    )
)


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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


# --------------------------------------------------------------------------- read_url


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def check(url: str) -> str:
        return url

    async def fetch(url: str, browser_ws_url: str | None, timeout_s: float) -> Extracted:
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


async def test_html_article_is_extracted() -> None:
    async with _client(lambda r: httpx.Response(200, html=ARTICLE)) as client:
        result = await web._fetch_generic(client, "https://example.com/post", None, 5)
    assert result.via == "http"
    assert result.title == "测试文章"
    assert "这是第 3 段正文" in result.text


async def test_gbk_page_without_header_charset_uses_meta() -> None:
    html = ARTICLE.replace("<head>", '<head><meta charset="gbk">').encode("gbk")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    async with _client(handler) as client:
        result = await web._fetch_generic(client, "https://example.com/post", None, 5)
    assert "这是第 3 段正文" in result.text


async def test_verification_page_raises() -> None:
    wall = (
        "<html><head><title>微信公众平台</title></head><body>"
        "<p>环境异常</p><p>当前环境异常，完成验证后即可继续访问。</p></body></html>"
    )
    async with _client(lambda r: httpx.Response(200, html=wall)) as client:
        with pytest.raises(ToolError, match="验证"):
            await web._fetch_generic(client, "https://mp.weixin.qq.com/s/abc", None, 5)


@pytest.mark.parametrize(
    ("status", "ctype"),
    [(200, "image/png"), (200, "video/mp4"), (200, "application/x-msdownload"), (404, "text/html")],
)
async def test_non_page_responses_raise(status: int, ctype: str) -> None:
    async with _client(lambda r: httpx.Response(status, content=b"\x00", headers={"content-type": ctype})) as client:
        with pytest.raises(ToolError):
            await web._fetch_generic(client, "https://example.com/x", None, 5)


async def test_pdf_link_is_parsed_as_document() -> None:
    from tests.tools.test_docs import _text_pdf

    pdf = _text_pdf("Linked PDF body")
    async with _client(
        lambda r: httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
    ) as client:
        result = await web._fetch_generic(client, "https://example.com/files/paper.pdf", None, 5)
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

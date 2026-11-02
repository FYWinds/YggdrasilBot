"""The Telegram adapter of ``read_url``: public channel posts from their embed widget."""

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted

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
    routes = Routes({"t.me": httpx.Response(200, html=TME_POST)})
    async with routes.client() as client:
        result = await web._telegram_post(client, httpx.URL("https://t.me/s/chan/7"))
        assert await web._telegram_post(client, httpx.URL("https://t.me/chan")) is None
    assert [str(r.url) for r in routes.seen] == ["https://t.me/chan/7?embed=1&mode=tme"]
    assert isinstance(result, Extracted)
    assert result.final_url == "https://t.me/chan/7"

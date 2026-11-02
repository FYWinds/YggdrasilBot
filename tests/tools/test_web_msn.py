"""The MSN adapter of ``read_url``: articles from the content API that MSN's own script pages load."""

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted, read_url
from yggdrasil.tools.web.model import Unreadable

MSN_ARTICLE = {
    "title": "Storm closes the harbour",
    "abstract": "A storm closed the harbour.",
    "body": (
        '<div><img data-reference="image" data-document-id="cms/api/amp/image/AA1img"></div>'
        "<p>A storm closed the harbour on Monday.</p><p>Ferries resume on Wednesday.</p>"
    ),
    "provider": {"name": "Example News"},
    "authors": [{"name": "Jane Doe"}],
    "publishedDateTime": "2026-01-02T03:04:05Z",
    "sourceHref": "https://news.example.com/storm",
}
# What the browser rendered for an MSN article in production (the IAB consent dialog), trimmed.
MSN_CONSENT = (
    "<html><head><title>Storm closes the harbour</title></head><body><div>"
    "<p>Microsoft and our third-party vendors use cookies to store and access information such as unique IDs and "
    "personal data/personal information to deliver, maintain, and improve our services and ads. If you agree, MSN "
    "and Microsoft Bing will personalise the content and ads that you see. You can select 'I Accept' to consent to "
    "these uses or click on 'Manage preferences' to review your options.</p>"
    "<p>Privacy statement Number of Partners (vendors): 1019.</p><p>We and our partners process data to</p>"
    "<p>Use precise geolocation data and identify devices based on information actively requested.</p>"
    "</div></body></html>"
)


async def test_msn_article_comes_from_the_content_api() -> None:
    def content_api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/AA1abcDE"):
            return httpx.Response(200, json=MSN_ARTICLE)
        return httpx.Response(410, json=[])

    routes = Routes({"assets.msn.com": content_api})
    async with routes.client() as client:
        url = "https://www.msn.com/en-in/news/other/storm-closes-the-harbour/ar-AA1abcDE?ocid=share"
        result = await web._msn_article(client, httpx.URL(url))
        with pytest.raises(ToolError, match="下架"):
            await web._msn_article(client, httpx.URL("https://www.msn.com/en-us/news/world/gone/ar-AA0gone0"))
        assert await web._msn_article(client, httpx.URL("https://www.msn.com/en-us/feed")) is None
    assert str(routes.seen[0].url) == "https://assets.msn.com/content/view/v2/Detail/en-in/AA1abcDE"
    assert isinstance(result, Extracted)
    assert (result.title, result.via) == ("Storm closes the harbour", "msn")
    assert result.text == (
        "来源：Example News · 作者：Jane Doe\n"
        "发布时间：2026-01-02 11:04\n"
        "原文：https://news.example.com/storm\n"
        "\n"
        "A storm closed the harbour on Monday.\n\nFerries resume on Wednesday."
    )


async def test_msn_page_without_the_content_api_explains_the_consent_wall(web_page: Routes) -> None:
    web_page.answers.update(
        {"assets.msn.com": httpx.Response(503), "www.msn.com": httpx.Response(200, html=MSN_CONSENT)}
    )
    with pytest.raises(Unreadable, match="Cookie 同意弹窗"):
        await read_url("https://www.msn.com/en-in/news/other/storm/ar-AA1abcDE")
    assert [r.url.host for r in web_page.seen] == ["assets.msn.com", "www.msn.com"]

"""The Bilibili adapter of ``read_url``: video facts from the public API, subtitles as a transcript, short links."""

from typing import Any

import httpx
import pytest

from tests.support.routes import Answer, Routes
from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted
from yggdrasil.tools.web.model import API_USER_AGENT

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
SUBTITLES_API = "https://api.bilibili.com/x/v2/dm/view"


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
    routes = Routes({VIEW_API: httpx.Response(200, json=payload)})
    async with routes.client() as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD/?p=1"))
    assert isinstance(result, Extracted)
    assert routes.seen[0].url.params["bvid"] == "BV1xx411c7mD"
    assert "Mozilla" not in routes.seen[0].headers["user-agent"]
    assert result.title == "字幕君交流场所"
    assert result.final_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert "UP主：碧诗" in result.text
    assert "时长：34:15" in result.text
    assert "播放 553.9万" in result.text
    assert "P2 P2（17:35）" in result.text


async def test_bilibili_deleted_video_raises() -> None:
    async with Routes({VIEW_API: httpx.Response(200, json={"code": -404, "message": "啥都木有"})}).client() as client:
        with pytest.raises(ToolError, match="不存在"):
            await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1GJ411x7h7"))


async def test_bilibili_risk_control_falls_back_to_generic() -> None:
    async with Routes({VIEW_API: httpx.Response(412, html="<html>blocked</html>")}).client() as client:
        assert await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1GJ411x7h7")) is None


async def test_b23_short_link_resolves_to_target() -> None:
    target = "https://www.bilibili.com/video/BV1xx411c7mD?share_source=qq"
    async with Routes({"b23.tv": httpx.Response(302, headers={"Location": target})}).client() as client:
        assert await web._bilibili_short_link(client, httpx.URL("https://b23.tv/abcd123")) == target


# --------------------------------------------------------------------------- subtitles

BILI_VIEW: dict[str, Any] = {
    "code": 0,
    "data": {
        "bvid": "BV1xx411c7mD",
        "aid": 99,
        "cid": 111,
        "title": "测试视频",
        "owner": {"name": "UP"},
        "stat": {},
        "pages": [{"page": 1, "part": "上", "cid": 111, "duration": 60}, {"page": 2, "part": "下", "cid": 222}],
        "subtitle": {"list": []},
    },
}


def _bili_routes(dm_view: Answer, view: dict[str, Any] = BILI_VIEW) -> Routes:
    return Routes({VIEW_API: httpx.Response(200, json=view), SUBTITLES_API: dm_view})


async def test_bilibili_subtitles_become_a_timed_transcript() -> None:
    def dm_view(request: httpx.Request) -> httpx.Response:
        assert (request.url.params["oid"], request.url.params["pid"]) == ("222", "99")  # the ?p=2 part
        tracks = [
            {
                "lan": "ai-zh",
                "lan_doc": "中文（自动生成）",
                "type": 1,
                "ai_type": 0,
                "subtitle_url": "//sub.example.com/ai",
            },
            {
                "lan": "zh-CN",
                "lan_doc": "中文（中国）",
                "type": 0,
                "ai_type": 0,
                "subtitle_url": "http://sub.example.com/cc",
            },
        ]
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": tracks}}})

    def subtitle(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        cues = [
            {"from": 0, "content": "第一句"},
            {"from": 30.5, "content": "第二句\n"},
            {"from": 61, "content": "第三句"},
        ]
        return httpx.Response(200, json={"body": cues})

    routes = _bili_routes(dm_view)
    routes.answers["https://sub.example.com/cc"] = subtitle
    async with routes.client() as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD?p=2"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert result.text.endswith("字幕（P2 · 中文（中国））：\n\n[0:00] 第一句 第二句\n\n[1:01] 第三句")
    assert "https://sub.example.com/ai" not in [str(r.url) for r in routes.seen]  # uploaded subtitles win over AI
    assert all(r.headers["user-agent"] == API_USER_AGENT for r in routes.seen)


async def test_bilibili_part_number_out_of_range_reads_the_first_part() -> None:
    def dm_view(request: httpx.Request) -> httpx.Response:
        assert request.url.params["oid"] == "111"
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}})

    routes = _bili_routes(dm_view)
    async with routes.client() as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD?p=9"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://www.bilibili.com/video/BV1xx411c7mD"


@pytest.mark.parametrize(
    ("dm_view", "listed", "note"),
    [
        (httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}}), [], "这个视频没有字幕"),
        (
            httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}}),
            [{"lan": "zh-CN", "subtitle_url": ""}],
            "这个视频有字幕，但 B 站要求登录才能读取",
        ),
        (httpx.Response(412, html="<html>blocked</html>"), [], "字幕读取失败"),
    ],
)
async def test_bilibili_without_subtitles_says_why(dm_view: httpx.Response, listed: list, note: str) -> None:
    view = {**BILI_VIEW, "data": {**BILI_VIEW["data"], "subtitle": {"list": listed}}}
    async with _bili_routes(dm_view, view).client() as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD"))
    assert isinstance(result, Extracted)
    assert result.text.endswith(f"（{note}，只有标题和简介，无法得知视频里具体说了什么）")

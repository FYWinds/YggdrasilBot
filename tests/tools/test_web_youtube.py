"""The YouTube adapter of ``read_url``: what the watch page shows, and the captions as a transcript."""

import json

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools import web
from yggdrasil.tools.web import Extracted


def _yt_markers(*items: tuple[str, str]) -> dict:
    return {
        "macroMarkersListRenderer": {
            "contents": [
                {"macroMarkersListItemRenderer": {"title": {"simpleText": t}, "timeDescription": {"simpleText": at}}}
                for at, t in items
            ]
        }
    }


YT_DATA = {
    "contents": {
        "results": [
            {
                "videoPrimaryInfoRenderer": {
                    "title": {"runs": [{"text": "CRDTs "}, {"text": "for Git"}]},
                    "viewCount": {"videoViewCountRenderer": {"viewCount": {"simpleText": "12,632次观看"}}},
                    "dateText": {"simpleText": "2026年10月9日"},
                }
            },
            {
                "videoSecondaryInfoRenderer": {
                    "owner": {
                        "videoOwnerRenderer": {
                            "title": {"runs": [{"text": "Zed"}]},
                            "subscriberCountText": {"simpleText": "1.89万位订阅者"},
                        }
                    },
                    # A "};" inside a string must not end the JSON early.
                    "attributedDescription": {"content": "Talk at the Rust meetup.\nfn x() {};"},
                }
            },
        ]
    },
    # Chapters show up in more than one panel.
    "engagementPanels": [_yt_markers(("0:00", "Intro"), ("1:33", "Design")), _yt_markers(("0:00", "Intro"))],
}
YT_PAGE = f"<script>var ytInitialData = {json.dumps(YT_DATA, ensure_ascii=False)};</script><script>var x = 1;</script>"
YT_TEXT = (
    "频道：Zed（1.89万位订阅者）\n发布时间：2026年10月9日\n播放：12,632次观看\n\n"
    "简介：\nTalk at the Rust meetup.\nfn x() {};\n\n"
    "章节（共 2 个）：\n0:00 Intro\n1:33 Design"
)


def test_youtube_watch_page_yields_metadata_description_and_chapters() -> None:
    result = web.parse_youtube_watch(YT_PAGE, "https://www.youtube.com/watch?v=yif2rday2bw")
    assert result is not None
    assert (result.title, result.via, result.text) == ("CRDTs for Git", "youtube", YT_TEXT)
    # No ytInitialData, or no video in it (removed / private): leave it to the generic path.
    assert web.parse_youtube_watch("<html>consent</html>", "https://www.youtube.com/watch?v=x") is None
    assert web.parse_youtube_watch('<script>var ytInitialData = {"contents": {}};</script>', "u") is None


async def test_youtube_adapter_reads_video_links_and_skips_other_pages() -> None:
    routes = Routes({"https://www.youtube.com/watch": httpx.Response(200, html=YT_PAGE)})
    async with routes.client() as client:
        for url in (
            "https://www.youtube.com/watch?v=yif2rday2bw&t=5326s",
            "https://youtu.be/yif2rday2bw?si=abc",
            "https://m.youtube.com/shorts/yif2rday2bw",
        ):
            result = await web._youtube_video(client, httpx.URL(url))
            assert isinstance(result, Extracted)
            assert result.final_url == "https://www.youtube.com/watch?v=yif2rday2bw"
        for other in ("https://www.youtube.com/@zed", "https://www.youtube.com/results?search_query=x"):
            assert await web._youtube_video(client, httpx.URL(other)) is None
    watched = [str(r.url) for r in routes.seen if r.url.path == "/watch"]
    assert watched == ["https://www.youtube.com/watch?v=yif2rday2bw&hl=zh-CN"] * 3


# --------------------------------------------------------------------------- transcript

YT_TRACKS = [
    {
        "baseUrl": "https://www.youtube.com/api/timedtext?v=x&kind=asr&lang=en&fmt=srv3",
        "kind": "asr",
        "languageCode": "en",
    },
    {
        "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en&fmt=srv3",
        "languageCode": "en",
        "name": {"runs": [{"text": "英语"}]},
    },
]


def _yt_routes(player: dict) -> Routes:
    def player_api(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert (sent["videoId"], sent["context"]["client"]["clientName"]) == ("yif2rday2bw", "ANDROID")
        return httpx.Response(200, json=player)

    def captions(request: httpx.Request) -> httpx.Response:
        assert "kind" not in request.url.params  # the uploaded track, not the auto-generated one
        assert request.url.params.get_list("fmt") == ["json3"]
        events = [
            {"tStartMs": 0, "dDurationMs": 60000, "id": 1},
            {"tStartMs": 500, "segs": [{"utf8": "Hello"}, {"utf8": " world", "tOffsetMs": 300}]},
            {"tStartMs": 900, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 65000, "segs": [{"utf8": "Second\nminute"}]},
        ]
        return httpx.Response(200, json={"events": events})

    return Routes(
        {
            "https://www.youtube.com/watch": httpx.Response(200, html=YT_PAGE),
            "https://www.youtube.com/youtubei/v1/player": player_api,
            "https://www.youtube.com/api/timedtext": captions,
        }
    )


async def test_youtube_transcript_comes_from_the_android_player() -> None:
    player = {
        "playabilityStatus": {"status": "OK"},
        "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": YT_TRACKS}},
    }
    async with _yt_routes(player).client() as client:
        result = await web._youtube_video(client, httpx.URL("https://youtu.be/yif2rday2bw"))
    assert isinstance(result, Extracted)
    assert result.text == f"{YT_TEXT}\n\n字幕（英语）：\n\n[0:00] Hello world\n\n[1:05] Second minute"


@pytest.mark.parametrize(
    ("player", "note"),
    [
        (
            {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you’re not a bot"}},
            "YouTube 不给字幕（Sign in to confirm you’re not a bot）",
        ),
        ({"playabilityStatus": {"status": "OK"}}, "这个视频没有字幕"),
    ],
)
async def test_youtube_without_captions_says_why(player: dict, note: str) -> None:
    async with _yt_routes(player).client() as client:
        result = await web._youtube_video(client, httpx.URL("https://www.youtube.com/watch?v=yif2rday2bw"))
    assert isinstance(result, Extracted)
    assert result.text.endswith(f"\n\n（{note}，只有标题和简介，无法得知视频里具体说了什么）")

"""Site adapters of ``read_url``: GitHub pages, Tangled code, Bilibili / YouTube transcripts."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from yggdrasil.tools import web
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import API_USER_AGENT, Extracted

type Routes = dict[str, Callable[[httpx.Request], httpx.Response] | httpx.Response]


def _client(routes: Routes, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """A client answering by ``host + path``; unrouted requests fail the test."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        answer = routes.get(f"{request.url.host}{request.url.path}")
        if answer is None:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        return answer(request) if callable(answer) else answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _paths(seen: list[httpx.Request]) -> list[str]:
    return [f"{r.url.host}{r.url.path}" for r in seen]


# --------------------------------------------------------------------------- GitHub


async def test_github_tree_lists_the_folder_and_its_readme() -> None:
    def contents(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ref"] == "feature/x"
        entries = [
            {"name": "app.py", "type": "file", "size": 2048},
            {"name": "README.md", "type": "file", "size": 9, "download_url": "https://raw.example.com/readme"},
            {"name": "sub", "type": "dir", "size": 0},
        ]
        return httpx.Response(200, json=entries)

    no_ref = httpx.Response(404, json={"message": "No commit found for the ref feature"})
    routes: Routes = {
        # The ref "feature/x" has a slash: the first guess (ref "feature") is not a branch.
        "api.github.com/repos/o/r/contents/x/pkg": no_ref,
        "api.github.com/repos/o/r/contents/pkg": contents,
        "raw.example.com/readme": httpx.Response(200, text="Package docs\n"),
    }
    async with _client(routes) as client:
        result = await web._github(client, httpx.URL("https://github.com/o/r/tree/feature/x/pkg"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://github.com/o/r/tree/feature/x/pkg"
    assert result.text == (
        "仓库：o/r（feature/x）\n目录：pkg\n\n内容（3 项）：\nsub/\napp.py（2.0 KB）\nREADME.md（9 B）\n\n"
        "读取文件：https://github.com/o/r/blob/feature/x/pkg/<文件名>\n\nREADME.md：\n\nPackage docs"
    )


async def test_github_tree_of_a_missing_folder_raises() -> None:
    async with _client({"api.github.com/repos/o/r/contents/nope": httpx.Response(404, json={})}) as client:
        with pytest.raises(ToolError, match="没有这个目录"):
            await web._github(client, httpx.URL("https://github.com/o/r/tree/main/nope"))


async def test_github_repo_without_readme_lists_the_top_level_files() -> None:
    files = [{"name": "install.sh", "type": "file", "size": 688}, {"name": "docs", "type": "dir", "size": 0}]
    routes: Routes = {
        "api.github.com/repos/o/r": httpx.Response(200, json={"full_name": "o/r", "language": "Shell"}),
        "raw.githubusercontent.com/o/r/HEAD/README.md": httpx.Response(404),
        "api.github.com/repos/o/r/readme": httpx.Response(404, json={"message": "Not Found"}),
        "api.github.com/repos/o/r/contents": httpx.Response(200, json=files),
    }
    async with _client(routes) as client:
        result = await web._github(client, httpx.URL("https://github.com/o/r"))
    assert isinstance(result, Extracted)
    assert result.text.endswith("仓库没有 README。根目录（2 项）：\ndocs/\ninstall.sh（688 B）")


REPOS = [
    {"name": "tool", "fork": True, "language": "Rust", "stargazers_count": 5, "pushed_at": "2026-10-19T10:03:14Z"},
    {"name": "notes", "description": "Personal notes", "archived": True, "pushed_at": "2025-01-02T00:00:00Z"},
]


async def test_github_profile_tab_lists_recently_pushed_repos() -> None:
    def repos(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sort"] == "pushed"
        return httpx.Response(200, json=REPOS)

    user = {"login": "someone", "type": "User", "public_repos": 2, "followers": 7, "created_at": "2024-07-01T12:54:02Z"}
    routes: Routes = {
        "api.github.com/users/someone": httpx.Response(200, json=user),
        "api.github.com/users/someone/repos": repos,
    }
    async with _client(routes) as client:
        result = await web._github(client, httpx.URL("https://github.com/someone?tab=repositories&sort=updated"))
    assert isinstance(result, Extracted)
    assert (result.title, result.final_url) == ("GitHub 用户 someone", "https://github.com/someone")
    assert result.text == (
        "GitHub 用户：someone\n公开仓库 2 · 关注者 7 · 关注 0 · 注册于 2024-07-01\n\n最近推送的仓库：\n"
        "- tool（fork） · Rust · ★ 5 · 推送于 2026-10-19\n"
        "- notes（已归档） · ★ 0 · 推送于 2025-01-02\n  Personal notes"
    )


async def test_github_org_overview_adds_the_profile_readme() -> None:
    org = {"login": "acme", "type": "Organization", "name": "Acme", "bio": "Tools", "blog": "https://example.com"}
    routes: Routes = {
        "api.github.com/users/acme": httpx.Response(200, json=org),
        "api.github.com/users/acme/repos": httpx.Response(200, json=[]),
        "raw.githubusercontent.com/acme/.github/HEAD/profile/README.md": httpx.Response(200, text="# Hi from Acme\n"),
    }
    async with _client(routes) as client:
        result = await web._github(client, httpx.URL("https://github.com/orgs/acme"))
    assert isinstance(result, Extracted)
    assert result.text.startswith("GitHub 组织：acme（Acme）\n简介：Tools\n网站 https://example.com\n")
    assert result.text.endswith("主页 README：\n\n# Hi from Acme")


ISSUE = {
    "number": 7,
    "title": "Crash on start",
    "html_url": "https://github.com/o/r/issues/7",
    "state": "closed",
    "state_reason": "not_planned",
    "user": {"login": "reporter"},
    "labels": [{"name": "bug"}, {"name": "wontfix"}],
    "created_at": "2026-10-25T10:02:32Z",
    "closed_at": "2026-10-26T00:00:00Z",
    "comments": 2,
    "body": "<!-- Describe the bug -->\r\nIt crashes.\r\n\r\nSteps: run it.",
}
ISSUE_COMMENTS = [
    {
        "user": {"login": "maint"},
        "author_association": "MEMBER",
        "created_at": "2026-10-25T12:00:00Z",
        "body": "Works for me.",
    },
    {
        "user": {"login": "spammer"},
        "author_association": "NONE",
        "created_at": "2026-10-25T13:00:00Z",
        "body": "buy now",
        "minimized": {"reason": "spam"},
    },
]


async def test_github_issue_reads_the_thread_from_the_api() -> None:
    routes: Routes = {
        "api.github.com/repos/o/r/issues/7": httpx.Response(200, json=ISSUE),
        "api.github.com/repos/o/r/issues/7/comments": httpx.Response(200, json=ISSUE_COMMENTS),
    }
    async with _client(routes) as client:
        result = await web._github(client, httpx.URL("https://github.com/o/r/issues/7"))
    assert isinstance(result, Extracted)
    assert result.title == "Crash on start · Issue #7 · o/r"
    assert result.text == (
        "Issue #7 · 已关闭（不计划处理）\n作者：reporter · 创建于 2026-10-25 18:02 · 关闭于 2026-10-26 08:00\n"
        "标签：bug、wontfix\n\n正文：\n\nIt crashes.\n\nSteps: run it.\n\n评论（2 条）：\n\n"
        "【maint（维护者） · 2026-10-25 20:00】\nWorks for me.\n\n【spammer · 2026-10-25 21:00】\n（已被折叠：spam）"
    )


def test_github_body_with_unclosed_comments_is_read_in_linear_time() -> None:
    # 64k characters of "<!--" that never close: the lazy comment regex used to scan to the end from each of them.
    tail = "<!--" * 16_384
    issue = {**ISSUE, "body": f"<!-- template -->\r\nIt crashes.\r\n{tail}", "comments": 0}
    started = time.perf_counter()
    text = web.format_github_issue(issue, [])
    assert time.perf_counter() - started < 0.5
    assert text.endswith(f"正文：\n\nIt crashes.\n{tail}")  # closed comments go; an unclosed "<!--" stays as text


PULL = {
    "number": 9,
    "title": "Add provider",
    "html_url": "https://github.com/o/r/pull/9",
    "state": "closed",
    "draft": True,
    "merged": False,
    "merged_at": None,
    "user": {"login": "dev"},
    "created_at": "2026-10-24T14:51:21Z",
    "closed_at": "2026-10-24T18:59:32Z",
    "head": {"label": "dev:feature"},
    "base": {"label": "o:main"},
    "commits": 3,
    "changed_files": 76,
    "additions": 6494,
    "deletions": 7,
    "comments": 1,
    "review_comments": 2,
    "body": "Adds a provider.",
}
REVIEWS = [
    {
        "user": {"login": "ci[bot]", "type": "Bot"},
        "state": "COMMENTED",
        "submitted_at": "2026-10-24T15:00:00Z",
        "body": "Tests pass.",
    },
    # The empty wrapper of inline comments, and a pending review: not part of the conversation.
    {"user": {"login": "lead"}, "state": "COMMENTED", "submitted_at": "2026-10-24T15:30:00Z", "body": ""},
    {"user": {"login": "lead"}, "state": "PENDING", "body": "draft notes"},
    {
        "user": {"login": "lead"},
        "author_association": "OWNER",
        "state": "APPROVED",
        "submitted_at": "2026-10-24T16:00:00Z",
        "body": "",
    },
]
PULL_COMMENTS = [
    {
        "user": {"login": "dev"},
        "author_association": "MEMBER",
        "created_at": "2026-10-24T19:00:49Z",
        "body": "Moved to #12",
    }
]


@pytest.mark.parametrize("path", ["pull/9", "issues/9"])
async def test_github_pull_request_has_state_branches_and_conversation(path: str) -> None:
    seen: list[httpx.Request] = []
    routes: Routes = {
        # An issue link to a PR: the issues API says so, the pulls API has the rest.
        "api.github.com/repos/o/r/issues/9": httpx.Response(200, json={**PULL, "pull_request": {"url": "u"}}),
        "api.github.com/repos/o/r/pulls/9": httpx.Response(200, json=PULL),
        "api.github.com/repos/o/r/issues/9/comments": httpx.Response(200, json=PULL_COMMENTS),
        "api.github.com/repos/o/r/pulls/9/reviews": httpx.Response(200, json=REVIEWS),
    }
    async with _client(routes, seen) as client:
        result = await web._github(client, httpx.URL(f"https://github.com/o/r/{path}"))
    assert isinstance(result, Extracted)
    assert result.title == "Add provider · PR #9 · o/r"
    assert result.text == (
        "PR #9 · 已关闭，未合并 · 草稿\n"
        "提示：dev 说「Moved to #12」，已转移或被取代：https://github.com/o/r/pull/12\n"
        "作者：dev · 创建于 2026-10-24 22:51 · 关闭于 2026-10-25 02:59\n"
        "分支：dev:feature → o:main\n改动：3 个提交 · 76 个文件 · +6494 −7\n\n正文：\n\nAdds a provider.\n\n"
        "评论（3 条；另有 2 条代码行内审查评论未列出）：\n\n"
        "【ci[bot]（机器人） · 审查 · 2026-10-24 23:00】\nTests pass.\n\n"
        "【lead（维护者） · 审查：批准 · 2026-10-25 00:00】\n\n"
        "【dev（作者、维护者） · 2026-10-25 03:00】\nMoved to #12"
    )
    assert "api.github.com/repos/o/r/pulls/9/comments" not in _paths(seen)  # inline review threads stay out


def test_github_merged_pull_request_state() -> None:
    merged = {**PULL, "draft": False, "merged_at": "2026-10-28T12:00:00Z", "merged_by": {"login": "lead"}}
    assert web.format_github_pull(merged, [], []).startswith("PR #9 · 已合并（2026-10-28 20:00，合并者 lead）\n")


async def test_github_pull_link_to_an_issue_number_is_redirected() -> None:
    async with _client({"api.github.com/repos/o/r/pulls/5": httpx.Response(404, json={})}) as client:
        target = await web._github(client, httpx.URL("https://github.com/o/r/pull/5"))
    assert target == "https://github.com/o/r/issues/5"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/pull/9",
        "https://github.com/someone",
        "https://github.com/o/r/tree/main/src",
    ],
)
async def test_github_rate_limit_falls_back_to_the_generic_path(url: str) -> None:
    limited = httpx.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: limited)) as client:
        assert await web._github(client, httpx.URL(url)) is None


# --------------------------------------------------------------------------- Tangled


async def test_tangled_blob_reads_the_raw_file_with_its_lines() -> None:
    code = 'fn main() {\n    println!("hi");\n}\n'

    def raw(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == API_USER_AGENT  # browser UAs without a browser TLS stack get 403
        return httpx.Response(200, text=code, headers={"content-type": "text/plain; charset=utf-8"})

    routes: Routes = {"tangled.org/alice.example.com/repo/raw/main/src/main.rs": raw}
    async with _client(routes) as client:
        result = await web._tangled_blob(
            client, httpx.URL("https://tangled.org/alice.example.com/repo/blob/main/src/main.rs")
        )
        other = await web._tangled_blob(client, httpx.URL("https://tangled.org/alice.example.com/repo/issues/1"))
    assert isinstance(result, Extracted)
    assert (result.text, result.via) == (code.strip(), "tangled")
    assert result.title == "alice.example.com/repo: src/main.rs"
    assert other is None


# --------------------------------------------------------------------------- Bilibili

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


def _bili_routes(dm_view: httpx.Response, view: dict[str, Any] = BILI_VIEW) -> Routes:
    return {
        "api.bilibili.com/x/web-interface/view": httpx.Response(200, json=view),
        "api.bilibili.com/x/v2/dm/view": dm_view,
    }


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

    routes = {
        **_bili_routes(httpx.Response(200)),
        "api.bilibili.com/x/v2/dm/view": dm_view,
        "sub.example.com/cc": subtitle,
    }
    seen: list[httpx.Request] = []
    async with _client(routes, seen) as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD?p=2"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert result.text.endswith("字幕（P2 · 中文（中国））：\n\n[0:00] 第一句 第二句\n\n[1:01] 第三句")
    assert "sub.example.com/ai" not in _paths(seen)  # uploaded subtitles win over AI ones
    assert all(r.headers["user-agent"] == API_USER_AGENT for r in seen)


async def test_bilibili_part_number_out_of_range_reads_the_first_part() -> None:
    def dm_view(request: httpx.Request) -> httpx.Response:
        assert request.url.params["oid"] == "111"
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}})

    async with _client({**_bili_routes(httpx.Response(200)), "api.bilibili.com/x/v2/dm/view": dm_view}) as client:
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
    async with _client(_bili_routes(dm_view, view)) as client:
        result = await web._bilibili_video(client, httpx.URL("https://www.bilibili.com/video/BV1xx411c7mD"))
    assert isinstance(result, Extracted)
    assert result.text.endswith(f"（{note}，只有标题和简介，无法得知视频里具体说了什么）")


# --------------------------------------------------------------------------- YouTube

YT_DATA = {
    "contents": [
        {"videoPrimaryInfoRenderer": {"title": {"simpleText": "A talk"}}},
        {
            "videoSecondaryInfoRenderer": {
                "owner": {"videoOwnerRenderer": {"title": {"simpleText": "Example Channel"}}},
                "attributedDescription": {"content": "About things."},
            }
        },
    ]
}
YT_PAGE = f"<script>var ytInitialData = {json.dumps(YT_DATA)};</script>"
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

    return {
        "www.youtube.com/watch": httpx.Response(200, html=YT_PAGE),
        "www.youtube.com/youtubei/v1/player": player_api,
        "www.youtube.com/api/timedtext": captions,
    }


async def test_youtube_transcript_comes_from_the_android_player() -> None:
    player = {
        "playabilityStatus": {"status": "OK"},
        "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": YT_TRACKS}},
    }
    async with _client(_yt_routes(player)) as client:
        result = await web._youtube_video(client, httpx.URL("https://youtu.be/yif2rday2bw"))
    assert isinstance(result, Extracted)
    assert result.text == (
        "频道：Example Channel\n\n简介：\nAbout things.\n\n字幕（英语）：\n\n[0:00] Hello world\n\n[1:05] Second minute"
    )


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
    async with _client(_yt_routes(player)) as client:
        result = await web._youtube_video(client, httpx.URL("https://www.youtube.com/watch?v=yif2rday2bw"))
    assert isinstance(result, Extracted)
    assert result.text.endswith(f"\n\n（{note}，只有标题和简介，无法得知视频里具体说了什么）")

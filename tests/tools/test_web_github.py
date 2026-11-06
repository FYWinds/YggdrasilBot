"""The GitHub adapter of ``read_url``: files, folders, repositories, profiles, issues and pull requests."""

import time

import httpx
import pytest

from tests.support.routes import Routes
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web import Extracted
from yggdrasil.tools.web.adapters import github


def _paths(routes: Routes) -> list[str]:
    return [f"{r.url.host}{r.url.path}" for r in routes.seen]


async def test_github_blob_maps_to_raw() -> None:
    routes = Routes({"raw.githubusercontent.com": httpx.Response(200, text="print('hi')\n")})
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/o/r/blob/main/src/app.py"))
    assert [str(r.url) for r in routes.seen] == ["https://raw.githubusercontent.com/o/r/main/src/app.py"]
    assert isinstance(result, Extracted)
    assert result.text == "print('hi')"


async def test_github_non_repo_pages_use_generic_path() -> None:
    routes = Routes()
    async with routes.client() as client:
        for url in ("https://github.com/o/r/pull/1/files", "https://github.com/advisories/GHSA-9v8h-qqgm-4m35"):
            assert await github.page(client, httpx.URL(url)) is None
    assert routes.seen == []


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
    routes = Routes(
        {
            # The ref "feature/x" has a slash: the first guess (ref "feature") is not a branch.
            "https://api.github.com/repos/o/r/contents/x/pkg": no_ref,
            "https://api.github.com/repos/o/r/contents/pkg": contents,
            "https://raw.example.com/readme": httpx.Response(200, text="Package docs\n"),
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/o/r/tree/feature/x/pkg"))
    assert isinstance(result, Extracted)
    assert result.final_url == "https://github.com/o/r/tree/feature/x/pkg"
    assert result.text == (
        "仓库：o/r（feature/x）\n目录：pkg\n\n内容（3 项）：\nsub/\napp.py（2.0 KB）\nREADME.md（9 B）\n\n"
        "读取文件：https://github.com/o/r/blob/feature/x/pkg/<文件名>\n\nREADME.md：\n\nPackage docs"
    )


async def test_github_tree_of_a_missing_folder_raises() -> None:
    routes = Routes({"https://api.github.com/repos/o/r/contents/nope": httpx.Response(404, json={})})
    async with routes.client() as client:
        with pytest.raises(ToolError, match="没有这个目录"):
            await github.page(client, httpx.URL("https://github.com/o/r/tree/main/nope"))


async def test_github_repo_without_readme_lists_the_top_level_files() -> None:
    files = [{"name": "install.sh", "type": "file", "size": 688}, {"name": "docs", "type": "dir", "size": 0}]
    routes = Routes(
        {
            "https://api.github.com/repos/o/r": httpx.Response(200, json={"full_name": "o/r", "language": "Shell"}),
            "https://raw.githubusercontent.com/o/r/HEAD/README.md": httpx.Response(404),
            "https://api.github.com/repos/o/r/readme": httpx.Response(404, json={"message": "Not Found"}),
            "https://api.github.com/repos/o/r/contents": httpx.Response(200, json=files),
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/o/r"))
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
    routes = Routes(
        {
            "https://api.github.com/users/someone": httpx.Response(200, json=user),
            "https://api.github.com/users/someone/repos": repos,
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/someone?tab=repositories&sort=updated"))
    assert isinstance(result, Extracted)
    assert (result.title, result.final_url) == ("GitHub 用户 someone", "https://github.com/someone")
    assert result.text == (
        "GitHub 用户：someone\n公开仓库 2 · 关注者 7 · 关注 0 · 注册于 2024-07-01\n\n最近推送的仓库：\n"
        "- tool（fork） · Rust · ★ 5 · 推送于 2026-10-19\n"
        "- notes（已归档） · ★ 0 · 推送于 2025-01-02\n  Personal notes"
    )


async def test_github_org_overview_adds_the_profile_readme() -> None:
    org = {"login": "acme", "type": "Organization", "name": "Acme", "bio": "Tools", "blog": "https://example.com"}
    routes = Routes(
        {
            "https://api.github.com/users/acme": httpx.Response(200, json=org),
            "https://api.github.com/users/acme/repos": httpx.Response(200, json=[]),
            "https://raw.githubusercontent.com/acme/.github/HEAD/profile/README.md": httpx.Response(
                200, text="# Hi from Acme\n"
            ),
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/orgs/acme"))
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
    routes = Routes(
        {
            "https://api.github.com/repos/o/r/issues/7": httpx.Response(200, json=ISSUE),
            "https://api.github.com/repos/o/r/issues/7/comments": httpx.Response(200, json=ISSUE_COMMENTS),
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL("https://github.com/o/r/issues/7"))
    assert isinstance(result, Extracted)
    assert result.title == "Crash on start · Issue #7 · o/r"
    assert result.text == (
        "Issue #7 · 已关闭（不计划处理）\n"
        "作者：reporter · 创建于 2026-10-25 18:02（北京时间） · 关闭于 2026-10-26 08:00（北京时间）\n"
        "标签：bug、wontfix\n\n正文：\n\nIt crashes.\n\nSteps: run it.\n\n评论（2 条）：\n\n"
        "【maint（维护者） · 2026-10-25 20:00（北京时间）】\nWorks for me.\n\n"
        "【spammer · 2026-10-25 21:00（北京时间）】\n（已被折叠：spam）"
    )


def test_github_body_with_unclosed_comments_is_read_in_linear_time() -> None:
    # 64k characters of "<!--" that never close: the lazy comment regex used to scan to the end from each of them.
    tail = "<!--" * 16_384
    issue = {**ISSUE, "body": f"<!-- template -->\r\nIt crashes.\r\n{tail}", "comments": 0}
    started = time.perf_counter()
    text = github.format_github_issue(issue, [])
    assert time.perf_counter() - started < 1  # linear: milliseconds; the quadratic scan took seconds
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
    routes = Routes(
        {
            # An issue link to a PR: the issues API says so, the pulls API has the rest.
            "https://api.github.com/repos/o/r/issues/9": httpx.Response(
                200, json={**PULL, "pull_request": {"url": "u"}}
            ),
            "https://api.github.com/repos/o/r/pulls/9": httpx.Response(200, json=PULL),
            "https://api.github.com/repos/o/r/issues/9/comments": httpx.Response(200, json=PULL_COMMENTS),
            "https://api.github.com/repos/o/r/pulls/9/reviews": httpx.Response(200, json=REVIEWS),
        }
    )
    async with routes.client() as client:
        result = await github.page(client, httpx.URL(f"https://github.com/o/r/{path}"))
    assert isinstance(result, Extracted)
    assert result.title == "Add provider · PR #9 · o/r"
    assert result.text == (
        "PR #9 · 已关闭，未合并 · 草稿\n"
        "提示：dev 说「Moved to #12」，已转移或被取代：https://github.com/o/r/pull/12\n"
        "作者：dev · 创建于 2026-10-24 22:51（北京时间） · 关闭于 2026-10-25 02:59（北京时间）\n"
        "分支：dev:feature → o:main\n改动：3 个提交 · 76 个文件 · +6494 −7\n\n正文：\n\nAdds a provider.\n\n"
        "评论（3 条；另有 2 条代码行内审查评论未列出）：\n\n"
        "【ci[bot]（机器人） · 审查 · 2026-10-24 23:00（北京时间）】\nTests pass.\n\n"
        "【lead（维护者） · 审查：批准 · 2026-10-25 00:00（北京时间）】\n\n"
        "【dev（作者、维护者） · 2026-10-25 03:00（北京时间）】\nMoved to #12"
    )
    assert "api.github.com/repos/o/r/pulls/9/comments" not in _paths(routes)  # inline review threads stay out


def test_github_merged_pull_request_state() -> None:
    merged = {**PULL, "draft": False, "merged_at": "2026-10-28T12:00:00Z", "merged_by": {"login": "lead"}}
    assert github.format_github_pull(merged, [], []).startswith(
        "PR #9 · 已合并，2026-10-28 20:00（北京时间），合并者 lead\n"
    )


async def test_github_pull_link_to_an_issue_number_is_redirected() -> None:
    async with Routes({"https://api.github.com/repos/o/r/pulls/5": httpx.Response(404, json={})}).client() as client:
        target = await github.page(client, httpx.URL("https://github.com/o/r/pull/5"))
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
    async with Routes({"api.github.com": limited}).client() as client:
        assert await github.page(client, httpx.URL(url)) is None

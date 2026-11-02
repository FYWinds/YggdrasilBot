"""GitHub: files, folders, repositories, profiles, issues and pull requests from the API."""

import re

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import cn_datetime, optional_get, optional_json, raw_file
from yggdrasil.tools.web.model import MAX_HTML_BYTES, MB, AdapterResult, Extracted

_GITHUB_RESERVED = frozenset(
    "about advisories apps codespaces collections copilot dashboard enterprise events explore features issues login "
    "marketplace new notifications organizations orgs pricing pulls readme search security settings solutions "
    "sponsors team topics trending users".split()
)
# Unauthenticated: 60 requests an hour per IP. Over the limit, adapters fall back to the generic path.
_GITHUB_API = "https://api.github.com"
_GITHUB_PAGE_SIZE = 100
_GITHUB_USER_REPOS = 30
# Branch names may contain "/" and tree URLs do not say where the ref ends: at most this many splits are tried.
_GITHUB_REF_TRIES = 3
_GITHUB_MAINTAINERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_GITHUB_CLOSE_REASONS = {"completed": "已完成", "not_planned": "不计划处理", "duplicate": "重复"}
_GITHUB_REVIEW_STATES = {
    "APPROVED": "审查：批准",
    "CHANGES_REQUESTED": "审查：要求修改",
    "COMMENTED": "审查",
    "DISMISSED": "审查（已撤销）",
}
# "Moved to #123", "superseded by <PR link>", "迁移到 #12": the thread points at its successor.
_GITHUB_SUCCESSOR_RE = re.compile(
    r"(?:\b(?:moved|migrated|continued|reopened|recreated)\s+(?:to|in|as|at)|\b(?:superseded|replaced)\s+by"
    r"|\bin\s+favou?r\s+of|\bduplicate\s+of|(?:迁移|移|转)(?:到|至))"
    r"\s*(#\d+|https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+)",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


async def page(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    segments = [s for s in url.path.split("/") if s]
    if not segments:
        return None
    if segments[0].lower() == "orgs" and len(segments) >= 2:
        if segments[2:] not in ([], ["repositories"]):
            return None
        return await _github_user(client, segments[1], readme=len(segments) == 2)
    if segments[0].lower() in _GITHUB_RESERVED:
        return None
    if len(segments) == 1:
        tab = url.params.get("tab", "")
        if tab not in ("", "overview", "repositories"):
            return None  # stars, followers, ...: generic HTML extraction
        return await _github_user(client, segments[0], readme=tab != "repositories")
    owner, repo = segments[0], segments[1].removesuffix(".git")
    kind, rest = (segments[2], segments[3:]) if len(segments) > 2 else ("", [])
    if kind == "blob" and len(rest) >= 2:
        return await _github_blob(client, owner, repo, rest[0], "/".join(rest[1:]))
    if kind == "tree" and len(rest) >= 2:
        return await _github_tree(client, owner, repo, rest)
    if kind in ("issues", "pull") and len(rest) == 1 and rest[0].isdigit():
        thread = _github_issue if kind == "issues" else _github_pull
        return await thread(client, owner, repo, int(rest[0]))
    if kind and not (kind == "tree" and len(rest) == 1):
        return None  # commits, actions, PR diffs, discussions, ...: generic HTML extraction handles them
    return await _github_repo(client, owner, repo, rest[0] if rest else None)


async def _github_api(client: httpx.AsyncClient, path: str, **params: str | int) -> httpx.Response:
    return await client.get(f"{_GITHUB_API}{path}", params=params, headers={"Accept": "application/vnd.github+json"})


async def _github_list(client: httpx.AsyncClient, path: str, **params: str | int) -> list[dict] | None:
    """A secondary API list (comments, reviews, repos, files); ``None`` when it could not be read."""
    items = await optional_json(_github_api(client, path, **params), "github api")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None


async def _github_blob(client: httpx.AsyncClient, owner: str, repo: str, ref: str, path: str) -> AdapterResult:
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    return await raw_file(client, raw, f"{owner}/{repo}: {path}", "github")


async def _github_tree(client: httpx.AsyncClient, owner: str, repo: str, rest: list[str]) -> AdapterResult:
    """A folder page (``rest`` is ``[ref, *path]``): its entries from the contents API, then its README."""
    for cut in range(1, min(len(rest), _GITHUB_REF_TRIES) + 1):
        ref, path = "/".join(rest[:cut]), "/".join(rest[cut:])
        if not path:
            return await _github_repo(client, owner, repo, ref)
        resp = await _github_api(client, f"/repos/{owner}/{repo}/contents/{path}", ref=ref)
        if resp.status_code == 404 and "No commit found for the ref" in resp.text:
            continue  # the ref goes on past this "/"
        if resp.status_code == 404:
            raise ToolError("GitHub 上没有这个目录，或仓库是私有的")
        if resp.status_code != 200:
            return None  # rate limited: the generic page still shows the folder README
        entries = resp.json()
        if isinstance(entries, dict):
            return await _github_blob(client, owner, repo, ref, path)  # a file, not a folder
        return await _github_dir(client, owner, repo, ref, path, [e for e in entries if isinstance(e, dict)])
    raise ToolError("GitHub 仓库里没有这个分支")


async def _github_dir(
    client: httpx.AsyncClient, owner: str, repo: str, ref: str, path: str, entries: list[dict]
) -> Extracted:
    lines = [
        f"仓库：{owner}/{repo}（{ref}）",
        f"目录：{path}",
        "",
        f"内容（{len(entries)} 项）：",
        *_github_entry_lines(entries),
        "",
        f"读取文件：https://github.com/{owner}/{repo}/blob/{ref}/{path}/<文件名>",
    ]
    readmes = (e for e in entries if e.get("type") == "file" and str(e.get("name")).lower().startswith("readme"))
    readme = next(readmes, {})
    if readme.get("download_url") and int(readme.get("size") or 0) <= MAX_HTML_BYTES:
        resp = await optional_get(client.get(str(readme["download_url"])), "github readme")
        if resp is not None and (text := resp.text.strip()):
            lines += ["", f"{readme['name']}：", "", text]
    return Extracted(
        f"https://github.com/{owner}/{repo}/tree/{ref}/{path}", f"{owner}/{repo}: {path}", "\n".join(lines), "github"
    )


def _github_entry_lines(entries: list[dict]) -> list[str]:
    """One line per contents-API entry, folders first as on GitHub."""
    return [
        f"{e.get('name')}/" if e.get("type") == "dir" else f"{e.get('name')}（{_fmt_size(int(e.get('size') or 0))}）"
        for e in sorted(entries, key=lambda e: e.get("type") != "dir")
    ]


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < MB:
        return f"{size / 1024:.1f} KB"
    return f"{size / MB:.1f} MB"


async def _github_repo(client: httpx.AsyncClient, owner: str, repo: str, ref: str | None) -> AdapterResult:
    api = f"/repos/{owner}/{repo}"
    info_resp = await _github_api(client, api)
    if info_resp.status_code == 404:
        raise ToolError("GitHub 仓库不存在或是私有仓库")
    info = info_resp.json() if info_resp.status_code == 200 else {}
    readme = ""
    raw = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{ref or 'HEAD'}/README.md")
    if raw.status_code == 200:
        readme = raw.text
    else:
        params = {"ref": ref} if ref else None
        api_readme = await client.get(
            f"{_GITHUB_API}{api}/readme", params=params, headers={"Accept": "application/vnd.github.raw"}
        )
        if api_readme.status_code == 200:
            readme = api_readme.text
    if not info and not readme:
        return None
    files = None if readme.strip() else await _github_list(client, f"{api}/contents", **({"ref": ref} if ref else {}))
    full_name = info.get("full_name") or f"{owner}/{repo}"
    lines = [f"仓库：{full_name}"]
    if description := info.get("description"):
        lines.append(f"简介：{description}")
    if homepage := info.get("homepage"):
        lines.append(f"主页：{homepage}")
    if info:
        facts = [
            f"语言 {info.get('language') or '未知'}",
            f"★ {info.get('stargazers_count', 0)}",
            f"Fork {info.get('forks_count', 0)}",
            f"开放 issue {info.get('open_issues_count', 0)}",
        ]
        lines.append(" · ".join(facts))
        if (license_ := info.get("license")) and license_.get("spdx_id"):
            lines.append(f"许可证：{license_['spdx_id']}")
        if topics := info.get("topics"):
            lines.append("主题：" + "、".join(topics))
        if pushed := info.get("pushed_at"):
            lines.append(f"最近推送：{pushed[:10]}")
        if info.get("archived"):
            lines.append("（仓库已归档）")
    if readme.strip():
        lines += ["", "README：", "", readme.strip()]
    elif files:
        lines += ["", f"仓库没有 README。根目录（{len(files)} 项）：", *_github_entry_lines(files)]
    return Extracted(f"https://github.com/{full_name}", full_name, "\n".join(lines), "github")


async def _github_user(client: httpx.AsyncClient, name: str, *, readme: bool) -> AdapterResult:
    """A user or organization page: profile, recently pushed repos and (overview only) the profile README."""
    resp = await _github_api(client, f"/users/{name}")
    if resp.status_code != 200:
        return None  # not an account (some other GitHub page), or rate limited: generic path
    user = resp.json()
    login = str(user.get("login") or name)
    org = user.get("type") == "Organization"
    repos = await _github_list(client, f"/users/{login}/repos", sort="pushed", per_page=_GITHUB_USER_REPOS)
    profile = ""
    if readme:
        home = f"{login}/.github/HEAD/profile" if org else f"{login}/{login}/HEAD"
        page = await optional_get(client.get(f"https://raw.githubusercontent.com/{home}/README.md"), "github profile")
        profile = page.text if page is not None else ""
    return Extracted(
        f"https://github.com/{login}",
        f"GitHub {'组织' if org else '用户'} {login}",
        format_github_user(user, repos, profile),
        "github",
    )


def format_github_user(user: dict, repos: list[dict] | None, readme: str) -> str:
    org = user.get("type") == "Organization"
    head = f"GitHub {'组织' if org else '用户'}：{user.get('login', '')}"
    lines = [f"{head}（{user['name']}）" if user.get("name") else head]
    if bio := str(user.get("bio") or "").strip():
        lines.append(f"简介：{bio}")
    twitter = user.get("twitter_username")
    contacts = [
        f"{label} {value}"
        for label, value in (
            ("公司", user.get("company")),
            ("位置", user.get("location")),
            ("网站", user.get("blog")),
            ("X", f"@{twitter}" if twitter else None),
        )
        if value
    ]
    if contacts:
        lines.append(" · ".join(contacts))
    counts = [f"公开仓库 {user.get('public_repos', 0)}", f"关注者 {user.get('followers', 0)}"]
    if not org:
        counts.append(f"关注 {user.get('following', 0)}")
    if created := user.get("created_at"):
        counts.append(f"注册于 {str(created)[:10]}")
    lines.append(" · ".join(counts))
    if repos is None:
        lines += ["", "（仓库列表没能读取）"]
    elif repos:
        lines += ["", "最近推送的仓库："]
        for repo in repos:
            tags = [tag for tag, on in (("fork", repo.get("fork")), ("已归档", repo.get("archived"))) if on]
            facts = [f"{repo.get('name')}（{'，'.join(tags)}）" if tags else str(repo.get("name"))]
            if language := repo.get("language"):
                facts.append(str(language))
            facts.append(f"★ {repo.get('stargazers_count', 0)}")
            if pushed := repo.get("pushed_at"):
                facts.append(f"推送于 {str(pushed)[:10]}")
            lines.append("- " + " · ".join(facts))
            if description := str(repo.get("description") or "").strip():
                lines.append(f"  {description}")
    if readme.strip():
        lines += ["", "主页 README：", "", readme.strip()]
    return "\n".join(lines)


async def _github_issue(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> AdapterResult:
    resp = await _github_api(client, f"/repos/{owner}/{repo}/issues/{number}")
    if resp.status_code in (404, 410):
        raise ToolError("GitHub 上没有这个 issue 或 PR（可能已删除），或仓库是私有的")
    if resp.status_code != 200:
        return None  # rate limited: the generic page is noisy but readable
    issue = resp.json()
    if issue.get("pull_request"):
        return await _github_pull(client, owner, repo, number)
    return Extracted(
        final_url=str(issue.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}"),
        title=f"{issue.get('title', '')} · Issue #{number} · {owner}/{repo}",
        text=format_github_issue(issue, await _github_comments(client, owner, repo, issue)),
        via="github",
    )


async def _github_pull(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> AdapterResult:
    resp = await _github_api(client, f"/repos/{owner}/{repo}/pulls/{number}")
    if resp.status_code == 404:
        return f"https://github.com/{owner}/{repo}/issues/{number}"  # an issue number: GitHub redirects too
    if resp.status_code != 200:
        return None
    pull = resp.json()
    comments = await _github_comments(client, owner, repo, pull)
    reviews = await _github_list(client, f"/repos/{owner}/{repo}/pulls/{number}/reviews", per_page=_GITHUB_PAGE_SIZE)
    return Extracted(
        final_url=str(pull.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"),
        title=f"{pull.get('title', '')} · PR #{number} · {owner}/{repo}",
        text=format_github_pull(pull, comments, reviews),
        via="github",
    )


async def _github_comments(client: httpx.AsyncClient, owner: str, repo: str, item: dict) -> list[dict] | None:
    """The conversation comments of an issue or PR (the first page)."""
    if not item.get("comments"):
        return []
    path = f"/repos/{owner}/{repo}/issues/{item.get('number')}/comments"
    return await _github_list(client, path, per_page=_GITHUB_PAGE_SIZE)


def format_github_issue(issue: dict, comments: list[dict] | None) -> str:
    if issue.get("state") == "closed":
        reason = _GITHUB_CLOSE_REASONS.get(str(issue.get("state_reason")))
        state = f"已关闭（{reason}）" if reason else "已关闭"
    else:
        state = "开放中"
    return _github_thread(issue, f"Issue #{issue.get('number')} · {state}", [], comments, [])


def format_github_pull(pull: dict, comments: list[dict] | None, reviews: list[dict] | None) -> str:
    """The PR's state, branches and size, its description and conversation: comments and review summaries, not
    the inline code review threads."""
    if pull.get("merged_at"):
        merger = f"，合并者 {_gh_login(pull['merged_by'])}" if pull.get("merged_by") else ""
        state = f"已合并（{cn_datetime(pull['merged_at'])}{merger}）"
    elif pull.get("state") == "closed":
        state = "已关闭，未合并"
    else:
        state = "开放中"
    if pull.get("draft"):
        state += " · 草稿"
    head, base = pull.get("head") or {}, pull.get("base") or {}
    facts = [
        f"分支：{head.get('label', '?')} → {base.get('label', '?')}",
        f"改动：{pull.get('commits', 0)} 个提交 · {pull.get('changed_files', 0)} 个文件 · "
        f"+{pull.get('additions', 0)} −{pull.get('deletions', 0)}",
    ]
    inline = int(pull.get("review_comments") or 0)
    return _github_thread(pull, f"PR #{pull.get('number')} · {state}", facts, comments, reviews, inline)


def _github_thread(
    item: dict, status: str, facts: list[str], comments: list[dict] | None, reviews: list[dict] | None, inline: int = 0
) -> str:
    """An issue or PR: status, people, labels, body, then comments and reviews in time order."""
    author = _gh_login(item.get("user"))
    lines = [status, *_github_successors(item, [*(comments or ()), *(reviews or ())])]
    people = [f"作者：{author}", f"创建于 {cn_datetime(item.get('created_at'))}"]
    if item.get("closed_at") and not item.get("merged_at"):
        people.append(f"关闭于 {cn_datetime(item['closed_at'])}")
    lines += [" · ".join(people), *facts]
    if labels := [str(label.get("name")) for label in item.get("labels") or () if isinstance(label, dict)]:
        lines.append("标签：" + "、".join(labels))
    if assignees := [_gh_login(a) for a in item.get("assignees") or ()]:
        lines.append("指派给：" + "、".join(assignees))
    if milestone := (item.get("milestone") or {}).get("title"):
        lines.append(f"里程碑：{milestone}")
    body = _gh_markdown(item.get("body"))
    lines += ["", "正文：", "", body] if body else ["", "（没有正文）"]
    entries = [(str(c.get("created_at") or ""), _gh_who(c, author), _gh_comment_body(c)) for c in comments or ()]
    for review in reviews or ():
        state = _GITHUB_REVIEW_STATES.get(str(review.get("state")))
        text = _gh_markdown(review.get("body"))
        if state is None or (review.get("state") == "COMMENTED" and not text):
            continue  # pending reviews, and the empty wrappers of inline comments
        entries.append((str(review.get("submitted_at") or ""), f"{_gh_who(review, author)} · {state}", text))
    entries.sort(key=lambda entry: entry[0])
    notes = []
    if comments is None:
        notes.append("评论没能读取")
    elif (total := int(item.get("comments") or 0)) > len(comments):
        notes.append(f"共 {total} 条评论，只列出前 {len(comments)} 条")
    if inline:
        notes.append(f"另有 {inline} 条代码行内审查评论未列出")
    if entries or notes:
        lines += ["", f"评论（{len(entries)} 条{''.join(f'；{n}' for n in notes)}）："]
        for at, who, text in entries:
            lines += ["", f"【{who} · {cn_datetime(at)}】", *([text] if text else [])]
    return "\n".join(lines)


def _github_successors(item: dict, replies: list[dict]) -> list[str]:
    """Notes for "Moved to #123" / "superseded by ..." in the body or replies, with the target's link."""
    repo_url = str(item.get("html_url") or "").rsplit("/", 2)[0]
    kind = "pull" if item.get("head") else "issues"
    found: dict[str, str] = {}
    for entry in (item, *replies):
        for match in _GITHUB_SUCCESSOR_RE.finditer(_gh_markdown(entry.get("body"))):
            target = match.group(1)
            link = f"{repo_url}/{kind}/{target[1:]}" if target.startswith("#") else target
            who = _gh_login(entry.get("user"))
            found.setdefault(link, f"提示：{who} 说「{match.group(0)}」，已转移或被取代：{link}")
    return list(found.values())[:3]


def _gh_markdown(body: object) -> str:
    """A Markdown body without template comments (``<!-- -->``) and CRLFs.

    Comments are only looked for up to the last ``-->``. Past it no ``<!--`` can close, and the lazy regex would scan
    from each of them to the end of the text (quadratic: seconds for a 64k body). An unclosed ``<!--`` stays as text.
    """
    text = str(body or "")
    end = text.rfind("-->")
    if end >= 0:
        text = _HTML_COMMENT_RE.sub("", text[: end + 3]) + text[end + 3 :]
    return text.replace("\r\n", "\n").strip()


def _gh_comment_body(comment: dict) -> str:
    if isinstance(minimized := comment.get("minimized"), dict):
        return f"（已被折叠：{minimized.get('reason') or '隐藏'}）"
    return _gh_markdown(comment.get("body"))


def _gh_who(entry: dict, author: str) -> str:
    """A comment's author with the badges GitHub shows (author, maintainer, bot)."""
    login = _gh_login(entry.get("user"))
    badges = (
        ("作者", login == author),
        ("维护者", entry.get("author_association") in _GITHUB_MAINTAINERS),
        ("机器人", (entry.get("user") or {}).get("type") == "Bot"),
    )
    tags = [tag for tag, on in badges if on]
    return f"{login}（{'、'.join(tags)}）" if tags else login


def _gh_login(user: object) -> str:
    return str(user.get("login") or "未知") if isinstance(user, dict) else "未知"

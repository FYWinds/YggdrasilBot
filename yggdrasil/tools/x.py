"""X (formerly Twitter): read one post, search X News stories and recent posts.

Reading tries FxTwitter (free, keyless, third party) first and the official X API v2 second; searching only uses
the official API. The API is pay per use (per post / story returned, deduplicated per UTC day) with an app bearer
token stored as the ``x`` search provider; every API request counts against that row's monthly limit.
"""

import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.runtime import SearchProviderInfo
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.search import reserve_quota

KIND = "x"
DEFAULT_BASE_URL = "https://api.x.com"
FX_API = "https://api.fxtwitter.com"
# FxTwitter sits behind Cloudflare, which challenges httpx's default UA but serves a bot that names itself.
USER_AGENT = "yggdrasil-bot/0.1 (+https://github.com/FYWinds/YggdrasilBot)"
_CN_TZ = timezone(timedelta(hours=8))
HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
        "fxtwitter.com",
        "www.fxtwitter.com",
        "fixupx.com",
        "www.fixupx.com",
        "vxtwitter.com",
        "www.vxtwitter.com",
        "fixvx.com",
    }
)
NEWS_MAX_HOURS = 720
POSTS_MAX_HOURS = 168  # recent search covers the last 7 days
_STATUS_RE = re.compile(r"^/(?:i/web|[A-Za-z0-9_]{1,15})/status(?:es)?/(\d{1,20})(?:/|$)")
_ID_RE = re.compile(r"^\d{1,20}$")
_TIMEOUT = 15.0
_CACHE_TTL = 600.0
_CACHE_SIZE = 64
_NEWS_POST_LINKS = 3
# Wire names follow the data dictionary (tweet.*); the OpenAPI spec's post.* names are accepted too (probed 2026-10).
_POST_PARAMS = {
    "tweet.fields": "created_at,public_metrics,note_tweet,article,entities,referenced_tweets,attachments,lang,"
    "in_reply_to_user_id",
    "expansions": "author_id,attachments.media_keys,attachments.poll_ids,referenced_tweets.id,"
    "referenced_tweets.id.author_id,in_reply_to_user_id",
    "media.fields": "type,url,preview_image_url,alt_text,duration_ms",
    "user.fields": "username,name",
    "poll.fields": "options,voting_status,end_datetime",
}
_SEARCH_PARAMS = {
    "tweet.fields": "created_at,public_metrics,note_tweet,entities,lang",
    "expansions": "author_id",
    "user.fields": "username,name",
}
_NEWS_FIELDS = "name,summary,category,updated_at,contexts,cluster_posts_results"

type Reserve = Callable[[SearchProviderInfo], Awaitable[bool]]


@dataclass(slots=True)
class Media:
    kind: Literal["photo", "video", "gif"]
    url: str
    """The photo, or the video's preview image."""
    alt: str = ""
    duration_s: float | None = None


@dataclass(slots=True)
class Post:
    id: str
    author: str
    handle: str
    text: str
    created_at: datetime | None = None
    likes: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    replies: int | None = None
    views: int | None = None
    media: list[Media] = field(default_factory=list)
    quote: "Post | None" = None
    quote_gone: str = ""
    """Why the quoted post can't be shown (deleted, private, ...), when it can't."""
    replying_to: str = ""
    poll: list[tuple[str, int]] = field(default_factory=list)
    community_note: str = ""
    article_title: str = ""
    article_text: str = ""
    thread: list["Post"] = field(default_factory=list)
    """Other posts of the same conversation, oldest first (FxTwitter only)."""
    via: str = ""

    @property
    def url(self) -> str:
        return f"https://x.com/{self.handle or 'i'}/status/{self.id}"

    def images(self) -> list[Media]:
        """Media of the post and its quoted post, then of each thread post: the numbering ``format_post`` uses."""
        return [m for p in (self, *self.thread) for m in (p.media + (p.quote.media if p.quote else []))]


@dataclass(slots=True)
class NewsStory:
    id: str
    name: str
    summary: str
    category: str = ""
    updated_at: datetime | None = None
    topics: list[str] = field(default_factory=list)
    post_ids: list[str] = field(default_factory=list)


class PostGone(ToolError):
    """The post is deleted, protected or otherwise unavailable (a definite answer, not a failure)."""


_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()


# --------------------------------------------------------------------------- providers, ids


def provider(providers: Sequence[SearchProviderInfo]) -> SearchProviderInfo | None:
    """The highest-priority enabled ``x`` provider with a key, or ``None``."""
    return next((p for p in providers if p.kind == KIND and p.enabled and p.api_key), None)


def post_id(ref: str) -> str | None:
    """The post id in an X status URL (any of :data:`HOSTS`) or a bare numeric id; ``None`` otherwise."""
    ref = ref.strip()
    if _ID_RE.match(ref):
        return ref
    try:
        url = httpx.URL(ref if "://" in ref else f"https://{ref}")
    except httpx.InvalidURL:
        return None
    if url.host.lower() not in HOSTS:
        return None
    match = _STATUS_RE.match(url.path)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- reading


async def read_post(
    pid: str,
    api: SearchProviderInfo | None,
    *,
    client: httpx.AsyncClient | None = None,
    reserve: Reserve | None = None,
) -> Post:
    """FxTwitter first, then the official API when ``api`` is configured. Raises :class:`ToolError`."""
    key = f"post:{pid}"
    if (cached := _cache_get(key)) is not None:
        return cached
    async with _client(client) as c:
        fx = await _fetch_fx(c, pid)
        if isinstance(fx, Post):
            return _cache_put(key, fx)
        if fx is not None:
            raise PostGone(fx)  # a definite answer: the API would only confirm it
        if api is None:
            raise ToolError("FxTwitter 暂时读不到这条帖子，也没有配置 X API")
        if not await (reserve or reserve_quota)(api):
            raise ToolError("FxTwitter 暂时读不到这条帖子，X API 本月额度已用完")
        post = parse_api_post(await api_get(api, f"/2/tweets/{pid}", _POST_PARAMS, client=c))
    return _cache_put(key, post)


async def _fetch_fx(client: httpx.AsyncClient, pid: str) -> Post | str | None:
    """The post from FxTwitter, a reason string when it says the post is unavailable, ``None`` on failure."""
    try:
        resp = await client.get(f"{FX_API}/2/thread/{pid}", headers={"User-Agent": USER_AGENT}, timeout=_TIMEOUT)
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        LOGGER.warning("fxtwitter failed for {}: {!r}", pid, exc)
        return None
    try:
        return parse_fx_thread(payload)
    except PostGone as gone:
        return str(gone)


def parse_fx_thread(payload: object) -> Post | None:
    """A ``/2/thread/{id}`` payload as a post with its conversation; ``None`` when it is not a usable answer.

    Raises :class:`PostGone` when FxTwitter says the post is unavailable (404 or a tombstone).
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if payload.get("code") == 404:
        raise PostGone("帖子不存在或已删除")
    if not isinstance(status, dict):
        return None
    if status.get("type") == "tombstone":
        raise PostGone(_gone_reason(status.get("reason")))
    post = _fx_post(status, depth=1)
    if post is None:
        return None
    thread = payload.get("thread") or []
    post.thread = [p for s in thread if isinstance(s, dict) and (p := _fx_post(s, depth=0)) and p.id != post.id]
    post.via = "fxtwitter"
    return post


def _fx_post(s: dict[str, Any], *, depth: int) -> Post | None:
    pid, text = s.get("id"), s.get("text")
    if not isinstance(pid, str) or not isinstance(text, str) or s.get("type") == "tombstone":
        return None
    author = a if isinstance(a := s.get("author"), dict) else {}
    ts = s.get("created_timestamp")
    post = Post(
        id=pid,
        author=str(author.get("name") or ""),
        handle=str(author.get("screen_name") or ""),
        text=text,
        created_at=datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None,
        likes=_int(s.get("likes")),
        reposts=_int(s.get("reposts")),
        quotes=_int(s.get("quotes")),
        replies=_int(s.get("replies")),
        views=_int(s.get("views")),
        media=_fx_media(s.get("media")),
    )
    if isinstance(reply := s.get("replying_to"), dict):
        post.replying_to = str(reply.get("screen_name") or "")
    if isinstance(note := s.get("community_note"), dict):
        post.community_note = str(note.get("text") or "")
    if isinstance(poll := s.get("poll"), dict):
        post.poll = [
            (str(c.get("label", "")), _int(c.get("count")) or 0)
            for c in poll.get("choices") or []
            if isinstance(c, dict)
        ]
    if isinstance(article := s.get("article"), dict):
        post.article_title = str(article.get("title") or "")
        blocks = (article.get("content") or {}).get("blocks") or []
        body = "\n\n".join(t for b in blocks if isinstance(b, dict) and (t := str(b.get("text") or "").strip()))
        post.article_text = body or str(article.get("preview_text") or "")
    if depth > 0 and isinstance(quote := s.get("quote"), dict):
        if quote.get("type") == "tombstone":
            post.quote_gone = _gone_reason(quote.get("reason"))
        else:
            post.quote = _fx_post(quote, depth=depth - 1)
    return post


def _fx_media(media: object) -> list[Media]:
    if not isinstance(media, dict):
        return []
    items = media.get("all") or [*(media.get("photos") or []), *(media.get("videos") or [])]
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        kind = m.get("type")
        if kind == "photo" and isinstance(url := m.get("url"), str):
            out.append(Media("photo", _large(url), str(m.get("altText") or "")))
        elif kind in ("video", "gif") and isinstance(thumb := m.get("thumbnail_url"), str):
            duration = m.get("duration")
            out.append(
                Media(
                    "gif" if kind == "gif" else "video",
                    thumb,
                    duration_s=float(duration) if isinstance(duration, int | float) else None,
                )
            )
    return out


def parse_api_post(payload: object) -> Post:
    """A ``GET /2/tweets/{id}`` payload (with :data:`_POST_PARAMS`). Raises :class:`PostGone` / :class:`ToolError`."""
    if not isinstance(payload, dict):
        raise ToolError("X API 返回了无法识别的内容")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PostGone(_api_error(payload) or "帖子不存在、已删除或不公开")
    includes = payload.get("includes") or {}
    users = _by(includes.get("users"), "id")
    media = _by(includes.get("media"), "media_key")
    polls = _by(includes.get("polls"), "id")
    tweets = _by(includes.get("tweets"), "id")
    post = _api_post(data, users, media, polls)
    if reply_to := users.get(str(data.get("in_reply_to_user_id") or "")):
        post.replying_to = str(reply_to.get("username") or "")
    for ref in data.get("referenced_tweets") or []:
        if isinstance(ref, dict) and ref.get("type") == "quoted":
            quoted = tweets.get(str(ref.get("id")))
            if quoted is None:
                post.quote_gone = "不可用"
            else:
                post.quote = _api_post(quoted, users, media, polls)
    post.via = "x-api"
    return post


def _api_post(
    t: dict[str, Any],
    users: Mapping[str, dict[str, Any]],
    media: Mapping[str, dict[str, Any]],
    polls: Mapping[str, dict[str, Any]],
) -> Post:
    author = users.get(str(t.get("author_id") or "")) or {}
    note = t.get("note_tweet") if isinstance(t.get("note_tweet"), dict) else None
    text = _expand_links(str((note or t).get("text") or ""), (note or t).get("entities"))
    metrics = t.get("public_metrics") or {}
    post = Post(
        id=str(t.get("id") or ""),
        author=str(author.get("name") or ""),
        handle=str(author.get("username") or ""),
        text=text,
        created_at=_iso(t.get("created_at")),
        likes=_int(metrics.get("like_count")),
        reposts=_int(metrics.get("retweet_count", metrics.get("repost_count"))),
        quotes=_int(metrics.get("quote_count")),
        replies=_int(metrics.get("reply_count")),
        views=_int(metrics.get("impression_count")),
    )
    attachments = t.get("attachments") or {}
    for key in attachments.get("media_keys") or []:
        m = media.get(str(key)) or {}
        kind = m.get("type")
        if kind == "photo" and isinstance(url := m.get("url"), str):
            post.media.append(Media("photo", _large(url), str(m.get("alt_text") or "")))
        elif kind in ("video", "animated_gif") and isinstance(preview := m.get("preview_image_url"), str):
            ms = m.get("duration_ms")
            post.media.append(
                Media(
                    "gif" if kind == "animated_gif" else "video",
                    preview,
                    duration_s=ms / 1000 if isinstance(ms, int | float) else None,
                )
            )
    for poll_id in attachments.get("poll_ids") or []:
        options = (polls.get(str(poll_id)) or {}).get("options") or []
        post.poll = [(str(o.get("label", "")), _int(o.get("votes")) or 0) for o in options if isinstance(o, dict)]
    if isinstance(article := t.get("article"), dict):
        post.article_title = str(article.get("title") or "")
    return post


def _expand_links(text: str, entities: object) -> str:
    """Replace t.co links with their targets; links to the post's own media are dropped."""
    urls = entities.get("urls") if isinstance(entities, dict) else None
    for u in urls or []:
        if not isinstance(u, dict) or not isinstance(short := u.get("url"), str):
            continue
        target = "" if u.get("media_key") else str(u.get("expanded_url") or short)
        text = text.replace(short, target)
    return text.strip()


# --------------------------------------------------------------------------- searching (official API only)


async def search_news(
    api: SearchProviderInfo,
    query: str,
    *,
    hours: int,
    n: int,
    client: httpx.AsyncClient | None = None,
    reserve: Reserve | None = None,
) -> list[NewsStory]:
    """X News stories (Grok-written summaries of what posts are saying) matching ``query``.

    ``reserve`` charges the provider's monthly limit before a request (cached answers are free); ``None`` skips it.
    """
    hours = max(1, min(hours, NEWS_MAX_HOURS))
    params = {"query": query, "max_results": str(max(1, min(n, 100))), "max_age_hours": str(hours)}
    key = f"news:{query}:{hours}:{params['max_results']}"
    if (cached := _cache_get(key)) is not None:
        return cached
    await _charge(api, reserve)
    async with _client(client) as c:
        payload = await api_get(api, "/2/news/search", {**params, "news.fields": _NEWS_FIELDS}, client=c)
    return _cache_put(key, parse_news(payload))


def parse_news(payload: object) -> list[NewsStory]:
    data = payload.get("data") if isinstance(payload, dict) else None
    stories = []
    for d in data or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        contexts = c if isinstance(c := d.get("contexts"), dict) else {}
        stories.append(
            NewsStory(
                id=str(d.get("id") or ""),
                name=str(d["name"]),
                summary=str(d.get("summary") or ""),
                category=str(d.get("category") or ""),
                updated_at=_iso(d.get("updated_at")),
                topics=[str(t) for t in contexts.get("topics") or []],
                post_ids=[
                    str(p["post_id"])
                    for p in d.get("cluster_posts_results") or []
                    if isinstance(p, dict) and p.get("post_id")
                ],
            )
        )
    return stories


async def search_posts(
    api: SearchProviderInfo,
    query: str,
    *,
    hours: int,
    client: httpx.AsyncClient | None = None,
    reserve: Reserve | None = None,
) -> list[Post]:
    """Up to 10 original posts (no reposts) from the last ``hours`` matching ``query``, by relevance."""
    if "is:retweet" not in query:
        query = f"{query} -is:retweet"
    params = {"query": query, "max_results": "10", "sort_order": "relevancy", **_SEARCH_PARAMS}
    if hours < POSTS_MAX_HOURS:
        params["start_time"] = (datetime.now(UTC) - timedelta(hours=max(1, hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = f"posts:{query}:{min(hours, POSTS_MAX_HOURS)}"
    if (cached := _cache_get(key)) is not None:
        return cached
    await _charge(api, reserve)
    async with _client(client) as c:
        payload = await api_get(api, "/2/tweets/search/recent", params, client=c)
    return _cache_put(key, parse_search_posts(payload))


async def _charge(api: SearchProviderInfo, reserve: Reserve | None) -> None:
    if reserve is not None and not await reserve(api):
        raise ToolError("X API 本月额度已用完")


def parse_search_posts(payload: object) -> list[Post]:
    if not isinstance(payload, dict):
        return []
    users = _by((payload.get("includes") or {}).get("users"), "id")
    return [_api_post(t, users, {}, {}) for t in payload.get("data") or [] if isinstance(t, dict) and t.get("id")]


async def api_get(
    api: SearchProviderInfo, path: str, params: dict[str, str], *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """One authenticated GET against the X API; HTTP and API errors become :class:`ToolError`."""
    headers = {"Authorization": f"Bearer {api.api_key}", "User-Agent": USER_AGENT}
    try:
        async with _client(client) as c:
            resp = await c.get(api.base_url.rstrip("/") + path, params=params, headers=headers, timeout=_TIMEOUT)
    except httpx.TimeoutException as exc:
        raise ToolError("X API 超时") from exc
    except httpx.HTTPError as exc:
        raise ToolError("X API 无法访问") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    detail = _api_error(payload) if isinstance(payload, dict) else ""
    if resp.status_code == 429:
        raise ToolError("X API 请求太频繁，稍后再试")
    if resp.status_code in (401, 403):
        raise ToolError(f"X API 拒绝了请求（HTTP {resp.status_code}{'：' + detail if detail else ''}）")
    if resp.status_code == 402:
        raise ToolError("X API 余额不足，需要在 X 开发者后台充值")
    if resp.status_code != 200 or not isinstance(payload, dict):
        raise ToolError(f"X API 出错（HTTP {resp.status_code}{'：' + detail if detail else ''}）")
    return payload


# --------------------------------------------------------------------------- formatting


def format_post(post: Post) -> str:
    """The post as labelled text; images are numbered in :meth:`Post.images` order (图 1, 图 2, ...).

    The other posts of the conversation (usually the author's own thread) are shown in full, with their media: a
    thread is often the whole point of the link, and they can't be read one by one.
    """
    lines = _post_lines(post, image_no=1)
    image_no = 1 + len(post.media) + (len(post.quote.media) if post.quote else 0)
    if post.thread:
        lines += ["", f"同一对话里的其他帖子（共 {len(post.thread)} 条，按时间）："]
    for k, p in enumerate(post.thread, 1):
        when = f" {p.created_at.astimezone(_CN_TZ):%m-%d %H:%M}（北京时间）" if p.created_at else ""
        lines += ["", f"【第 {k} 条】{who(p)}{when}", *_body_lines(p, image_no=image_no)]
        image_no += len(p.media) + (len(p.quote.media) if p.quote else 0)
    return "\n".join(lines)


def _post_lines(post: Post, *, image_no: int) -> list[str]:
    lines = [f"作者：{who(post)}"]
    if post.created_at:
        lines.append(f"发布时间：{post.created_at.astimezone(_CN_TZ):%Y-%m-%d %H:%M}（北京时间）")
    if stats := _stats(post):
        lines.append(f"数据：{stats}")
    if post.replying_to:
        lines.append(f"回复给：@{post.replying_to}")
    return [*lines, "", *_body_lines(post, image_no=image_no)]


def _body_lines(post: Post, *, image_no: int) -> list[str]:
    """Text, article, poll, media, note and quoted post: everything below the header."""
    lines = [post.text or "（没有文字）"]

    if post.article_title or post.article_text:
        lines += ["", f"文章《{post.article_title}》：" if post.article_title else "文章：", post.article_text]
    if post.poll:
        total = sum(votes for _, votes in post.poll)
        lines += ["", f"投票（共 {_count(total)} 票）："]
        lines += [f"- {label}：{_count(votes)}" for label, votes in post.poll]
    if post.media:
        lines += ["", "附件："]
        for i, m in enumerate(post.media, image_no):
            lines.append(f"- 图 {i}：{_media_label(m)}")
    if post.community_note:
        lines += ["", f"社区笔记：{post.community_note}"]
    if post.quote:
        quoted = "\n".join(_post_lines(post.quote, image_no=image_no + len(post.media))).split("\n")
        lines += ["", "引用的帖子：", *(f"> {line}" if line else ">" for line in quoted)]
    elif post.quote_gone:
        lines += ["", f"引用的帖子：{post.quote_gone}"]
    return lines


def format_news(stories: Sequence[NewsStory]) -> str:
    blocks = []
    for i, s in enumerate(stories, 1):
        head = f"{i}. {s.name}"
        if s.updated_at:
            head += f"（更新于北京时间 {s.updated_at.astimezone(_CN_TZ):%m-%d %H:%M}）"
        block = [head, s.summary]
        if s.topics:
            block.append("话题：" + "、".join(s.topics))
        if s.post_ids:
            links = " ".join(f"https://x.com/i/status/{pid}" for pid in s.post_ids[:_NEWS_POST_LINKS])
            block.append(f"相关帖子（共 {len(s.post_ids)} 条）：{links}")
        blocks.append("\n".join(block))
    note = (
        "（以下是 X 用 Grok 根据帖子写的新闻摘要，可能有误；要核实细节就用 read_x 读相关帖子。"
        "摘要正文里提到的时间没有注明时区，多半是事发地或 UTC 时间，不是北京时间）"
    )
    return note + "\n\n" + "\n\n".join(blocks)


def format_posts(posts: Sequence[Post]) -> str:
    blocks = []
    for i, p in enumerate(posts, 1):
        head = f"{i}. {who(p)}"
        if p.created_at:
            head += f" {p.created_at.astimezone(_CN_TZ):%m-%d %H:%M}（北京时间）"
        if stats := _stats(p):
            head += f" · {stats}"
        blocks.append(f"{head}\n{p.text}\n{p.url}")
    return "\n\n".join(blocks)


def who(post: Post) -> str:
    """``Name（@handle）`` for display."""
    if post.author and post.handle:
        return f"{post.author}（@{post.handle}）"
    return f"@{post.handle}" if post.handle else post.author or "未知作者"


def _stats(post: Post) -> str:
    counters = (
        ("赞", post.likes),
        ("转发", post.reposts),
        ("引用", post.quotes),
        ("回复", post.replies),
        ("浏览", post.views),
    )
    return " · ".join(f"{label} {_count(v)}" for label, v in counters if v is not None)


def _count(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def _media_label(m: Media) -> str:
    if m.kind == "photo":
        return f"图片（{m.alt}）" if m.alt else "图片"
    label = "GIF 动图的封面" if m.kind == "gif" else "视频封面"
    if m.duration_s:
        minutes, seconds = divmod(round(m.duration_s), 60)
        label += f"，时长 {minutes}:{seconds:02d}"
    return label


# --------------------------------------------------------------------------- helpers


def _client(client: httpx.AsyncClient | None) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """``async with`` over the caller's client (left open) or a fresh one."""
    return nullcontext(client) if client is not None else httpx.AsyncClient()


def _large(url: str) -> str:
    """pbs.twimg.com photos at ``name=large`` (at most 2048 px) instead of the original size."""
    u = httpx.URL(url)
    if u.host != "pbs.twimg.com":
        return url
    return str(u.copy_set_param("name", "large"))


def _gone_reason(reason: object) -> str:
    reasons = {
        "deleted": "帖子已删除",
        "suspended": "作者账号已被封禁",
        "private": "作者设为仅关注者可见",
        "blocked": "作者屏蔽了访问",
    }
    return reasons.get(str(reason), "帖子不可用")


def _api_error(payload: dict[str, Any]) -> str:
    """The first error's text from an X API problem response or ``errors`` array."""
    if detail := payload.get("detail") or payload.get("title"):
        return str(detail)[:200]
    for err in payload.get("errors") or []:
        if isinstance(err, dict) and (text := err.get("detail") or err.get("title") or err.get("message")):
            return str(text)[:200]
    return ""


def _by(items: object, key: str) -> dict[str, dict[str, Any]]:
    return {str(i[key]): i for i in items or [] if isinstance(i, dict) and key in i} if isinstance(items, list) else {}


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _cache_get(key: str) -> Any:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, value = entry
    if time.monotonic() - stored_at > _CACHE_TTL:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return value


def _cache_put[T](key: str, value: T) -> T:
    _cache[key] = (time.monotonic(), value)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return value

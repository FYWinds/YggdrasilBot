"""Bilibili: video facts from the public API, subtitles as a transcript, and b23.tv short links."""

import re
from datetime import UTC, datetime

import httpx

from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.web.adapters.common import (
    cn_count,
    cn_time,
    fmt_duration,
    no_transcript,
    optional_json,
    transcript,
)
from yggdrasil.tools.web.model import API_USER_AGENT, AdapterResult, Extracted

_BILI_VIDEO_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", re.IGNORECASE)
_BILI_GONE_CODES = frozenset({-404, 62002, 62004, 62012})


async def short_link(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    resp = await client.get(url, follow_redirects=False)
    location = resp.headers.get("location")
    if resp.is_redirect and location:
        return str(resp.url.join(location))
    return None


async def video(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    match = _BILI_VIDEO_RE.search(url.path)
    if match is None:
        return None
    vid = match.group(1)
    params = {"bvid": vid} if vid[:2].upper() == "BV" else {"aid": vid[2:]}
    resp = await client.get(
        "https://api.bilibili.com/x/web-interface/view", params=params, headers={"User-Agent": API_USER_AGENT}
    )
    try:
        payload = resp.json()
    except ValueError:
        return None
    code = payload.get("code")
    if code in _BILI_GONE_CODES:
        raise ToolError("B 站视频不存在、已删除或需要登录才能看")
    if code != 0 or not isinstance(payload.get("data"), dict):
        return None  # risk control etc.; the generic path may still get something
    data = payload["data"]
    bvid = data.get("bvid") or vid
    p = url.params.get("p", "")
    part = int(p) if p.isdigit() and 0 < int(p) <= len(data.get("pages") or ()) else 1
    return Extracted(
        final_url=f"https://www.bilibili.com/video/{bvid}" + (f"?p={part}" if part > 1 else ""),
        title=str(data.get("title", "")),
        text=f"{format_bilibili_video(data)}\n\n{await _bilibili_subtitles(client, data, part)}",
        via="bilibili",
    )


async def _bilibili_subtitles(client: httpx.AsyncClient, data: dict, part: int) -> str:
    """Subtitles of part ``part`` as a transcript, or a note saying why there are none.

    The player APIs hide subtitles from anonymous visitors (``need_login_subtitle``); the danmaku view API still
    lists them with their URLs, AI-generated ones included.
    """
    parts = data.get("pages") or []
    cid = parts[part - 1].get("cid") if 0 < part <= len(parts) else data.get("cid")
    view = await optional_json(
        client.get(
            "https://api.bilibili.com/x/v2/dm/view",
            params={"type": 1, "oid": cid, "pid": data.get("aid")},
            headers={"User-Agent": API_USER_AGENT},
        ),
        "bilibili subtitle list",
    )
    if not isinstance(view, dict) or view.get("code") != 0:
        return no_transcript("字幕读取失败")
    subtitle = (view.get("data") or {}).get("subtitle") or {}
    tracks = [t for t in subtitle.get("subtitles") or [] if isinstance(t, dict) and t.get("subtitle_url")]
    if not tracks:
        listed = (data.get("subtitle") or {}).get("list")
        return no_transcript("这个视频有字幕，但 B 站要求登录才能读取" if listed else "这个视频没有字幕")
    track = min(tracks, key=_bilibili_track_rank)
    src = str(track["subtitle_url"])
    src = f"https:{src}" if src.startswith("//") else src.replace("http://", "https://", 1)
    body = await optional_json(client.get(src, headers={"User-Agent": API_USER_AGENT}), "bilibili subtitles")
    cues = body.get("body") if isinstance(body, dict) else None
    text = transcript(
        (float(c.get("from") or 0), str(c.get("content") or "")) for c in cues or () if isinstance(c, dict)
    )
    if not text:
        return no_transcript("字幕读取失败")
    label = str(track.get("lan_doc") or track.get("lan") or "")
    return f"字幕（{f'P{part} · ' if len(parts) > 1 else ''}{label}）：\n\n{text}"


def _bilibili_track_rank(track: dict) -> tuple[bool, bool, bool]:
    """Uploaded subtitles before AI ones, AI transcriptions before AI translations, then Chinese first."""
    lang = str(track.get("lan", "")).removeprefix("ai-")
    return track.get("type") != 0, track.get("ai_type") != 0, not lang.startswith("zh")


def format_bilibili_video(data: dict) -> str:
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    lines = [f"UP主：{owner.get('name', '未知')}"]
    if pubdate := data.get("pubdate"):
        lines.append(f"发布时间：{cn_time(datetime.fromtimestamp(pubdate, UTC))}")
    if duration := data.get("duration"):
        lines.append(f"时长：{fmt_duration(int(duration))}")
    if tname := data.get("tname") or data.get("tname_v2"):
        lines.append(f"分区：{tname}")
    counters = (
        ("播放", "view"),
        ("弹幕", "danmaku"),
        ("点赞", "like"),
        ("投币", "coin"),
        ("收藏", "favorite"),
        ("分享", "share"),
        ("评论", "reply"),
    )
    lines.append("数据：" + " · ".join(f"{label} {cn_count(stat[key])}" for label, key in counters if key in stat))
    desc = str(data.get("desc") or "").strip()
    if desc and desc != "-":
        lines += ["", "简介：", desc]
    parts = data.get("pages") or []
    if len(parts) > 1:
        lines += ["", f"分P（共 {len(parts)} 个）："]
        lines += [
            f"P{p.get('page', i)} {p.get('part', '')}（{fmt_duration(int(p.get('duration', 0)))}）"
            for i, p in enumerate(parts[:50], 1)
        ]
    return "\n".join(lines)

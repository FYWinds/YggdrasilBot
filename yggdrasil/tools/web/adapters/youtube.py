"""YouTube: what the watch page shows, and the captions as a transcript."""

import json
import re
from collections.abc import Iterator
from typing import Any

import httpx

from yggdrasil.tools.web.adapters.common import no_transcript, optional_json, transcript
from yggdrasil.tools.web.adapters.registry import site_adapter
from yggdrasil.tools.web.model import AdapterResult, Extracted

_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_ID_PATHS = frozenset({"shorts", "live", "embed"})
_YT_INITIAL_DATA = "var ytInitialData = "
_YT_MAX_CHAPTERS = 100
# Caption URLs from the web player need a proof-of-origin token; the Android app's player response carries URLs that
# work without one (no login, no API key). Datacenter IPs may still get a "confirm you're not a bot" answer.
_YT_PLAYER = "https://www.youtube.com/youtubei/v1/player"
_YT_ANDROID_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30, "hl": "zh-CN"}
_YT_ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"


@site_adapter("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be")
async def video(client: httpx.AsyncClient, url: httpx.URL) -> AdapterResult:
    vid = _youtube_video_id(url)
    if vid is None:
        return None
    resp = await client.get(f"https://www.youtube.com/watch?v={vid}&hl=zh-CN")
    if resp.status_code != 200:
        return None
    page = parse_youtube_watch(resp.text, f"https://www.youtube.com/watch?v={vid}")
    if page is not None:
        page.text += f"\n\n{await _youtube_transcript(client, vid)}"
    return page


async def _youtube_transcript(client: httpx.AsyncClient, vid: str) -> str:
    """The captions as a transcript, or a note saying why there are none."""
    player = await optional_json(
        client.post(
            _YT_PLAYER,
            params={"prettyPrint": "false"},
            json={"context": {"client": _YT_ANDROID_CLIENT}, "videoId": vid},
            headers={"User-Agent": _YT_ANDROID_UA},
        ),
        "youtube player",
    )
    if not isinstance(player, dict):
        return no_transcript("字幕读取失败")
    status = player.get("playabilityStatus") or {}
    if status.get("status") != "OK":
        return no_transcript(f"YouTube 不给字幕（{status.get('reason') or status.get('status')}）")
    renderer = (player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    tracks = [t for t in renderer.get("captionTracks") or () if isinstance(t, dict) and t.get("baseUrl")]
    if not tracks:
        return no_transcript("这个视频没有字幕")
    track = min(tracks, key=_youtube_track_rank)
    src = httpx.URL(str(track["baseUrl"])).copy_set_param("fmt", "json3")
    captions = await optional_json(client.get(src), "youtube captions")
    events = captions.get("events") if isinstance(captions, dict) else None
    text = transcript(
        (int(e.get("tStartMs") or 0) / 1000, "".join(str(s.get("utf8", "")) for s in e.get("segs") or ()))
        for e in events or ()
        if isinstance(e, dict)
    )
    if not text:
        return no_transcript("字幕读取失败")
    return f"字幕（{_yt_text(track.get('name')) or track.get('languageCode')}）：\n\n{text}"


def _youtube_track_rank(track: dict) -> tuple[bool, bool, bool]:
    """Uploaded captions before auto-generated ones, then Chinese, then English."""
    lang = str(track.get("languageCode", ""))
    return track.get("kind") == "asr", not lang.startswith("zh"), not lang.startswith("en")


def _youtube_video_id(url: httpx.URL) -> str | None:
    segments = [s for s in url.path.split("/") if s]
    if url.host.lower() == "youtu.be":
        vid = segments[0] if segments else ""
    elif url.path == "/watch":
        vid = url.params.get("v", "")
    else:
        vid = segments[1] if len(segments) >= 2 and segments[0] in _YT_ID_PATHS else ""
    return vid if _YT_ID_RE.match(vid) else None


def parse_youtube_watch(html: str, final_url: str) -> Extracted | None:
    """Metadata, description and chapters from a watch page's ``ytInitialData``, or ``None`` without them.

    Datacenter IPs get the page but not the player data (a "confirm you're not a bot" check);
    ``ytInitialData`` still carries everything shown below the video.
    """
    start = html.find(_YT_INITIAL_DATA)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start + len(_YT_INITIAL_DATA))
    except ValueError:
        return None
    primary = next(_find_key(data, "videoPrimaryInfoRenderer"), None) or {}
    title = _yt_text(primary.get("title"))
    if not title:
        return None  # unavailable video, or a layout we do not know: the generic path takes over
    secondary = next(_find_key(data, "videoSecondaryInfoRenderer"), None) or {}
    owner = (secondary.get("owner") or {}).get("videoOwnerRenderer") or {}
    lines = []
    if channel := _yt_text(owner.get("title")):
        subscribers = _yt_text(owner.get("subscriberCountText"))
        lines.append(f"频道：{channel}（{subscribers}）" if subscribers else f"频道：{channel}")
    if date := _yt_text(primary.get("dateText")):
        lines.append(f"发布时间：{date}")
    if views := _yt_text(((primary.get("viewCount") or {}).get("videoViewCountRenderer") or {}).get("viewCount")):
        lines.append(f"播放：{views}")
    if description := _yt_text(secondary.get("attributedDescription")).strip():
        lines += ["", "简介：", description]
    # The same markers appear in several panels; keep the first occurrence of each.
    chapters = list(
        dict.fromkeys(
            f"{_yt_text(m.get('timeDescription'))} {_yt_text(m.get('title'))}".strip()
            for m in _find_key(data, "macroMarkersListItemRenderer")
            if isinstance(m, dict)
        )
    )
    if chapters:
        lines += ["", f"章节（共 {len(chapters)} 个）：", *chapters[:_YT_MAX_CHAPTERS]]
    return Extracted(final_url, title, "\n".join(lines), "youtube")


def _find_key(node: object, key: str) -> Iterator[Any]:
    """Every value stored under ``key`` anywhere in a JSON tree, depth first."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)


def _yt_text(node: object) -> str:
    """Text of a YouTube text object: ``simpleText``, ``runs`` or ``content`` (attributed text)."""
    if not isinstance(node, dict):
        return ""
    if isinstance(text := node.get("simpleText") or node.get("content"), str):
        return text
    return "".join(str(r.get("text", "")) for r in node.get("runs") or () if isinstance(r, dict))

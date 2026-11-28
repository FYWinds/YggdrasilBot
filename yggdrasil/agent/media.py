"""Downloading and preparing message attachments (images, files) for the model."""

import asyncio
import io
import itertools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence, ImageStat
from pydantic_ai import BinaryContent

from yggdrasil.agent.deps import Handle
from yggdrasil.core.log import LOGGER
from yggdrasil.qq.client import QQ
from yggdrasil.qq.model import QQError
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import guarded_client
from yggdrasil.transcript.notes import TraceNotes, image_key
from yggdrasil.transcript.store import blob as stored_blob

IMAGE_MAX_DOWNLOAD = 20 * 1024 * 1024
_IMAGE_MAX_SIDE = 1568  # Claude downsizes anything larger; also keeps payloads well under the 5 MB cap
_IMAGE_MAX_BYTES = 3 * 1024 * 1024

# Animated images: models only read the first frame, so they get one sheet of sampled frames instead.
_SHEET_MAX_FRAMES = 16
_SAMPLE_STEP_MS = 100  # look at the animation every 0.1 s
_HOLD_MS = 300  # a frame with only a small change is still kept after this long
_CHANGE = 0.06  # visible change: mean difference (0-1) of the most-changed 1/16 of the picture
_MIN_CHANGE = 0.015  # below this a frame counts as unchanged (dithering, compression noise)
_MAX_DECODE_FRAMES = 600
_DEFAULT_FRAME_MS = 100  # browsers play GIF frames of 0-10 ms at 100 ms
_SHEET_GAP = 4
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_UNSAFE_NAME = re.compile(r"[^\w.\-]+")


class MediaTooLarge(ToolError):
    """An attachment is larger than the caller allows."""


async def download(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
    """Download ``url``; ``guarded`` routes it through the SSRF guard (URLs that came from third-party content)."""
    client = guarded_client(timeout=60) if guarded else httpx.AsyncClient(timeout=60, follow_redirects=True)
    async with client, client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise MediaTooLarge(f"文件太大（超过 {max_bytes // 1024 // 1024}MB）")
            chunks.append(chunk)
    return b"".join(chunks)


async def media_bytes(
    qq: QQ, group_id: int | None, user_id: int, h: Handle, max_bytes: int, *, made: Mapping[str, bytes] | None = None
) -> tuple[bytes, str]:
    """Bytes + file name ("file" when there is none) of an image/file handle: an image the bot made this run from
    ``made`` (``Deps.made_images``), a picture it made or sent earlier from our blob store, else the segment's
    ``url``. A QQ attachment without a url (files never carry one), or whose url has expired (QQ signs media links
    with an rkey that lasts hours), gets a working link from ``QQ.media_url`` once.

    Handles registered by web tools carry a ``source`` (``x``, ...): their URLs come from third-party content and
    are fetched through the SSRF guard, never refreshed. QQ attachment URLs come from the protocol side and are
    fetched directly.
    """
    data = h.data
    name = str(data.get("file_name") or "file")
    if sha := data.get("blob"):
        if made and (own := made.get(str(sha))) is not None:
            return own, name
        if (kept := await stored_blob(str(sha))) is not None:
            return kept.data, name
        if data.get("made"):  # its run's transcript wasn't kept (privacy setting, failed save): nothing else has it
            raise ToolError(f"{h.key} 是之前生成的图，已经找不到了；需要的话重新生成")
    url = str(data.get("url") or "")
    if data.get("source"):
        return await download(url, max_bytes, guarded=True), name
    if url:
        try:
            return await download(url, max_bytes), name
        except httpx.HTTPStatusError as e:
            if not 400 <= e.response.status_code < 500:
                raise
    what = "图片" if h.kind == "image" else "文件"
    gone = f"{what}链接已过期，也没能重新获取（可能太久了）" if url else f"拿不到{what}的下载地址"
    try:
        fresh = await qq.media_url({"type": h.kind, "data": data}, group_id=group_id, user_id=user_id)
    except QQError as e:
        raise ToolError(gone) from e
    if not fresh or fresh == url:
        raise ToolError(gone)
    return await download(fresh, max_bytes), name


def is_image(blob: bytes) -> bool:
    """Whether Pillow recognises the bytes as a picture (reads the header only)."""
    try:
        Image.open(io.BytesIO(blob)).close()
    except Image.UnidentifiedImageError:
        return False
    except (OSError, ValueError, Image.DecompressionBombError):
        return True  # recognised, just not decodable here; fit_image leaves it to the model API
    return True


def looks_like_image(h: Handle) -> bool:
    """An image segment, or a file named like a picture (photos are often sent as group files to keep quality)."""
    if h.kind == "image":
        return True
    name = str(h.data.get("file_name") or "")
    return h.kind == "file" and PurePath(name).suffix.lower() in _IMAGE_SUFFIXES


def media_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob[:3] == b"GIF":
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def safe_name(name: str, fallback: str) -> str:
    """A plain file name: no directories, no shell-hostile characters."""
    base = _UNSAFE_NAME.sub("_", PurePosixPath(name.replace("\\", "/")).name).strip("._")[:120]
    return base or fallback


def file_name(ref: str, h: Handle, blob: bytes) -> str:
    """What to call the bytes of attachment ``ref`` in a workspace: its own file name, else one made from the ref
    (``img-3.png``)."""
    fallback = re.sub(r"\W+", "-", ref).strip("-") or "file"
    if is_image(blob):
        fallback += "." + media_type(blob).removeprefix("image/").replace("jpeg", "jpg")
    return safe_name(str(h.data.get("file_name") or ""), fallback)


def fit_image(blob: bytes) -> bytes:
    """Downscale/re-encode images that are too large or in a format models don't take (BMP, ...)."""
    try:
        img = Image.open(io.BytesIO(blob))
        too_big = max(img.size) > _IMAGE_MAX_SIDE or len(blob) > _IMAGE_MAX_BYTES
        if not too_big and img.format in ("PNG", "JPEG", "GIF", "WEBP"):
            return blob
        img.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
        out = io.BytesIO()
        if img.mode in ("RGBA", "LA", "P"):
            img.convert("RGBA").save(out, "PNG", optimize=True)
        else:
            img.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return blob  # not decodable here; let the model API judge


_SEND_MAX_BYTES = 600 * 1024


def compact_image(blob: bytes) -> bytes:
    """A large opaque picture as JPEG before it is sent: QQ recompresses it anyway, and a 1 MB PNG uploads slowly
    enough to time out the send. Transparent and animated images stay as they are."""
    if len(blob) <= _SEND_MAX_BYTES:
        return blob
    try:
        with Image.open(io.BytesIO(blob)) as img:
            if getattr(img, "is_animated", False):
                return blob
            if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
                lowest_alpha = img.convert("RGBA").getchannel("A").getextrema()[0]
                if isinstance(lowest_alpha, int | float) and lowest_alpha < 255:
                    return blob
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    except (OSError, ValueError, Image.DecompressionBombError):
        return blob
    data = out.getvalue()
    return data if len(data) < len(blob) else blob


@dataclass(frozen=True, slots=True)
class ModelImage:
    """An image ready for a vision model; an animation arrives as one sheet of sampled frames."""

    data: bytes
    times: tuple[float, ...] = ()  # seconds of each sampled frame; empty for a still image
    duration: float = 0.0

    @property
    def note(self) -> str:
        """Tells the model how to read a frame sheet; empty for a still image."""
        if not self.times:
            return ""
        return (
            f"（动图，时长 {self.duration:.1f} 秒：按时间顺序抽了 {len(self.times)} 帧拼成一张，"
            "从左到右、从上到下看，每格左上角是帧号和秒数；这是同一个动画，不是多张图）"
        )

    @property
    def content(self) -> BinaryContent:
        return BinaryContent(data=self.data, media_type=media_type(self.data))


def model_image(blob: bytes) -> ModelImage:
    """What a vision model should see: an animation as a frame sheet, anything else through :func:`fit_image`."""
    try:
        with Image.open(io.BytesIO(blob)) as img:
            if getattr(img, "is_animated", False) and (sheet := _frame_sheet(img)) is not None:
                return sheet
    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    return ModelImage(fit_image(blob))


def _flatten(frame: Image.Image) -> Image.Image:
    rgba = frame.convert("RGBA")
    out = Image.new("RGB", rgba.size, "white")
    out.paste(rgba, mask=rgba.getchannel("A"))
    return out


def _change(a: Image.Image, b: Image.Image) -> float:
    """Mean difference (0-1, over RGB) of the most-changed cell of a 4x4 grid, so a blink counts as much as a big
    move and a colour change counts even at the same brightness."""
    diff = ImageChops.difference(a, b)
    cells = ((x, y, x + 16, y + 16) for x in range(0, 64, 16) for y in range(0, 64, 16))
    return max(sum(ImageStat.Stat(diff.crop(cell)).mean) / (3 * 255) for cell in cells)


type _Sample = tuple[int, int, Image.Image]  # frame index, sample time in ms, 64x64 thumbnail


def _samples(img: Image.Image) -> tuple[list[_Sample], int]:
    """One sample per ``_SAMPLE_STEP_MS`` of playback (shorter frames may be skipped), and the duration in ms."""
    samples: list[_Sample] = []
    start = sample = 0
    for i, frame in enumerate(itertools.islice(ImageSequence.Iterator(img), _MAX_DECODE_FRAMES)):
        ms = int(frame.info.get("duration") or 0)
        end = start + (ms if ms > 10 else _DEFAULT_FRAME_MS)
        if sample < end:
            samples.append((i, sample, _flatten(frame).resize((64, 64), Image.Resampling.BILINEAR)))
            sample += (end - sample + _SAMPLE_STEP_MS - 1) // _SAMPLE_STEP_MS * _SAMPLE_STEP_MS
        start = end
    return samples, start


def _changed(samples: list[_Sample]) -> list[_Sample]:
    """Samples that changed visibly since the last kept one, or changed a little and ``_HOLD_MS`` has passed."""
    kept = [samples[0]]
    for s in samples[1:]:
        d = _change(s[2], kept[-1][2])
        if d >= _CHANGE or (d >= _MIN_CHANGE and s[1] - kept[-1][1] >= _HOLD_MS):
            kept.append(s)
    return kept


def _frame_sheet(img: Image.Image) -> ModelImage | None:
    """The frames where the picture changed; more than ``_SHEET_MAX_FRAMES`` are thinned evenly, so busy stretches
    keep more frames than calm ones. None when nothing moves (the first frame says it all)."""
    samples, duration = _samples(img)
    kept = _changed(samples) if samples else []
    if len(kept) < 2:
        return None
    if len(kept) > _SHEET_MAX_FRAMES:
        n = len(kept) - 1
        kept = [kept[round(k * n / (_SHEET_MAX_FRAMES - 1))] for k in range(_SHEET_MAX_FRAMES)]
    wanted = {s[0] for s in kept}
    sequence = itertools.islice(ImageSequence.Iterator(img), kept[-1][0] + 1)
    frames = [_flatten(f) for i, f in enumerate(sequence) if i in wanted]
    times = tuple(s[1] / 1000 for s in kept)
    return ModelImage(_tile(frames, times), times, duration / 1000)


def _tile(frames: list[Image.Image], times: tuple[float, ...]) -> bytes:
    w, h = frames[0].size
    n = len(frames)
    cols = max(1, min(n, round(math.sqrt(n * h / w))))
    rows = math.ceil(n / cols)
    room = _IMAGE_MAX_SIDE - _SHEET_GAP * (max(cols, rows) - 1)
    scale = min(1.0, room / (cols * w), room / (rows * h))
    cw, ch = max(1, int(w * scale)), max(1, int(h * scale))
    sheet = Image.new("RGB", (cols * cw + (cols - 1) * _SHEET_GAP, rows * ch + (rows - 1) * _SHEET_GAP), (64, 64, 64))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=max(10, min(cw, ch) // 9))
    for k, (frame, t) in enumerate(zip(frames, times, strict=True)):
        x, y = (k % cols) * (cw + _SHEET_GAP), (k // cols) * (ch + _SHEET_GAP)
        sheet.paste(frame.resize((cw, ch), Image.Resampling.LANCZOS) if scale < 1 else frame, (x, y))
        label = f"{k + 1} {t:.1f}s"
        box = draw.textbbox((x + 3, y + 2), label, font=font)
        draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=(0, 0, 0))
        draw.text((x + 3, y + 2), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "PNG", optimize=True)
    if out.tell() > _IMAGE_MAX_BYTES:
        out = io.BytesIO()
        sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


async def load_image(
    qq: QQ, group_id: int | None, user_id: int, h: Handle, *, made: Mapping[str, bytes] | None = None
) -> ModelImage:
    """An image handle, or a file handle whose content is a picture, ready for a vision model; ``made`` as for
    :func:`media_bytes`."""
    blob, name = await media_bytes(qq, group_id, user_id, h, IMAGE_MAX_DOWNLOAD, made=made)
    if not is_image(blob):
        # Bytes Pillow can't place (SVG, HEIC, a web page) would reach a vision model labelled as JPEG and fail the
        # whole run with a provider 400.
        raise ToolError(f"{name} 不是图片，用 read_file 读" if h.kind == "file" else f"{h.key} 不是能识别的图片格式")
    return await asyncio.to_thread(model_image, blob)


async def load_images(
    qq: QQ, group_id: int | None, user_id: int, handles: list[Handle]
) -> list[tuple[str, ModelImage]]:
    """(handle key, model-ready image) for each image that downloads; failures are left to view_image."""
    results = await asyncio.gather(*(load_image(qq, group_id, user_id, h) for h in handles), return_exceptions=True)
    loaded = []
    for h, result in zip(handles, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning("inline image {} failed: {!r}", h.key, result)
        else:
            loaded.append((h.key, result))
    return loaded


_KEEP_MAX = 12


async def keep_context_images(qq: QQ, group_id: int | None, user_id: int, notes: TraceNotes) -> None:
    """Keep the bytes of the pictures in the request's own messages (the trigger, the quoted message, what that
    replies to) for the console, which can only show a QQ link until it expires.

    The run loaded most of them for the model already; left are an ops run's, a route without vision's and those
    past the inline limit. Up to ``_KEEP_MAX``; one that no longer downloads is skipped.
    """
    context = notes.context
    if not notes.full or context is None:
        return
    wanted: dict[str, dict[str, Any]] = {}
    for message in (context.trigger, context.quoted, *context.chain):
        for segment in message.segments if message is not None else []:
            data = segment.get("data")
            # ``blob``: a picture the bot sent itself, kept when it was sent.
            if segment.get("type") != "image" or not isinstance(data, dict) or data.get("blob"):
                continue
            key = image_key(data)
            if key and key not in notes.image_keys:
                wanted.setdefault(key, data)
    handles = [Handle(key, "image", data) for key, data in list(wanted.items())[:_KEEP_MAX]]
    for key, image in await load_images(qq, group_id, user_id, handles):
        notes.image(image.data, image.content.media_type, segment=wanted[key])

"""Downloading and preparing message attachments (images, files) for the model."""

import asyncio
import base64
import io
import itertools
import math
from dataclasses import dataclass

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence, ImageStat
from pydantic_ai import BinaryContent

from yggdrasil.agent.deps import Handle
from yggdrasil.core.log import LOGGER
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import guarded_client

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


async def download(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
    """Download ``url``; ``guarded`` routes it through the SSRF guard (URLs that came from third-party content)."""
    client = guarded_client(timeout=60) if guarded else httpx.AsyncClient(timeout=60, follow_redirects=True)
    async with client, client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ToolError(f"文件太大（超过 {max_bytes // 1024 // 1024}MB）")
            chunks.append(chunk)
    return b"".join(chunks)


async def media_bytes(api: NapCatAPI, group_id: int | None, h: Handle, max_bytes: int) -> tuple[bytes, str]:
    """Bytes + filename of an image/file handle, via URL in the segment or NapCat's get_file.

    Handles registered by web tools carry a ``source`` (``x``, ...): their URLs come from third-party content and
    are fetched through the SSRF guard. QQ attachment URLs come from the protocol side and are fetched directly.
    """
    data = h.data
    name = str(data.get("file") or data.get("name") or "file")
    url = data.get("url")
    if h.kind == "file" and group_id and data.get("file_id"):
        try:
            got = await api.get_group_file_url(group_id=group_id, file_id=str(data["file_id"]), busid=data.get("busid"))
            url = got.url
        except Exception:
            url = None
    if not url and data.get("file_id"):
        info = await api.get_file(file_id=str(data["file_id"]))
        if info.base64:
            return base64.b64decode(info.base64), info.file_name or name
        url = info.url
        name = info.file_name or name
    if not url:
        raise ToolError("拿不到文件下载地址")
    try:
        return await download(url, max_bytes, guarded=bool(data.get("source"))), name
    except httpx.HTTPStatusError as e:
        # QQ image URLs carry an rkey that expires within hours; the protocol side re-signs it by file name.
        if h.kind != "image" or not data.get("file") or not 400 <= e.response.status_code < 500:
            raise
    try:
        fresh = (await api.get_image(file=str(data["file"]))).url
    except Exception as e:
        raise ToolError("图片链接已过期，也没能重新获取（可能太久了）") from e
    if not fresh or fresh == url:
        raise ToolError("图片链接已过期，也没能重新获取（可能太久了）")
    return await download(fresh, max_bytes), name


def media_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob[:3] == b"GIF":
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
    enough to time out the OneBot call. Transparent and animated images stay as they are."""
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


async def load_image(api: NapCatAPI, group_id: int | None, h: Handle) -> ModelImage:
    blob, _ = await media_bytes(api, group_id, h, IMAGE_MAX_DOWNLOAD)
    return await asyncio.to_thread(model_image, blob)


async def load_images(api: NapCatAPI, group_id: int | None, handles: list[Handle]) -> list[tuple[str, ModelImage]]:
    """(handle key, model-ready image) for each image that downloads; failures are left to view_image."""
    results = await asyncio.gather(*(load_image(api, group_id, h) for h in handles), return_exceptions=True)
    loaded = []
    for h, result in zip(handles, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning("inline image {} failed: {!r}", h.key, result)
        else:
            loaded.append((h.key, result))
    return loaded

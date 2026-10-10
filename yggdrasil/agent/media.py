"""Downloading and preparing message attachments (images, files) for the model."""

import asyncio
import base64
import io

import httpx
from PIL import Image

from yggdrasil.agent.deps import Handle
from yggdrasil.core.log import LOGGER
from yggdrasil.napcat.api import NapCatAPI
from yggdrasil.tools.errors import ToolError

IMAGE_MAX_DOWNLOAD = 20 * 1024 * 1024
_IMAGE_MAX_SIDE = 1568  # Claude downsizes anything larger; also keeps payloads well under the 5 MB cap
_IMAGE_MAX_BYTES = 3 * 1024 * 1024


async def download(url: str, max_bytes: int) -> bytes:
    async with (
        httpx.AsyncClient(timeout=60, follow_redirects=True) as client,
        client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp,
    ):
        resp.raise_for_status()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ToolError(f"文件太大（超过 {max_bytes // 1024 // 1024}MB）")
            chunks.append(chunk)
    return b"".join(chunks)


async def media_bytes(api: NapCatAPI, group_id: int | None, h: Handle, max_bytes: int) -> tuple[bytes, str]:
    """Bytes + filename of an image/file handle, via URL in the segment or NapCat's get_file."""
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
    return await download(url, max_bytes), name


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


async def load_image(api: NapCatAPI, group_id: int | None, h: Handle) -> bytes:
    blob, _ = await media_bytes(api, group_id, h, IMAGE_MAX_DOWNLOAD)
    return await asyncio.to_thread(fit_image, blob)


async def load_images(api: NapCatAPI, group_id: int | None, handles: list[Handle]) -> list[tuple[str, bytes]]:
    """(handle key, model-ready bytes) for each image that downloads; failures are left to view_image."""
    results = await asyncio.gather(*(load_image(api, group_id, h) for h in handles), return_exceptions=True)
    loaded = []
    for h, result in zip(handles, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning("inline image {} failed: {!r}", h.key, result)
        else:
            loaded.append((h.key, result))
    return loaded

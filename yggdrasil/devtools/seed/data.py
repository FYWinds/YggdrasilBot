"""Scope constants and synthetic media for the whole seed; ``NOW``, ``PERSONA`` and ``MEDIA`` are fixed at import."""

import base64
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from yggdrasil.settings import Settings

TECH, GAME = 10001, 10002
GROUPS = {TECH: "示例技术群", GAME: "示例游戏群"}
SELF = 10000  # the bot's QQ number in seed data
MEMBERS = {20001: "阿明", 20002: "小红", 20003: "老王", 20004: "Neko", 20005: "路人甲", 20006: "新来的"}
SUPERUSER = 20001
JOB_PREFIX = "seed-"
PROVIDER = "seed-example"
MODEL, FAST = "example/chat-large", "example/chat-fast"
ADMIN = "admin@example.com"
TRACE_PREFIX = "5eed"  # seeded invocations' trace ids start with it, so reset finds them (private ones too)
FLAG_PREFIX = "seed-"  # bot event request flags
PRIVATE_TEXT = "把示例游戏群每人每天的次数调到 120"
PERSONA = Settings().persona.name
NOW = datetime.now(UTC)


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest().upper()  # QQ's file name scheme, not security

    @property
    def url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _png(
    label: str, color: tuple[int, int, int], size: tuple[int, int] = (640, 400), bars: list[int] | None = None
) -> Media:
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle((10, 10, w - 10, h - 10), outline=(255, 255, 255), width=4)
    draw.text((32, 28), label, fill=(255, 255, 255), font=_font(34))
    for i, value in enumerate(bars or []):
        x = 60 + i * 110
        draw.rectangle((x, h - 40 - value, x + 70, h - 40), fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Media(buf.getvalue(), "image/png")


def _gif(label: str, colors: list[tuple[int, int, int]]) -> Media:
    frames = []
    for i, color in enumerate(colors):
        img = Image.new("RGB", (240, 240), color)
        draw = ImageDraw.Draw(img)
        draw.ellipse((40 + i * 10, 40, 200 - i * 10, 200), fill=(255, 255, 255))
        draw.text((70, 105), label, fill=color, font=_font(30))
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return Media(buf.getvalue(), "image/gif")


def _media() -> dict[str, Media]:
    return {
        "bench": _png("free-threading benchmark", (45, 85, 140), bars=[200, 190, 260, 300]),
        "map": _png("season 7 map", (40, 120, 70)),
        "render": _png("rendered markdown", (90, 60, 130), size=(800, 500)),
        "screenshot": _png("pelican site", (180, 90, 40), size=(960, 600)),
        "drawing": _png("pixel poster", (60, 150, 200), size=(640, 640)),
        "doge": _gif("doge", [(200, 160, 60), (220, 180, 80), (240, 200, 100)]),
        "cat": _png("cat", (120, 120, 120), size=(240, 240)),
        "happy": _png("happy", (230, 120, 150), size=(240, 240)),
    }


MEDIA = _media()


def mface_segment() -> dict[str, Any]:
    return {
        "type": "mface",
        "data": {
            "emoji_package_id": "230001",
            "emoji_id": "5f3c2e1a9b8d7c6e5f4a3b2c1d0e9f8a",
            "key": "seedkey0001",
            "summary": "[开心]",
            "url": MEDIA["happy"].url,
        },
    }


def broken_ref() -> str:
    return hashlib.md5(b"seed-broken-sticker").hexdigest().upper()


JOB_IDS = [f"{JOB_PREFIX}{s}" for s in ("train", "crawl", "site", "scrape", "live")]

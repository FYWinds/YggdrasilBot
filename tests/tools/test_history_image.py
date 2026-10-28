"""view_image on images from the chat history (``#<message id>``) and re-signing expired QQ image URLs."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete

from yggdrasil.agent import media
from yggdrasil.agent.deps import Deps, Handle, Handles
from yggdrasil.agent.tools import _history_image
from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.napcat.models import FileInfo
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

GROUP, OTHER = 990011, 990012


def _img(name: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": name, "url": f"https://img.example.com/{name}?rkey=old"}}


def _ctx(group_id: int = GROUP) -> RunContext[Deps]:
    deps = Deps(cast(Any, None), group_id, 2, Settings(), Handles(), "chat")
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


@pytest.fixture
async def ids() -> AsyncIterator[dict[str, int]]:
    async def cleanup() -> None:
        async with get_session() as s:
            await s.execute(delete(Message).where(Message.group_id.in_((GROUP, OTHER))))

    await cleanup()
    out: dict[str, int] = {}
    rows = {
        "two": (GROUP, [_img("a.png"), {"type": "text", "data": {"text": "看这个"}}, _img("b.png")], False),
        "text": (GROUP, [{"type": "text", "data": {"text": "没有图"}}], False),
        "recalled": (GROUP, [_img("c.png")], True),
        "other": (OTHER, [_img("d.png")], False),
    }
    async with get_session() as s:
        for key, (gid, segs, recalled) in rows.items():
            m = Message(
                sent_at=datetime.now(UTC),
                group_id=gid,
                user_id=3,
                qq_message_id=1,
                plain_text="",
                segments=segs,
                recalled=recalled,
            )
            s.add(m)
            await s.flush()
            out[key] = m.id
    yield out
    await cleanup()


async def test_history_image_picks_the_nth_image(ids: dict[str, int]) -> None:
    ctx = _ctx()
    first = await _history_image(ctx, f"#{ids['two']}")
    second = await _history_image(ctx, f"{ids['two']}/2")
    assert (first.kind, first.data["file"]) == ("image", "a.png")
    assert second.data["file"] == "b.png"
    # Registered like any attachment, so the model can keep using the img#N key.
    assert ctx.deps.handles.get(second.key) is second


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("other", "没有消息"),
        ("recalled", "没有消息"),
        ("text", "没有图片"),
        ("two/3", "只有 2 张图"),
        ("bogus", "不是"),
    ],
)
async def test_history_image_refusals(ids: dict[str, int], ref: str, why: str) -> None:
    key, _, n = ref.partition("/")
    target = f"#{ids[key]}" + (f"/{n}" if n else "") if key in ids else "img#9"
    with pytest.raises(ToolError, match=why):
        await _history_image(_ctx(), target)


class _Api:
    def __init__(self, fresh: str | None) -> None:
        self.fresh = fresh
        self.asked: list[str] = []

    async def get_image(self, *, file: str) -> FileInfo:
        self.asked.append(file)
        if self.fresh is None:
            raise RuntimeError("image not found in cache")
        return FileInfo(url=self.fresh)


def _expired(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def fake(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
        seen.append(url)
        if "rkey=old" in url:
            raise _expired(url)
        return b"png"

    monkeypatch.setattr(media, "download", fake)
    return seen


async def test_expired_image_url_is_resigned(downloads: list[str]) -> None:
    api = _Api("https://img.example.com/a.png?rkey=new")
    h = Handle("img#1", "image", _img("a.png")["data"])
    assert await media.media_bytes(cast(Any, api), GROUP, h, 1024) == (b"png", "a.png")
    assert api.asked == ["a.png"]
    assert downloads == ["https://img.example.com/a.png?rkey=old", "https://img.example.com/a.png?rkey=new"]


async def test_expired_image_without_fresh_url_explains(downloads: list[str]) -> None:
    h = Handle("img#1", "image", _img("a.png")["data"])
    with pytest.raises(ToolError, match="过期"):
        await media.media_bytes(cast(Any, _Api(None)), GROUP, h, 1024)


async def test_files_are_not_resigned(downloads: list[str]) -> None:
    h = Handle("file#1", "file", {"file": "a.pdf", "url": "https://files.example.com/a.pdf?rkey=old"})
    with pytest.raises(httpx.HTTPStatusError):
        await media.media_bytes(cast(Any, _Api("unused")), None, h, 1024)

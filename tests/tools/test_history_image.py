"""Tool refs into the chat history (``#<message id>[/<k>]``), resolved the same way by every tool, and re-signing
expired QQ image URLs."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete

from yggdrasil.agent import media, tools
from yggdrasil.agent.deps import Deps, Handle, Handles
from yggdrasil.agent.tools import _resolve
from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.napcat.models import FileInfo, ForwardMsg, MessageRecord, Segment, Sender
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError

GROUP, OTHER = 990011, 990012


def _img(name: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": name, "url": f"https://img.example.com/{name}?rkey=old"}}


def _ctx(group_id: int = GROUP, api: Any = None) -> RunContext[Deps]:
    deps = Deps(cast(Any, api), group_id, 2, Settings(), Handles(), "chat")
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
        # A forward as QQ hands it over: only its id, the nodes are fetched when it is opened.
        "forward": (GROUP, [{"type": "forward", "data": {"id": "res-1"}}], False),
        "link": (GROUP, [{"type": "text", "data": {"text": "看看 https://example.com/post 这个"}}], False),
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


async def test_a_message_ref_picks_the_kth_attachment_of_the_wanted_kind(ids: dict[str, int]) -> None:
    ctx = _ctx()
    first = await _resolve(ctx, f"#{ids['two']}", "image")
    second = await _resolve(ctx, f"#{ids['two']}/2", "image")
    assert (first.kind, first.data["file"]) == ("image", "a.png")
    assert second.data["file"] == "b.png"
    # Registered like any attachment, so the model can keep using the img#N key.
    assert ctx.deps.handles.get(second.key) is second
    assert (await _resolve(ctx, f"#{ids['forward']}", "forward")).data == {"id": "res-1"}
    # Links in the text count for the link kinds (read_url / read_x).
    assert (await _resolve(ctx, f"#{ids['link']}", "url", "card")).data == {"url": "https://example.com/post"}


async def test_a_handle_of_another_kind_says_what_it_is(ids: dict[str, int]) -> None:
    ctx = _ctx()
    image = await _resolve(ctx, f"#{ids['two']}", "image")
    with pytest.raises(ToolError, match="是图片，不是合并转发"):
        await _resolve(ctx, image.key, "forward")


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("other", "没有消息"),
        ("recalled", "没有消息"),
        ("text", "没有图片"),
        ("two/3", "只有 2 个图片"),
        ("img#9", "找不到 img#9"),
        # A bare number is not a message ref (read_x takes bare post ids).
        ("12345", "找不到 12345"),
    ],
)
async def test_refusals_explain_what_is_missing(ids: dict[str, int], ref: str, why: str) -> None:
    key, _, n = ref.partition("/")
    target = f"#{ids[key]}" + (f"/{n}" if n else "") if key in ids else ref
    with pytest.raises(ToolError, match=why):
        await _resolve(_ctx(), target, "image")


class _ForwardApi:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def get_forward_msg(self, *, id: str) -> ForwardMsg:
        self.asked.append(id)
        node = MessageRecord(
            time=1760000000,
            sender=Sender(user_id=10001, nickname="转发里的人"),
            message=[Segment(type="text", data={"text": "节点内容"})],
        )
        return ForwardMsg(messages=[node])


async def test_a_forward_from_the_history_opens_by_message_number(ids: dict[str, int]) -> None:
    # The prod failure: a remark replied to a forward that was only in the history; expand_forward("#<id>") said
    # "找不到句柄" because only fwd#N handles were accepted.
    api = _ForwardApi()
    result = await tools.expand_forward(_ctx(api=api), f"#{ids['forward']}")
    assert api.asked == ["res-1"]
    assert "节点内容" in str(result)


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

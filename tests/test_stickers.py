"""Sticker collection (ingest -> stickers / usage / blobs) and vision tagging, against the local database."""

import io
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy import delete, select

from yggdrasil import runtime
from yggdrasil.db.models import Message, Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.ingest.writer import IncomingMessage, IngestWriter
from yggdrasil.memory import llm
from yggdrasil.stickers import collect, tagging
from yggdrasil.stickers.collect import COLLECTOR, TOO_LARGE, PendingFetch
from yggdrasil.tools.errors import ToolError

GROUP_A, GROUP_B = 990021, 990022
TOP = 10**12  # seen_count that puts test stickers first in the tagging queue


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(out, "PNG")
    return out.getvalue()


def _sticker_seg(ref: str) -> dict[str, Any]:
    return {
        "type": "image",
        "data": {"file": f"{ref}.jpg", "url": f"https://example.com/{ref}", "summary": "[动画表情]", "sub_type": 1},
    }


def _msg(group_id: int, *segments: dict[str, Any], is_self: bool = False) -> IncomingMessage:
    return IncomingMessage(
        group_id=group_id,
        user_id=10001,
        qq_message_id=uuid.uuid4().int % 2**31,
        message_seq=None,
        sent_at=datetime.now(UTC),
        sender_name="tester",
        segments=list(segments),
        is_self=is_self,
    )


@pytest.fixture
async def refs() -> AsyncIterator[Callable[[], str]]:
    made: list[str] = []

    def new() -> str:
        made.append(uuid.uuid4().hex.upper())
        return made[-1]

    yield new
    await COLLECTOR.drain()
    async with get_session() as session:
        await session.execute(delete(Sticker).where(Sticker.ref.in_(made)))
        await session.execute(delete(Message).where(Message.group_id.in_((GROUP_A, GROUP_B))))


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Downloads by ref; an entry's queued outcomes are consumed in order (bytes or an exception), default a PNG."""
    plan: dict[str, list[Any]] = {"calls": []}

    async def fetch(item: PendingFetch) -> bytes:
        ref = item.file.split(".")[0]
        plan["calls"].append(ref)
        queued = plan.get(ref) or []
        outcome = queued.pop(0) if queued else _png()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(collect, "fetch_image", fetch)
    return plan


async def _sticker(ref: str) -> Sticker:
    async with get_session() as session:
        st = await session.scalar(select(Sticker).where(Sticker.ref == ref))
    assert st is not None
    return st


async def _ingest(*messages: IncomingMessage) -> None:
    writer = IngestWriter()
    for m in messages:
        writer.submit(m)
    await writer.flush()
    await COLLECTOR.drain()


async def test_sightings_count_per_group_and_the_image_is_saved_once(
    refs: Callable[[], str], fetches: dict[str, list[Any]]
) -> None:
    x = refs()
    package, emoji = refs(), refs()  # market face ids, cleaned up with the refs
    photo = {
        "type": "image",
        "data": {"file": f"{refs()}.jpg", "url": "https://example.com/p", "summary": "", "sub_type": 0},
    }
    market = {
        "type": "mface",
        "data": {"emoji_package_id": package, "emoji_id": emoji, "key": "k", "summary": "[吃瓜]"},
    }
    await _ingest(
        _msg(GROUP_A, _sticker_seg(x)),
        _msg(GROUP_A, _sticker_seg(x), market),
        _msg(GROUP_B, _sticker_seg(x), photo),
        _msg(GROUP_B, _sticker_seg(x), is_self=True),  # the bot's own messages are not sightings
    )
    st = await _sticker(x)
    assert (st.kind, st.status, st.seen_count, st.mime, st.width, st.height) == (
        "image",
        "untagged",
        3,
        "image/png",
        4,
        3,
    )
    async with get_session() as session:
        usage = dict(
            (
                await session.execute(
                    select(StickerUsage.group_id, StickerUsage.count).where(StickerUsage.sticker_id == st.id)
                )
            ).all()
        )
        assert await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == st.id)) == _png()
        face = await session.scalar(select(Sticker).where(Sticker.ref == f"{package}:{emoji}"))
    assert usage == {GROUP_A: 2, GROUP_B: 1}
    assert face is not None
    assert (face.kind, face.status, face.summary, face.emoji_id) == ("mface", "ready", "吃瓜", emoji)
    assert fetches["calls"] == [x]  # the photo is not a sticker

    await _ingest(_msg(GROUP_B, _sticker_seg(x)))
    assert (await _sticker(x)).seen_count == 4
    assert fetches["calls"] == [x]  # already saved: no second download


async def test_failed_download_is_retried_on_the_next_sighting_but_an_oversized_one_is_not(
    refs: Callable[[], str], fetches: dict[str, list[Any]]
) -> None:
    flaky, huge = refs(), refs()
    fetches[flaky] = [RuntimeError("link expired")]
    fetches[huge] = [ToolError("文件太大（超过 5MB）")]
    await _ingest(_msg(GROUP_A, _sticker_seg(flaky), _sticker_seg(huge)))
    assert ((await _sticker(flaky)).status, (await _sticker(huge)).error) == ("failed", TOO_LARGE)

    await _ingest(_msg(GROUP_A, _sticker_seg(flaky), _sticker_seg(huge)))
    assert (await _sticker(flaky)).status == "untagged"
    assert (await _sticker(huge)).status == "failed"
    assert fetches["calls"].count(flaky) == 2
    assert fetches["calls"].count(huge) == 1


def _model(answers: list[dict[str, Any] | Exception]) -> FunctionModel:
    """Answers one tagging call per entry (output tool arguments, or an exception to raise)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        part = respond(messages, info).parts[0]
        assert isinstance(part, ToolCallPart)
        yield {0: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    return FunctionModel(respond, stream_function=stream)


async def _untagged(ref: str, seen: int) -> None:
    async with get_session() as session:
        st = Sticker(kind="image", ref=ref, status="untagged", summary="动画表情", seen_count=seen)
        session.add(st)
        await session.flush()
        session.add(StickerBlob(sticker_id=st.id, data=_png()))


SAFE = {
    "description": "猫猫歪头",
    "ocr_text": " 啊？ ",
    "emotions": ["困惑", " ", "无语"],
    "usage": "看不懂时",
    "safe": True,
}
GORE = {"description": "血腥画面", "emotions": ["惊吓"], "usage": "不适合", "safe": False, "unsafe_category": "gore"}


async def test_tagging_fills_the_tags_and_only_flags_unsafe_stickers_by_default(refs: Callable[[], str]) -> None:
    cat, gore = refs(), refs()
    await _untagged(cat, TOP + 1)
    await _untagged(gore, TOP)
    with llm.override_models(sticker=_model([SAFE, GORE])):
        assert await tagging.tag_pending(limit=2) == {"tagged": 2, "unsafe": 1, "failed": 0}
    st = await _sticker(cat)
    assert (st.status, st.description, st.ocr_text, st.emotions, st.usage) == (
        "ready",
        "猫猫歪头",
        "啊？",
        ["困惑", "无语"],
        "看不懂时",
    )
    assert (st.enabled, st.unsafe_category) == (True, "")
    flagged = await _sticker(gore)
    assert (flagged.status, flagged.enabled, flagged.unsafe_category) == ("ready", True, "gore")


async def test_the_moderation_setting_disables_unsafe_stickers(
    refs: Callable[[], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime, "_snapshot", runtime.Snapshot(global_settings_doc={"moderation": {"disable_unsafe_stickers": True}})
    )
    gore = refs()
    await _untagged(gore, TOP)
    with llm.override_models(sticker=_model([GORE])):
        await tagging.tag_pending(limit=1)
    st = await _sticker(gore)
    assert (st.enabled, st.unsafe_category) == (False, "gore")


async def test_tagging_gives_up_after_three_failures(refs: Callable[[], str]) -> None:
    broken = refs()
    await _untagged(broken, TOP)
    for expected in ("untagged", "untagged", "failed"):
        with llm.override_models(sticker=_model([RuntimeError("model down")])):
            await tagging.tag_pending(limit=1)
        assert (await _sticker(broken)).status == expected
    st = await _sticker(broken)
    assert (st.tag_attempts, st.error) == (3, "tagging: model down")

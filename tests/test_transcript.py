"""Transcripts of real agent runs: what the console shows for tool images, retries, failures, cancellations, runs
still going, and with content capture off."""

import asyncio
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tests.support.files import png
from tests.support.qq import FakeQQ
from tests.support.routes import Routes
from yggdrasil.agent.media import keep_context_images
from yggdrasil.qq.model import QQError
from yggdrasil.transcript.capture import TranscriptRecorder, entries
from yggdrasil.transcript.notes import TraceNotes, blob_id
from yggdrasil.transcript.schema import Entry, ImagePart, QQContextDisplay, QQMessage
from yggdrasil.transcript.schema import ToolCallPart as TcPart

PNG = b"\x89PNG\r\n\x1a\n-fake-image"


def _returned(messages: list[ModelMessage]) -> list[str]:
    return [p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def _agent(
    respond: Any, recorder: TranscriptRecorder, slow_started: asyncio.Event | None = None
) -> Agent[TraceNotes, str]:
    agent: Agent[TraceNotes, str] = Agent(
        FunctionModel(respond), deps_type=TraceNotes, instructions="你是群聊助手。", capabilities=[recorder]
    )

    @agent.tool
    async def view_image(ctx: RunContext[TraceNotes], id: int) -> ToolReturn:
        ctx.deps.image(PNG, "image/png", segment={"ref": "abc.png", "url": "https://example.com/abc.png", "sha1": ""})
        return ToolReturn(return_value=f"图片 #{id}", content=[BinaryContent(PNG, media_type="image/png")])

    @agent.tool
    async def lookup(ctx: RunContext[TraceNotes], q: str) -> str:
        if q == "bad":
            raise ModelRetry("q 不能是 bad")
        return f"结果：{q}"

    @agent.tool
    async def slow(ctx: RunContext[TraceNotes]) -> str:
        if slow_started is not None:
            slow_started.set()
        await asyncio.sleep(30)
        return "never"

    return agent


def _kinds(items: list[Entry]) -> list[str]:
    return [e.kind if e.kind != "tool" else f"tool:{e.name}:{e.outcome}" for e in items]


async def test_a_run_reads_as_a_conversation() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answered = sum(isinstance(m, ModelResponse) for m in messages)
        if answered == 0:
            return ModelResponse(parts=[ToolCallPart("view_image", {"id": 7}, tool_call_id="c1")])
        if answered == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "bad"}, tool_call_id="c2")])
        if answered == 2:
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "鹈鹕"}, tool_call_id="c3")])
        return ModelResponse(parts=[TextPart("鹈鹕是一种鸟。")])

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    trigger = QQMessage(user_id=10001, segments=[{"type": "text", "data": {"text": "这是什么"}}])
    notes.context = QQContextDisplay(trigger=trigger)
    result = await _agent(respond, recorder).run("看看 #7 是什么", deps=notes)
    assert result.output == "鹈鹕是一种鸟。"

    items = entries(recorder, notes)
    assert _kinds(items) == [
        "system",
        "user",
        "assistant",
        "tool:view_image:ok",
        "assistant",
        "tool:lookup:retry",
        "assistant",
        "tool:lookup:ok",
        "assistant",
    ]
    assert items[0].parts[0].text == "你是群聊助手。"  # pyright: ignore[reportAttributeAccessIssue]
    # The trigger's QQ context sits on the first user turn only.
    assert items[1].display == notes.context
    call = items[2].parts[0]
    assert isinstance(call, TcPart)
    assert (call.id, call.args) == ("c1", {"id": 7})
    # The image view_image handed the model shows on its tool entry (not as a separate user turn), kept as a blob
    # that also stands in for the QQ image segment it came from.
    image = items[3].parts[1]
    assert isinstance(image, ImagePart)
    assert image.blob == blob_id(PNG)
    assert notes.blobs[blob_id(PNG)].data == PNG
    assert notes.image_keys == {"abc.png": blob_id(PNG)}
    assert "q 不能是 bad" in (items[5].parts[0].text or "")  # pyright: ignore[reportAttributeAccessIssue]
    assert items[-1].model == "function:respond:"  # the name the model reported
    assert all(e.duration_ms is not None for e in items if e.kind == "assistant")


async def test_a_failed_run_keeps_what_happened_before_the_failure() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returned(messages):
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "鹈鹕"}, tool_call_id="c1")])
        raise RuntimeError("provider exploded")

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    with pytest.raises(RuntimeError):
        await _agent(respond, recorder).run("问个问题", deps=notes)

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:lookup:ok", "event"]
    assert items[3].parts[0].text == "结果：鹈鹕"  # pyright: ignore[reportAttributeAccessIssue]
    assert items[-1].level == "error"
    assert "provider exploded" in (items[-1].text or "")


async def test_a_running_tool_has_no_result_until_the_run_is_cancelled() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("slow", {}, tool_call_id="c1")])

    steps: list[int] = []
    recorder = TranscriptRecorder(on_change=lambda: steps.append(len(recorder.history)))
    notes = TraceNotes()
    started = asyncio.Event()
    task = asyncio.create_task(_agent(respond, recorder, started).run("跑个慢的", deps=notes))
    await started.wait()
    # While the run goes: the call is there without a result (the console shows it as running).
    assert _kinds(entries(recorder, notes, live=True)) == ["system", "user", "assistant"]
    assert steps == [1, 2, 2]  # the request sent, its answer, the tool started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:slow:cancelled"]
    assert items[-1].parts == []


async def test_request_pictures_the_run_never_loaded_are_kept_for_the_console(monkeypatch: pytest.MonkeyPatch) -> None:
    def image(name: str) -> dict[str, Any]:
        data = {"ref": name, "url": f"https://qq.example.com/{name}", "sha1": "", "sticker": False}
        return {"type": "image", "data": data}

    Routes({"https://qq.example.com/quoted.png": httpx.Response(200, content=png("orange"))}).install(monkeypatch)
    qq = FakeQQ({"media_url": QQError("expired")})
    notes = TraceNotes()
    notes.context = QQContextDisplay(
        trigger=QQMessage(user_id=2, segments=[image("seen.png"), {"type": "text", "data": {"text": "这是啥"}}]),
        quoted=QQMessage(user_id=3, segments=[image("quoted.png"), image("gone.png")]),
    )
    seen = notes.image(png("red"), "image/png", segment=image("seen.png")["data"])  # the run showed it to the model

    await keep_context_images(qq, 990001, 2, notes)

    assert set(notes.image_keys) == {"seen.png", "quoted.png"}  # the one QQ no longer serves is left as a link
    assert notes.image_keys["seen.png"] == seen
    kept = Image.open(io.BytesIO(notes.blobs[notes.image_keys["quoted.png"]].data)).convert("RGB")
    assert kept.getpixel((0, 0)) == (255, 165, 0)


async def test_metadata_mode_keeps_shapes_without_content() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returned(messages):
            return ModelResponse(parts=[ToolCallPart("view_image", {"id": 7}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("一只鸟")])

    recorder = TranscriptRecorder()
    notes = TraceNotes(full=False)
    # The pipeline sets the prompt's QQ context whatever the mode; none of it may be kept.
    notes.context = QQContextDisplay(
        trigger=QQMessage(user_id=10001, segments=[{"type": "text", "data": {"text": "群聊原文"}}])
    )
    await _agent(respond, recorder).run("看看 #7", deps=notes)

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:view_image:ok", "assistant"]
    dumped = json.dumps([e.model_dump(mode="json") for e in items], ensure_ascii=False)
    for secret in ("你是群聊助手", "看看 #7", "图片 #7", "一只鸟", '"id": 7', "群聊原文"):
        assert secret not in dumped
    # Sizes, roles and tool names survive; no image bytes are kept.
    assert items[1].parts[0].chars == len("看看 #7")  # pyright: ignore[reportAttributeAccessIssue]
    call = items[2].parts[0]
    assert isinstance(call, TcPart)
    assert (call.name, call.args) == ("view_image", None)
    assert call.chars > 0
    assert notes.blobs == {}
    assert notes.image_keys == {}

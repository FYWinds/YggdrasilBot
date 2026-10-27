"""An invocation's transcript in the console: the recorded run with the QQ messages its tools read (their images
served from the kept bytes once QQ links expire), the pipeline's moderation and ending woven in; older invocations
fall back to their request."""

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic_ai import Agent, PromptedOutput, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import delete

from yggdrasil.agent.schema import BotReply
from yggdrasil.db.models import Invocation, Message, ModerationResult, TranscriptBlob, UserAlias
from yggdrasil.db.session import get_session
from yggdrasil.transcript import store
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes, blob_id

GROUP, ASKER, POSTER = 990071, 10001, 10003


@dataclass
class Seeded:
    invocation: uuid.UUID
    message: int
    image: bytes
    segment: dict


@pytest.fixture
async def seeded() -> AsyncIterator[Seeded]:
    image = b"\x89PNG\r\n\x1a\n" + os.urandom(16)
    segment = {"file": f"{uuid.uuid4().hex}.png", "url": "https://example.com/expired.png", "sub_type": 0}
    now = datetime.now(UTC)
    async with get_session() as s:
        inv = Invocation(
            group_id=GROUP,
            user_id=ASKER,
            handler="chat",
            request_text="那张图是什么",
            status="ok",
            duration_ms=60_000,
            created_at=now - timedelta(seconds=2),  # the agent runs right after, the ending is 60 s later
        )
        msg = Message(
            sent_at=now - timedelta(minutes=10),
            group_id=GROUP,
            user_id=POSTER,
            qq_message_id=uuid.uuid4().int % 2**31,
            sender_name="旧名片",
            segments=[{"type": "text", "data": {"text": "看这个"}}, {"type": "image", "data": segment}],
        )
        s.add_all([inv, msg, UserAlias(group_id=GROUP, user_id=POSTER, name="发图的人", last_seen=now)])
        await s.flush()
        s.add(
            ModerationResult(
                invocation_id=inv.id,
                stage="input",
                verdict="Safe",
                score=0.01,
                action="pass",
                created_at=now - timedelta(seconds=1),
            )
        )
        seeded = Seeded(inv.id, msg.id, image, segment)
    yield seeded
    async with get_session() as s:
        await s.execute(delete(Invocation).where(Invocation.id == seeded.invocation))
        await s.execute(delete(Message).where(Message.id == seeded.message))
        await s.execute(delete(UserAlias).where(UserAlias.group_id == GROUP))
        await s.execute(delete(TranscriptBlob).where(TranscriptBlob.sha256 == blob_id(image)))


async def _record(seeded: Seeded) -> None:
    """Run an agent whose tool reads the stored message and loads its image, and save the transcript."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("recent_chat", {}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("是一只鹈鹕。")])

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    agent: Agent[TraceNotes, str] = Agent(FunctionModel(respond), deps_type=TraceNotes, capabilities=[recorder])

    @agent.tool
    async def recent_chat(ctx: RunContext[TraceNotes]) -> str:
        ctx.deps.messages(ctx.tool_call_id, [seeded.message])
        ctx.deps.image(seeded.image, "image/png", segment=seeded.segment)
        return f"#{seeded.message} 发图的人：看这个 [图片]"

    await agent.run("那张图是什么", deps=notes)
    await store.save_invocation(seeded.invocation, recorder, notes, GROUP)


async def test_transcript_renders_the_messages_tools_read(client: httpx.AsyncClient, seeded: Seeded) -> None:
    await _record(seeded)
    r = await client.get(f"/api/invocations/{seeded.invocation}/transcript")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["content"] == "full"
    entries = body["entries"]
    # Moderation before the run, the ending after it.
    assert [e["kind"] for e in entries] == ["event", "user", "assistant", "tool", "assistant", "event"]
    assert entries[0]["text"].startswith("输入审核：Safe")
    assert entries[-1]["text"] == "已发出合并转发"
    assert entries[3]["display"] == {"kind": "qq_messages", "message_ids": [seeded.message]}
    assert entries[4]["parts"] == [{"type": "text", "text": "是一只鹈鹕。", "chars": 6}]  # a plain answer stays text
    # The message as it was stored, its image served from the bytes the run loaded (the QQ link has expired),
    # and the poster labelled by their latest card.
    msg = body["messages"][str(seeded.message)]
    assert msg["user_id"] == POSTER
    image = msg["segments"][1]["data"]
    assert image["src"] == f"/api/transcripts/blobs/{blob_id(seeded.image)}"
    assert body["names"][str(POSTER)] == "发图的人"

    blob = await client.get(image["src"])
    assert blob.status_code == 200
    assert blob.headers["content-type"] == "image/png"
    assert blob.content == seeded.image
    assert (await client.get("/api/transcripts/blobs/" + "0" * 64)).status_code == 404


async def test_an_answer_written_as_text_reads_as_the_answer(client: httpx.AsyncClient, seeded: Seeded) -> None:
    # Some providers return the structured output as the response text (prompted / native output mode) instead of
    # calling final_result; the console must still get the reply, not a JSON blob.
    reply = {"mode": "forward", "title": "鹈鹕", "sections": [{"content": "是一只鹈鹕。"}], "sources": []}
    recorder = TranscriptRecorder()
    agent = Agent(
        FunctionModel(lambda _m, _i: ModelResponse(parts=[TextPart(json.dumps(reply, ensure_ascii=False))])),
        output_type=PromptedOutput(BotReply),
        deps_type=TraceNotes,
        capabilities=[recorder],
    )
    await agent.run("那张图是什么", deps=TraceNotes())
    await store.save_invocation(seeded.invocation, recorder, TraceNotes(), GROUP)

    body = (await client.get(f"/api/invocations/{seeded.invocation}/transcript")).json()
    answer = [e for e in body["entries"] if e["kind"] == "assistant"][-1]
    assert [p["type"] for p in answer["parts"]] == ["output"]
    assert answer["parts"][0]["value"] == reply


async def test_an_older_invocation_falls_back_to_its_request(client: httpx.AsyncClient, seeded: Seeded) -> None:
    body = (await client.get(f"/api/invocations/{seeded.invocation}/transcript")).json()
    assert body["available"] is False
    # The request, its input moderation, the ending.
    assert [e["kind"] for e in body["entries"]] == ["user", "event", "event"]
    assert body["entries"][0]["parts"][0]["text"] == "那张图是什么"
    assert (await client.get(f"/api/invocations/{uuid.uuid4()}/transcript")).status_code == 404

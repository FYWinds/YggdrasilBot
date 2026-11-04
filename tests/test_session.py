"""A reply to one of the bot's answers continues that answer's conversation (``pipeline.session``)."""

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import delete, update

from tests.support.groups import fresh_group
from yggdrasil.agent.deps import Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.db.models import Invocation, InvocationSession
from yggdrasil.db.session import get_session
from yggdrasil.pipeline import session as sessions
from yggdrasil.transcript.capture import TranscriptRecorder, entries
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import QQContextDisplay, QQMessage

PNG = b"\x89PNG\r\n\x1a\n-fake-image"
PAGE = "正文" * 5000


def _answer(title: str) -> ToolCallPart:
    return ToolCallPart("final_result", {"mode": "forward", "title": title, "sections": [{"content": title}]})


def _agent(respond: Any, recorder: TranscriptRecorder, instructions: str) -> Agent[None, BotReply]:
    agent = Agent(FunctionModel(respond), output_type=BotReply, instructions=instructions, capabilities=[recorder])

    @agent.tool_plain
    async def read_url(url: str) -> str:
        return PAGE

    return agent


def _wire(messages: list[ModelMessage]) -> list[Any]:
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


async def test_a_reply_resends_the_answers_run_unchanged() -> None:
    def first(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(
                parts=[
                    ThinkingPart("先读原文", signature="sig", provider_name="anthropic"),
                    ToolCallPart("read_url", {"url": "https://a.example"}),
                ]
            )
        return ModelResponse(parts=[_answer("原文说了什么")])

    answer = await _agent(first, TranscriptRecorder(), "tldr 的规则").run(
        ["<请求-ab12cd>看看 img#1</请求-ab12cd>", "img#1：", BinaryContent(PNG, media_type="image/png")]
    )
    # What is kept goes through JSON, as it does through the database.
    kept = ModelMessagesTypeAdapter.validate_python(_wire(answer.all_messages()))

    seen: list[list[ModelMessage]] = []

    def second(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return ModelResponse(parts=[_answer("接着说")])

    recorder = TranscriptRecorder(prior=kept, continued_from="inv-1")
    notes = TraceNotes()
    notes.context = QQContextDisplay(trigger=QQMessage(user_id=10001))
    reply = await _agent(second, recorder, "tldr 的规则").run("<请求-ab12cd>为什么</请求-ab12cd>", message_history=kept)
    assert reply.output.title == "接着说"

    # Everything before the new turn is what the provider got last time (thinking, image and full tool result
    # included), so its prompt cache hits; the new request joins the request that closed the answer.
    sent = seen[0]
    assert _wire(sent[: len(kept) - 1]) == _wire(kept[:-1])
    assert sent[len(kept) - 1].parts[: len(kept[-1].parts)] == kept[-1].parts
    assert any(isinstance(p, ThinkingPart) and p.signature == "sig" for m in sent for p in m.parts)
    page = next(p for m in sent for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "read_url")
    assert page.content == PAGE
    assert isinstance(sent[-1], ModelRequest)
    assert sent[-1].instructions == "tldr 的规则"
    assert [p.content for p in sent[-1].parts if isinstance(p, UserPromptPart)] == ["<请求-ab12cd>为什么</请求-ab12cd>"]

    # The console shows this run's turns; the earlier ones belong to the earlier invocation.
    shown = entries(recorder, notes)
    assert [e.kind for e in shown] == ["event", "system", "user", "assistant"]
    assert "inv-1" in (shown[0].text or "")
    assert shown[2].display == notes.context
    assert shown[2].parts[0].text == "<请求-ab12cd>为什么</请求-ab12cd>"  # pyright: ignore[reportAttributeAccessIssue]


def test_handles_carry_over_and_keep_counting() -> None:
    handles = Handles()
    handles.add("forward", {"id": "res-1"})
    handles.add("image", {"file": "a.png"})
    restored = sessions.load_handles(sessions.dump_handles(handles))
    fwd = restored.get("fwd#1")
    assert fwd is not None
    assert fwd.data == {"id": "res-1"}
    assert restored.add("image", {"file": "a.png"}) == "img#1"
    assert restored.add("image", {"file": "b.png"}) == "img#2"


@pytest.mark.db
async def test_only_a_recent_delivered_answer_is_continued() -> None:
    group_id, answer = fresh_group(), random.randint(1, 2**31)
    messages = [ModelRequest(parts=[UserPromptPart("问题")]), ModelResponse(parts=[_answer("答案")])]
    handles = Handles()
    handles.add("forward", {"id": "res-1"})
    setup = sessions.Setup("tldr", "tldr 的规则", ["read_url", "view_image"], sandbox=False)

    def answered(reply_qq_message_id: int, status: str) -> Invocation:
        return Invocation(
            group_id=group_id, user_id=10001, handler="chat", reply_qq_message_id=reply_qq_message_id, status=status
        )

    ok, held, stale = answered(answer, "ok"), answered(answer + 1, "review"), answered(answer + 2, "ok")
    async with get_session() as s:
        s.add_all([ok, held, stale])
    try:
        for inv in (ok, held, stale):
            await sessions.save(inv.id, setup, messages, handles, "ab12cd", 4242)
        async with get_session() as s:
            await s.execute(
                update(InvocationSession)
                .where(InvocationSession.invocation_id == stale.id)
                .values(created_at=datetime.now(UTC) - timedelta(hours=2))
            )
        found = await sessions.load(group_id, answer, timedelta(hours=1))
        assert found is not None
        assert (found.invocation_id, found.nonce, found.seen_message_id) == (ok.id, "ab12cd", 4242)
        assert found.setup == setup
        assert [type(m) for m in found.messages] == [ModelRequest, ModelResponse]
        assert found.handles.get("fwd#1") is not None
        assert await sessions.load(group_id, answer + 1, timedelta(hours=1)) is None  # held for review
        assert await sessions.load(group_id, answer + 2, timedelta(hours=1)) is None  # too old
        assert await sessions.load(group_id + 1, answer, timedelta(hours=1)) is None  # another group
    finally:
        async with get_session() as s:
            await s.execute(delete(Invocation).where(Invocation.group_id == group_id))

"""A running invocation in the console: its transcript and the row's numbers are written while the run goes."""

import asyncio
import functools
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.pipeline import invoke, record
from yggdrasil.qq.model import Login
from yggdrasil.settings import Settings
from yggdrasil.transcript.capture import TranscriptRecorder, entries
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.store import LiveWriter
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10001, 20001
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
FORWARD = '{"mode": "forward", "sections": [{"content": "答案"}]}'


async def test_the_console_follows_a_run_that_waits_on_a_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    saves: list[tuple[bool, list[str]]] = []  # (live, what the transcript held)
    rows: list[tuple[str, int]] = []  # (status, input tokens) written to the invocation's row
    waiting, release = asyncio.Event(), asyncio.Event()

    async def save_invocation(
        _id: Any, recorder: TranscriptRecorder, notes: TraceNotes, _group: Any, *, live: bool = False
    ) -> None:
        kinds = [e.kind if e.kind != "tool" else f"tool:{e.name}" for e in entries(recorder, notes, live=live)]
        saves.append((live, kinds))

    async def progress_row(_id: Any, meta: record.Meta) -> None:
        rows.append(("running", meta.tokens_in))

    async def finish_row(_id: Any, status: str, _error: Any, meta: record.Meta, *_a: Any, **_k: Any) -> None:
        rows.append((status, meta.tokens_in))

    async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
        return Verdict(stage, "Skipped", 0.0)

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(invoke.transcripts, "save_invocation", save_invocation)
    monkeypatch.setattr(invoke.transcripts, "LiveWriter", functools.partial(LiveWriter, interval=0.01))
    monkeypatch.setattr(invoke.record, "progress_row", progress_row)
    monkeypatch.setattr(invoke.record, "finish_row", finish_row)
    monkeypatch.setattr(invoke, "moderate", moderate)
    monkeypatch.setattr(invoke, "react", nothing)
    monkeypatch.setattr(invoke.Invocator, "deliver", nothing)
    monkeypatch.setattr(invoke.ratelimit, "add_tokens", nothing)

    async def wait(_ctx: RunContext[Deps]) -> str:
        """Wait."""
        waiting.set()
        await release.wait()
        return "好了"

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=FORWARD)}
        else:
            yield {0: DeltaToolCall(name="wait", json_args="{}")}

    def never(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    settings = Settings()
    qq = FakeQQ({"login": Login(SELF, "Yggdrasil")}, self_id=SELF)
    m = incoming("问个问题", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=7, to_me=True)
    inv = invoke.Invocator(qq, m, CHAT, "chat", "", settings, GROUP)
    inv.invocation_id = uuid4()
    model = FunctionModel(never, stream_function=stream)
    resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")

    async def _run(_inv: invoke.Invocator) -> str:
        agent = inv._agent(resolved, tools=[Tool(wait)], capabilities=[], instructions="")  # pyright: ignore[reportPrivateUsage]
        deps = Deps(qq, GROUP, ALICE, settings, Handles(), "chat")
        return await inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(invoke.Invocator, "_run", _run)
    async with asyncio.timeout(5):
        task = asyncio.create_task(inv.run())
        await waiting.wait()
        await asyncio.sleep(0.1)
        # The request and the model's tool call are there; the tool is still running, so it has no result yet.
        assert saves[-1] == (True, ["user", "assistant"])
        assert rows[-1][0] == "running"
        assert rows[-1][1] > 0
        release.set()
        assert await task == "ok"
    assert saves[-1] == (False, ["user", "assistant", "tool:wait", "assistant"])
    assert [live for live, _ in saves].count(False) == 1  # nothing is written after the final save
    assert rows[-1][0] == "ok"


async def test_live_writes_carry_the_latest_state_and_end_with_stop() -> None:
    state = {"step": 0}
    written: list[int] = []

    async def write() -> None:
        written.append(state["step"])

    live = LiveWriter(write, interval=0.05)
    state["step"] = 1
    live.poke()
    await asyncio.sleep(0.01)
    assert written == [1]
    for step in (2, 3, 4):  # a burst inside the interval is one write, of the last state
        state["step"] = step
        live.poke()
    await asyncio.sleep(0.15)
    assert written == [1, 4]
    state["step"] = 5
    live.poke()
    await live.stop()
    live.poke()
    await asyncio.sleep(0.1)
    assert written == [1, 4]

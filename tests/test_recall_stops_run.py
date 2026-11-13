"""Recalling the message that asked the bot stops the run before it answers."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.groups import fresh_group
from tests.support.onebot import FakeNapCat, group_message
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.pipeline import invoke, recalls
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10001, 20001
TRIGGER = 7
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
ANSWER = '{"mode": "forward", "sections": [{"content": "答案"}]}'


class Run:
    """A chat run whose model streams until ``release`` is set, then answers."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.streaming = asyncio.Event()
        self.release = asyncio.Event()
        self.model_cancelled = False
        self.delivered: list[BotReply] = []
        self.reactions: list[str] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_api: Any, _message_id: int, emoji: str) -> None:
            self.reactions.append(emoji)

        async def deliver(_inv: invoke.Invocator, reply: BotReply) -> None:
            self.delivered.append(reply)

        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke, "react", react)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver)

    async def stream(self, _messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        self.streaming.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.model_cancelled = True
            raise
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=ANSWER)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def start(self, watch: recalls.Watch) -> "asyncio.Task[str]":
        event = group_message("@bot 问个问题", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=TRIGGER)
        settings = Settings()
        api = FakeNapCat(self_id=SELF)
        # No group on the run itself: its rate-limit and invocation bookkeeping stay out of the way.
        inv = invoke.Invocator(api.script, api, event, CHAT, "chat", "", settings, None, watch=watch)  # pyright: ignore[reportArgumentType]
        model = FunctionModel(self._never, stream_function=self.stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(api, None, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")
        task = asyncio.create_task(
            inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]
        )
        async with asyncio.timeout(5):
            await self.streaming.wait()
        return task


async def test_recalling_the_trigger_stops_the_streaming_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch)
    with recalls.watch(GROUP, TRIGGER) as watch:
        task = await run.start(watch)
        assert recalls.recall(GROUP, TRIGGER)
        async with asyncio.timeout(5):
            status = await task
    assert status == "cancelled"
    assert run.model_cancelled
    assert run.delivered == []
    assert run.reactions == []  # the message is gone: nothing is added to it


async def test_another_recall_leaves_the_run_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch)
    with recalls.watch(GROUP, TRIGGER) as watch:
        task = await run.start(watch)
        assert not recalls.recall(GROUP, TRIGGER + 1)
        assert not recalls.recall(GROUP + 1, TRIGGER)
        run.release.set()
        async with asyncio.timeout(5):
            status = await task
    assert status == "ok"
    assert [r.sections[0].content for r in run.delivered] == ["答案"]
    # Once the run is over, recalling its trigger finds nothing to stop.
    assert not recalls.recall(GROUP, TRIGGER)

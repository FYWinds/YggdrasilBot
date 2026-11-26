"""Status reactions on the trigger message: QQ shows the requester a gray tip for each one, so a request gets
"received" right away, and a result reaction only when no message carries the outcome."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ContentFilterError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, ThinkingPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.groups import fresh_group
from tests.support.qq import FakeQQ, incoming
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.llm.errors import describe
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.pipeline import invoke
from yggdrasil.qq.model import Login
from yggdrasil.settings import Settings
from yggdrasil.triggers.index import Resolved

GROUP, SELF, ALICE = fresh_group(), 10001, 20001
TRIGGER = 7
CHAT = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
FORWARD = '{"mode": "forward", "sections": [{"content": "答案"}]}'
SILENT = '{"mode": "silent", "silent_reason": "不是在问我"}'
REASON = "This request triggered restrictions on violative cyber content."


def refused() -> ContentFilterError:
    """What pydantic-ai raises for Anthropic's stop_reason=refusal: the whole response dumped as the error's body,
    the thinking signature ahead of the reason."""
    response = ModelResponse(
        parts=[ThinkingPart("想到一半", signature="s" * 3000)],
        finish_reason="content_filter",
        provider_details={"finish_reason": "refusal", "refusal": REASON, "refusal_category": "cyber"},
    )
    body = ModelMessagesTypeAdapter.dump_json([response]).decode()
    return ContentFilterError("Content filter triggered. Finish reason: 'refusal'", body=body)


class Run:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: str, *, think_s: float = 0) -> None:
        self.answer = answer
        self.think_s = think_s
        self.reactions: list[str] = []
        self.delivered: list[BotReply] = []

        async def moderate(stage: str, *_a: Any, **_k: Any) -> Verdict:
            return Verdict(stage, "Skipped", 0.0)

        async def react(_qq: Any, _group_id: int | None, _message_id: int, emoji: str) -> None:
            self.reactions.append(emoji)

        async def deliver(_inv: invoke.Invocator, reply: BotReply) -> None:
            self.delivered.append(reply)

        async def add_tokens(*_a: Any) -> None:
            return None

        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke, "react", react)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver)
        monkeypatch.setattr(invoke.ratelimit, "add_tokens", add_tokens)

    async def _stream(self, _messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        await asyncio.sleep(self.think_s)
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=self.answer)}

    def _never(self, _messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("agents stream")

    async def go(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail: Exception | None = None,
        slow_after_s: float = 30,
        refusal_notice: bool = True,
    ) -> str:
        m = incoming("问个问题", group_id=GROUP, user_id=ALICE, self_id=SELF, message_id=TRIGGER, to_me=True)
        settings = Settings()
        settings.interaction = settings.interaction.model_copy(
            update={"slow_after_s": slow_after_s, "refusal_notice": refusal_notice}
        )
        qq = self.qq = FakeQQ({"login": Login(SELF, "Yggdrasil")}, self_id=SELF)
        inv = invoke.Invocator(qq, m, CHAT, "chat", "", settings, GROUP)
        model = FunctionModel(self._never, stream_function=self._stream)
        agent = Agent(model, output_type=BotReply, deps_type=Deps)
        deps = Deps(qq, GROUP, ALICE, settings, Handles(), "chat")
        resolved = ResolvedModel("chat", model, {}, UsageLimits(), "test")

        async def _run(_inv: invoke.Invocator) -> str:
            if fail is not None:
                raise fail
            return await inv._execute(agent, "问个问题", deps, resolved, "问个问题")  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(invoke.Invocator, "_run", _run)
        async with asyncio.timeout(5):
            return await inv.run()


async def test_a_sent_answer_adds_only_received(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Run(monkeypatch, FORWARD)
    assert await run.go(monkeypatch) == "ok"
    assert len(run.delivered) == 1
    assert run.reactions == [Settings().interaction.received_emoji]  # no "done" on top of the answer itself


async def test_an_outcome_without_a_message_gets_its_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    silent = Run(monkeypatch, SILENT)
    assert await silent.go(monkeypatch) == "silent"
    assert silent.reactions == [cfg.received_emoji, cfg.silent_emoji]

    failed = Run(monkeypatch, "")
    assert await failed.go(monkeypatch, fail=RuntimeError("boom")) == "failed"
    assert failed.reactions == [cfg.received_emoji, cfg.failure_emoji]


async def test_a_run_still_working_after_slow_after_s_says_so_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    slow = Run(monkeypatch, FORWARD, think_s=0.3)
    assert await slow.go(monkeypatch, slow_after_s=0.05) == "ok"
    assert slow.reactions == [cfg.received_emoji, cfg.slow_emoji]

    quick = Run(monkeypatch, FORWARD)
    assert await quick.go(monkeypatch, slow_after_s=0.05) == "ok"
    await asyncio.sleep(0.1)  # the timer is cancelled once the answer is in, not left to fire after it went out
    assert quick.reactions == [cfg.received_emoji]


async def test_a_refusal_tells_the_chat_why_instead_of_a_failure_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings().interaction
    told = Run(monkeypatch, "")
    assert await told.go(monkeypatch, fail=refused()) == "failed"
    [sent] = told.qq.called("send")
    texts = [s["data"]["text"] for s in sent["segments"] if s["type"] == "text"]
    assert texts == [f"模型拒绝了这个请求（cyber）：{REASON}"]
    assert told.reactions == [cfg.received_emoji]  # the message is the outcome

    quiet = Run(monkeypatch, "")
    assert await quiet.go(monkeypatch, fail=refused(), refusal_notice=False) == "failed"
    assert quiet.qq.called("send") == []
    assert quiet.reactions == [cfg.received_emoji, cfg.failure_emoji]


def test_a_recorded_error_leads_with_the_reason() -> None:
    # The first 1000 characters are what an invocation keeps.
    assert describe(refused())[:1000] == (
        "ContentFilterError: Content filter triggered. Finish reason: 'refusal'\n"
        f"refusal_category: cyber\nrefusal: {REASON}"
    )
    every_model_failed = ExceptionGroup("All models failed", [TimeoutError("primary timed out"), refused()])
    assert describe(every_model_failed).splitlines()[:2] == [
        "ExceptionGroup: All models failed",
        "- TimeoutError: primary timed out",
    ]
    assert REASON in describe(every_model_failed)
    assert describe(ValueError("plain")) == "ValueError: plain"

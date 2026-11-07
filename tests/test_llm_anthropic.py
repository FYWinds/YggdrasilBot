import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings, ThinkingLevel

from yggdrasil.agent.schema import BotReply
from yggdrasil.llm import registry
from yggdrasil.runtime import ProviderInfo

_MESSAGE = {"id": "m", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn"}
_REPLY = '{"mode": "silent"}'


def _answer(request: httpx2.Request) -> httpx2.Response:
    if not json.loads(request.content).get("stream"):
        usage = {"input_tokens": 1, "output_tokens": 1}
        return httpx2.Response(200, json={**_MESSAGE, "content": [{"type": "text", "text": _REPLY}], "usage": usage})
    events = [
        ("message_start", {"message": {**_MESSAGE, "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": _REPLY}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {}),
    ]
    sse = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events)
    return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})


async def _sent(monkeypatch: pytest.MonkeyPatch, model: str, thinking: ThinkingLevel | None) -> dict[str, Any]:
    """The body of the first request a structured-output agent on ``model`` sends through an Anthropic provider."""
    seen: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return _answer(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    client = AsyncAnthropic(api_key="k", base_url="https://gw.example.com", http_client=http)
    monkeypatch.setattr(registry, "anthropic_client", lambda _p: client)
    provider = ProviderInfo(1, "gw", "https://gw.example.com/v1", None, {}, True, (), api_format="anthropic")
    agent = Agent(registry._build_model("chat", model, provider, False), output_type=BotReply)  # pyright: ignore[reportPrivateUsage]
    await agent.run("hi", model_settings=ModelSettings(thinking=thinking) if thinking else ModelSettings())
    return seen[0]


@pytest.mark.parametrize("model", ["cvp/claude-opus-5-5", "claude-haiku-5-5", "claude-opus-4-7"])
async def test_claude_that_omits_thinking_text_is_asked_for_a_summary(monkeypatch: pytest.MonkeyPatch, model: str):
    body = await _sent(monkeypatch, model, "low")
    assert body["thinking"]["display"] == "summarized"


@pytest.mark.parametrize("model", ["claude-opus-4-6", "cvp/claude-sonnet-4-6"])
async def test_claude_that_rejects_display_gets_no_display_field(monkeypatch: pytest.MonkeyPatch, model: str):
    body = await _sent(monkeypatch, model, "low")
    assert "display" not in body["thinking"]


async def test_default_thinking_of_claude_5_comes_with_a_summary(monkeypatch: pytest.MonkeyPatch):
    body = await _sent(monkeypatch, "cvp/claude-opus-5-5", None)
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "effort" not in body.get("output_config", {})


async def test_a_model_that_does_not_think_by_default_gets_no_thinking_config(monkeypatch: pytest.MonkeyPatch):
    assert "thinking" not in await _sent(monkeypatch, "claude-opus-4-7", None)


async def test_a_gateway_prefixed_claude_5_is_recognized(monkeypatch: pytest.MonkeyPatch):
    # Unrecognized, the level became {"type": "enabled", "budget_tokens": 2048} and no effort.
    body = await _sent(monkeypatch, "cvp/claude-opus-5-5", "low")
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"]["effort"] == "low"


async def test_structured_output_stays_prompted_for_a_recognized_claude(monkeypatch: pytest.MonkeyPatch):
    body = await _sent(monkeypatch, "cvp/claude-opus-5-5", None)
    assert "format" not in body.get("output_config", {})
    assert "final_result" not in {t["name"] for t in body.get("tools", [])}
    assert "json" in body["system"][-1]["text"].lower()

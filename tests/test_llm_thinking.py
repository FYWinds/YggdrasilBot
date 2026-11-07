import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings, ThinkingLevel

from yggdrasil.llm import registry
from yggdrasil.runtime import ProviderInfo

_MESSAGE = {"id": "m", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn"}


def _answer(request: httpx2.Request) -> httpx2.Response:
    if not json.loads(request.content).get("stream"):
        usage = {"input_tokens": 1, "output_tokens": 1}
        return httpx2.Response(200, json={**_MESSAGE, "content": [{"type": "text", "text": "ok"}], "usage": usage})
    events = [
        ("message_start", {"message": {**_MESSAGE, "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"index": 0}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        ("message_stop", {}),
    ]
    sse = "".join(f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n" for name, data in events)
    return httpx2.Response(200, text=sse, headers={"content-type": "text/event-stream"})


async def _sent_thinking(monkeypatch: pytest.MonkeyPatch, model: str, thinking: ThinkingLevel | None) -> Any:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return _answer(request)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    client = AsyncAnthropic(api_key="k", base_url="https://gw.example.com", http_client=http)
    monkeypatch.setattr(registry, "anthropic_client", lambda _p: client)
    provider = ProviderInfo(1, "gw", "https://gw.example.com/v1", None, {}, True, (), api_format="anthropic")
    agent = Agent(registry._build_model("chat", model, provider, False))  # pyright: ignore[reportPrivateUsage]
    await agent.run("hi", model_settings=ModelSettings(thinking=thinking) if thinking else ModelSettings())
    return seen[0].get("thinking")


@pytest.mark.parametrize("model", ["cvp/claude-opus-5-5", "claude-haiku-5-5", "claude-opus-4-7"])
async def test_claude_that_omits_thinking_text_is_asked_for_a_summary(monkeypatch: pytest.MonkeyPatch, model: str):
    thinking = await _sent_thinking(monkeypatch, model, "low")
    assert thinking["display"] == "summarized"


@pytest.mark.parametrize("model", ["claude-opus-4-6", "cvp/claude-sonnet-4-6"])
async def test_claude_that_rejects_display_gets_no_display_field(monkeypatch: pytest.MonkeyPatch, model: str):
    thinking = await _sent_thinking(monkeypatch, model, "low")
    assert "display" not in thinking


async def test_a_request_without_thinking_config_stays_without_one(monkeypatch: pytest.MonkeyPatch):
    assert await _sent_thinking(monkeypatch, "cvp/claude-opus-5-5", None) is None

"""generate_image: the image route's OpenRouter-style /images call, and the picture going to the group as its own
message (moderated like every image the bot sends)."""

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest
from PIL import Image
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil import runtime
from yggdrasil.agent import tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm import registry
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.napcat.models import MessageRecord, Segment, SendResult
from yggdrasil.settings import Settings

GROUP, TRIGGER, SENT = 10001, 555, 77
MODEL = "openai/gpt-image-test"


def _png(color: str = "red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, "PNG")
    return out.getvalue()


def _ok(png: bytes, cost: float = 0.04) -> httpx.Response:
    item = {"b64_json": base64.b64encode(png).decode(), "media_type": "image/png"}
    return httpx.Response(
        200, json={"data": [item], "usage": {"prompt_tokens": 12, "completion_tokens": 4175, "cost": cost}}
    )


@dataclass
class Upstream:
    response: httpx.Response = field(default_factory=lambda: _ok(_png()))
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    state = Upstream()
    provider = runtime.ProviderInfo(1, "or", "https://openrouter.example.com/api/v1", "sk-test", {}, True, ())
    image_route = runtime.Route("image", 1, MODEL, {"quality": "high"}, None, None, {})
    monkeypatch.setattr(runtime, "_snapshot", runtime.Snapshot(providers={1: provider}, routes={"image": image_route}))
    real = registry.generate_images

    async def generate(prompt: str, **kwargs: Any) -> registry.GeneratedImages:
        async with httpx.AsyncClient(transport=httpx.MockTransport(state.handle)) as client:
            return await real(prompt, client=client, **kwargs)

    monkeypatch.setattr(tools, "generate_images", generate)
    return state


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[list[dict[str, Any]]] = []

    async def send_group_msg(self, *, group_id: int, message: list[dict[str, Any]]) -> SendResult:
        self.sent.append(message)
        return SendResult(message_id=SENT)

    async def get_msg(self, *, message_id: int) -> MessageRecord:
        stored = Segment(type="image", data={"file": "abc.png", "url": "https://example.com/abc.png"})
        return MessageRecord(message_id=message_id, message=[stored])


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[IncomingMessage]:
    out: list[IncomingMessage] = []
    monkeypatch.setattr(send.WRITER, "submit", out.append)
    return out


def _ctx(api: FakeAPI, *, moderated: bool = False) -> RunContext[Deps]:
    settings = Settings.model_validate({"moderation": {"enabled": moderated, "check_output": moderated}})
    deps = Deps(cast(Any, api), GROUP, 20001, settings, Handles(), "chat", trigger_message_id=TRIGGER, self_id=10000)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id="call_1")


async def test_the_picture_is_sent_replying_to_the_trigger(upstream: Upstream, recorded: list[IncomingMessage]):
    api = FakeAPI()
    ctx = _ctx(api)
    result = await tools.generate_image(ctx, "一只戴墨镜的猫", aspect_ratio="16:9", text="画好了")

    assert upstream.requests[0].url.path == "/api/v1/images"
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-test"
    # The route's params go along; "auto" would leave aspect_ratio out.
    assert upstream.body == {
        "quality": "high",
        "model": MODEL,
        "prompt": "一只戴墨镜的猫",
        "n": 1,
        "aspect_ratio": "16:9",
    }

    [message] = api.sent
    assert message[0] == {"type": "reply", "data": {"id": str(TRIGGER)}}
    assert message[1] == {"type": "text", "data": {"text": "画好了"}}
    assert message[2]["type"] == "image"
    picture = base64.b64decode(message[2]["data"]["file"].removeprefix("base64://"))
    assert Image.open(io.BytesIO(picture)).size == (8, 8)

    assert "$0.040" in str(result)
    assert ctx.deps.actions == [f"image {SENT}"]  # lets the run end with mode=done
    # Our own record keeps the picture as QQ stored it, so a later "改一下这张图" can load it again.
    [own] = recorded
    assert own.is_self
    assert own.qq_message_id == SENT
    assert own.segments == [{"type": "image", "data": {"file": "abc.png", "url": "https://example.com/abc.png"}}]


async def test_reference_images_go_as_data_urls(upstream: Upstream):
    png = _png("blue")
    await registry.generate_images(
        "改成蓝色",
        references=[(png, "image/png")],
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handle)),
    )
    url = upstream.body["input_references"][0]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(png).decode()
    assert "aspect_ratio" not in upstream.body


async def test_a_refusal_reaches_the_model_and_nothing_is_sent(upstream: Upstream):
    upstream.response = httpx.Response(400, json={"error": {"message": "Request rejected by the safety system"}})
    api = FakeAPI()
    ctx = _ctx(api)
    result = await tools.generate_image(ctx, "x")
    assert "画图失败：400 Request rejected by the safety system" in str(result)
    assert api.sent == []
    assert ctx.deps.actions == []


async def test_an_unsafe_picture_is_not_sent(upstream: Upstream, monkeypatch: pytest.MonkeyPatch):
    async def unsafe(_blob: bytes) -> ImageVerdict:
        return ImageVerdict(False, "gore", "vision-model", 10, 2)

    monkeypatch.setattr(tools, "check_image", unsafe)
    api = FakeAPI()
    ctx = _ctx(api, moderated=True)
    result = await tools.generate_image(ctx, "x")
    assert "审核没通过（gore）" in str(result)
    assert api.sent == []
    assert ctx.deps.actions == []

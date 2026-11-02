"""generate_image: the image route's OpenRouter-style /images call, and the picture going to the group as its own
message (moderated like every image the bot sends)."""

import base64
import io
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import NetworkError
from PIL import Image
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.onebot import FakeNapCat
from tests.support.routes import Routes
from yggdrasil import runtime
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools import images
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.llm import registry
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.napcat.models import MessageRecord, Segment, SendResult
from yggdrasil.settings import Settings

GROUP, TRIGGER, SENT = 10001, 555, 77
MODEL = "openai/gpt-image-test"
IMAGES = "https://openrouter.example.com/api/v1/images"


def _ok(picture: bytes, cost: float = 0.04) -> httpx.Response:
    item = {"b64_json": base64.b64encode(picture).decode(), "media_type": "image/png"}
    return httpx.Response(
        200, json={"data": [item], "usage": {"prompt_tokens": 12, "completion_tokens": 4175, "cost": cost}}
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch, snapshot: Callable[..., runtime.Snapshot]) -> Routes:
    """The image route's provider; ``answers[IMAGES]`` is what it draws."""
    provider = runtime.ProviderInfo(1, "or", "https://openrouter.example.com/api/v1", "sk-test", {}, True, ())
    snapshot(
        providers={1: provider}, routes={"image": runtime.Route("image", 1, MODEL, {"quality": "high"}, None, None, {})}
    )
    routes = Routes({IMAGES: _ok(png())})
    routes.install(monkeypatch)
    return routes


def _body(upstream: Routes) -> dict[str, Any]:
    return json.loads(upstream.seen[-1].content)


def _qq(*, timeout: bool = False) -> FakeNapCat:
    """QQ confirms the picture as message ``SENT`` (or never does), and keeps it as an image file with a URL."""
    stored = Segment(type="image", data={"file": "abc.png", "url": "https://example.com/abc.png"})
    confirmed = NetworkError("WebSocket call api send_group_msg timeout") if timeout else SendResult(message_id=SENT)
    return FakeNapCat(
        {
            "send_group_msg": confirmed,
            "get_msg": lambda message_id, **_: MessageRecord(message_id=message_id, message=[stored]),
        }
    )


def _sent(api: FakeNapCat) -> list[list[dict[str, Any]]]:
    return [call["message"] for call in api.called("send_group_msg")]


def _ctx(api: FakeNapCat, *, moderated: bool = False) -> RunContext[Deps]:
    settings = Settings.model_validate({"moderation": {"enabled": moderated, "check_output": moderated}})
    return run_ctx(
        api=api,
        group_id=GROUP,
        user_id=20001,
        settings=settings,
        tool_call_id="call_1",
        trigger_message_id=TRIGGER,
        self_id=10000,
    )


async def test_the_picture_is_sent_replying_to_the_trigger(upstream: Routes, recorded: list[IncomingMessage]):
    api = _qq()
    ctx = _ctx(api)
    result = await images.generate_image(ctx, "一只戴墨镜的猫", aspect_ratio="16:9", text="画好了")

    assert upstream.seen[0].url.path == "/api/v1/images"
    assert upstream.seen[0].headers["authorization"] == "Bearer sk-test"
    # The route's params go along; "auto" would leave aspect_ratio out.
    assert _body(upstream) == {
        "quality": "high",
        "model": MODEL,
        "prompt": "一只戴墨镜的猫",
        "n": 1,
        "aspect_ratio": "16:9",
    }

    [message] = _sent(api)
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


async def test_an_unconfirmed_send_counts_as_sent(upstream: Routes, recorded: list[IncomingMessage]):
    # QQ can take longer than the call's timeout to confirm a picture it did post (the first prod drawing): the run
    # must end as done, and the model must not post it again.
    api = _qq(timeout=True)
    ctx = _ctx(api)
    result = await images.generate_image(ctx, "x")
    [call] = api.called("send_group_msg")
    assert call["_timeout"] > nonebot.get_driver().config.api_timeout
    assert "不要重发" in str(result)
    assert ctx.deps.actions == ["image (unconfirmed)"]
    assert recorded == []  # no message id to record it under


async def test_a_large_picture_is_sent_as_jpeg(upstream: Routes, recorded: list[IncomingMessage]):
    noisy = io.BytesIO()
    Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)).save(noisy, "PNG")  # over the 600 KB send limit
    upstream.answers[IMAGES] = _ok(noisy.getvalue())
    api = _qq()
    await images.generate_image(_ctx(api), "x")
    reply, image = _sent(api)[0]  # no text: the reply to the trigger, then the picture
    assert reply["type"] == "reply"
    sent = base64.b64decode(image["data"]["file"].removeprefix("base64://"))
    assert sent[:2] == b"\xff\xd8"
    assert len(sent) < len(noisy.getvalue())
    assert Image.open(io.BytesIO(sent)).size == (512, 512)


async def test_reference_images_go_as_data_urls(upstream: Routes):
    blue = png("blue")
    async with upstream.client() as client:
        await registry.generate_images("改成蓝色", references=[(blue, "image/png")], client=client)
    url = _body(upstream)["input_references"][0]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(blue).decode()
    assert "aspect_ratio" not in _body(upstream)


async def test_a_refusal_reaches_the_model_and_nothing_is_sent(upstream: Routes):
    upstream.answers[IMAGES] = httpx.Response(400, json={"error": {"message": "Request rejected by the safety system"}})
    api = _qq()
    ctx = _ctx(api)
    result = await images.generate_image(ctx, "x")
    assert "画图失败：400 Request rejected by the safety system" in str(result)
    assert _sent(api) == []
    assert ctx.deps.actions == []


async def test_an_unsafe_picture_is_not_sent(upstream: Routes, monkeypatch: pytest.MonkeyPatch):
    async def unsafe(_blob: bytes) -> ImageVerdict:
        return ImageVerdict(False, "gore", "vision-model", 10, 2)

    monkeypatch.setattr(images, "check_image", unsafe)
    api = _qq()
    ctx = _ctx(api, moderated=True)
    result = await images.generate_image(ctx, "x")
    assert "审核没通过（gore）" in str(result)
    assert _sent(api) == []
    assert ctx.deps.actions == []

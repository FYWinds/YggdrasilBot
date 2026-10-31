"""Sending the reply forward when QQ doesn't confirm it in time or rejects it."""

import json
import time
from typing import Any, cast

import pytest
from nonebot.adapters.onebot.v11 import ActionFailed, NetworkError

from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.napcat.models import (
    ForwardMsg,
    ForwardResult,
    MessageRecord,
    MsgHistory,
    Segment,
    Sender,
    SendResult,
)
from yggdrasil.settings import InteractionSettings

GROUP = 990001
SELF = 10000
FORWARD_ID = 41
TEXT_ID = 42

REPLY = BotReply(
    mode="forward",
    title="解读",
    sections=[Section(content="**第一段**正文"), Section(content="第二段")],
    sources=[SourceRef(label="gist", url="https://example.com/gist")],
)


class FakeAPI:
    def __init__(
        self,
        *,
        forward: Exception | None = None,
        history: list[MessageRecord] | Exception,
        posted: dict[str, str | Exception] | None = None,
    ) -> None:
        self.forward_error = forward
        self.history = history
        self.posted = posted or {}  # resid -> first node text of forwards in the group
        self.forwards: list[list[dict[str, Any]]] = []
        self.timeouts: list[float | None] = []
        self.texts: list[list[dict[str, Any]]] = []

    async def send_group_forward_msg(
        self, *, group_id: int, messages: list[dict[str, Any]], reply_timeout: float | None = None
    ) -> ForwardResult:
        self.forwards.append(messages)
        self.timeouts.append(reply_timeout)
        if self.forward_error is not None:
            raise self.forward_error
        return ForwardResult(message_id=FORWARD_ID, res_id="res")

    async def get_group_msg_history(self, *, group_id: int, count: int = 20) -> MsgHistory:
        if isinstance(self.history, Exception):
            raise self.history
        return MsgHistory(messages=self.history)

    async def get_forward_msg(self, *, id: str) -> ForwardMsg:
        first = self.posted[id]
        if isinstance(first, Exception):
            raise first
        return ForwardMsg(messages=[MessageRecord(user_id=SELF, message=[Segment(type="text", data={"text": first})])])

    async def send_group_msg(self, *, group_id: int, message: list[dict[str, Any]]) -> SendResult:
        self.texts.append(message)
        return SendResult(message_id=TEXT_ID)


def _msg(user_id: int, ago: float, segment: Segment, message_id: int = 7) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        time=int(time.time() - ago),
        user_id=user_id,
        sender=Sender(user_id=user_id),
        message=[segment],
    )


TIMEOUT = NetworkError("WebSocket call api send_group_forward_msg timeout")
OLD_CHAT = _msg(20001, 600, Segment(type="text", data={"text": "早些时候的话"}), message_id=5)
OURS = "第一段正文"  # first node of REPLY as sent (markdown stripped)


def _forward(resid: str, message_id: int) -> MessageRecord:
    return _msg(SELF, 2, Segment(type="forward", data={"id": resid}), message_id=message_id)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[IncomingMessage]:
    out: list[IncomingMessage] = []
    monkeypatch.setattr(send.WRITER, "submit", out.append)
    monkeypatch.setattr(send, "FORWARD_CHECK_DELAYS_S", (0.0, 0.0))
    return out


async def _send(api: FakeAPI, reply: BotReply = REPLY, cfg: InteractionSettings | None = None) -> int | None:
    return await send.send_forward(
        cast(Any, api),
        group_id=GROUP,
        user_id=20001,
        reply=reply,
        meta_text="⚙ 元数据",
        cfg=cfg or InteractionSettings(),
        self_id=SELF,
        nickname="Yggdrasil",
    )


async def test_forwards_wait_longer_than_the_adapter_timeout(recorded: list[IncomingMessage]):
    api = FakeAPI(history=[])
    assert await _send(api) == FORWARD_ID
    assert api.timeouts == [send.SEND_FORWARD_TIMEOUT_S]
    assert [m.qq_message_id for m in recorded] == [FORWARD_ID]


async def test_unconfirmed_forward_found_in_history_counts_as_sent(recorded: list[IncomingMessage]):
    # QQ posted the forward after the call timed out (2c567dde): it's the bot's reply, nothing is sent again.
    api = FakeAPI(forward=TIMEOUT, history=[OLD_CHAT, _forward("late", 99)], posted={"late": OURS})
    assert await _send(api) == 99
    assert len(api.forwards) == 1
    assert api.texts == []
    [record] = recorded
    assert record.qq_message_id == 99
    assert record.segments[0]["data"]["id"] == "late"
    assert "第二段" in str(record.segments[0]["data"]["content"])


async def test_another_runs_forward_is_not_taken_for_ours(recorded: list[IncomingMessage]):
    # Another answer posted in the same window: ours is the one opening with our first node.
    api = FakeAPI(
        forward=TIMEOUT,
        history=[OLD_CHAT, _forward("other", 98), _forward("late", 99)],
        posted={"other": "别的回答", "late": OURS},
    )
    assert await _send(api) == 99
    assert [m.qq_message_id for m in recorded] == [99]

    # Only the other run's forward is there: ours never went out, so the answer goes out as text.
    recorded.clear()
    api = FakeAPI(forward=TIMEOUT, history=[OLD_CHAT, _forward("other", 98)], posted={"other": "别的回答"})
    assert await _send(api) == TEXT_ID
    assert len(api.texts) == 1
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


async def test_napcat_self_forward_card_is_recognised(recorded: list[IncomingMessage]):
    # NapCat hands the bot's own forwards back as a com.tencent.multimsg json card.
    card = json.dumps({"app": "com.tencent.multimsg", "meta": {"detail": {"resid": "card-res"}}})
    posted = _msg(SELF, 2, Segment(type="json", data={"data": card}), message_id=99)
    api = FakeAPI(forward=TIMEOUT, history=[OLD_CHAT, posted], posted={"card-res": OURS})
    assert await _send(api) == 99
    assert api.texts == []
    [record] = recorded
    assert record.segments[0]["data"]["id"] == "card-res"


@pytest.mark.parametrize(
    "message",
    [
        "send group message failed: no response",
        "group message delivery was not confirmed: QQ returned no message receipt; check the conversation before "
        "retrying",
    ],
)
async def test_snowluma_maybe_delivered_failure_checks_the_history(recorded: list[IncomingMessage], message: str):
    failed = ActionFailed(status="failed", retcode=100, data=None, message=message, wording=message)
    api = FakeAPI(forward=failed, history=[OLD_CHAT, _forward("late", 99)], posted={"late": OURS})
    assert await _send(api) == 99
    assert api.texts == []
    assert [m.qq_message_id for m in recorded] == [99]


@pytest.mark.parametrize("history", ["unavailable", "busy"])
async def test_unconfirmed_forward_the_history_cant_rule_out_is_not_sent_again(
    recorded: list[IncomingMessage], history: str
):
    # Built here, not at collection: "newer than the send" must hold when the test runs.
    page: list[MessageRecord] | Exception = (
        RuntimeError("history unavailable")
        if history == "unavailable"
        # Busy group: the whole page is newer than the send, so the forward may be further back.
        else [_msg(20001, -5, Segment(type="text", data={"text": f"刷屏{i}"}), message_id=i) for i in range(3)]
    )
    api = FakeAPI(forward=TIMEOUT, history=page)
    assert await _send(api) is None
    assert len(api.forwards) == 1
    assert api.texts == []
    assert recorded == []


async def test_bot_forward_that_cant_be_checked_is_not_sent_again(recorded: list[IncomingMessage]):
    api = FakeAPI(
        forward=TIMEOUT, history=[OLD_CHAT, _forward("late", 99)], posted={"late": RuntimeError("get_forward_msg")}
    )
    assert await _send(api) is None
    assert api.texts == []
    assert recorded == []


async def test_forward_with_pictures_missing_from_history_is_not_sent_as_text(recorded: list[IncomingMessage]):
    # Its pictures may still be uploading: a text copy now could be followed by the forward.
    api = FakeAPI(forward=TIMEOUT, history=[OLD_CHAT])
    result = await send.send_forward(
        cast(Any, api),
        group_id=GROUP,
        user_id=20001,
        reply=REPLY,
        meta_text=None,
        cfg=InteractionSettings(),
        self_id=SELF,
        nickname="Yggdrasil",
        images=[(b"\x89PNG", "图")],
    )
    assert result is None
    assert len(api.forwards) == 1
    assert api.texts == []


async def test_forward_missing_from_history_falls_back_to_text_once(recorded: list[IncomingMessage]):
    api = FakeAPI(forward=TIMEOUT, history=[OLD_CHAT])
    assert await _send(api) == TEXT_ID
    assert len(api.forwards) == 1
    [message] = api.texts
    text = message[0]["data"]["text"]
    assert text.startswith("解读\n\n第一段正文\n\n第二段")  # plain text, markdown stripped
    assert "https://example.com/gist" in text
    assert "元数据" not in text
    assert [m.qq_message_id for m in recorded] == [TEXT_ID]


async def test_rejected_forward_falls_back_to_one_text_within_the_length_limit(recorded: list[IncomingMessage]):
    rejected = "send group message rejected: result=120 err=禁言中"
    api = FakeAPI(forward=ActionFailed(status="failed", retcode=100, message=rejected), history=[])
    long = REPLY.model_copy(update={"sections": [Section(content="长" * 150), Section(content="文" * 150)]})
    assert await _send(api, long, InteractionSettings(node_max_chars=200)) == TEXT_ID
    assert len(api.forwards) == 1
    [message] = api.texts
    text = message[0]["data"]["text"]
    assert len(text) <= 200
    assert text.endswith("（后文过长，已省略）")

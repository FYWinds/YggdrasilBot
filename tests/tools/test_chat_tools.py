"""The chat-history tools as the model sees them: expand_forward on the bot's own forwards, search_history,
recent_chat (rows in the local ParadeDB are removed afterwards)."""

import math
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete

from yggdrasil.agent import tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import EmbedSpace, MemoryVector, Message, MessageChunk, Provider, Topic
from yggdrasil.db.session import get_session
from yggdrasil.delivery import send
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.memory import search
from yggdrasil.napcat.api import node_seg, text_seg
from yggdrasil.napcat.models import ForwardResult
from yggdrasil.settings import ContextSettings, InteractionSettings, Settings

GROUP = 990021
SELF = 10000
HEADER = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] ")  # a chat line's time
MINUTE = timedelta(minutes=1)


def _ctx(*, trigger: int | None = None, settings: Settings | None = None) -> RunContext[Deps]:
    deps = Deps(cast(Any, None), GROUP, 20001, settings or Settings(), Handles(), "chat", self_id=SELF)
    deps.trigger_message_id = trigger
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


# --- expand_forward on the bot's own forwards ------------------------------------------------------


class _ForwardApi:
    async def send_group_forward_msg(
        self, *, group_id: int, messages: list[dict[str, Any]], reply_timeout: float | None = None
    ) -> ForwardResult:
        return ForwardResult(message_id=41, res_id="res")


async def _expand(ctx: RunContext[Deps], forward: dict[str, Any]) -> str:
    key = ctx.deps.handles.add("forward", forward["data"])
    assert key is not None
    return str(await tools.expand_forward(ctx, key))


async def test_the_bots_own_forward_shows_its_send_time_and_no_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # The prod failure: every node of a forward the bot sent came back as "[] Yggdrasil(你): …", and the
    # "⚙ 元数据" node (model, tokens, trace id) was handed to the model as part of the answer.
    recorded: list[IncomingMessage] = []
    monkeypatch.setattr(send.WRITER, "submit", recorded.append)
    meta = send.Meta(model_names=["some-model"], tokens_in=1200, tokens_out=80, trace_id="0123456789abcdef0")
    reply = BotReply(
        mode="forward",
        title="解读",
        sections=[Section(content="第一段正文"), Section(content="第二段")],
        sources=[SourceRef(label="gist", url="https://example.com/gist")],
    )
    await send.send_forward(
        cast(Any, _ForwardApi()),
        group_id=GROUP,
        user_id=20001,
        reply=reply,
        meta_text=meta.render(["model", "tokens", "trace"]),
        cfg=InteractionSettings(),
        self_id=SELF,
        nickname="Yggdrasil",
    )
    [record] = recorded

    lines = (await _expand(_ctx(), record.segments[0])).splitlines()

    nodes = [line for line in lines if HEADER.match(line)]
    assert [n.split(": ", 1)[1] for n in nodes] == ["第一段正文", "第二段", "来源"]
    assert not any(word in line for line in lines for word in ("元数据", "tokens", "trace", "some-model"))


async def test_forwards_stored_before_the_fix_show_no_empty_time_and_no_metadata() -> None:
    meta = "⚙ 元数据\n模型  some-model\ntrace  0123456789abcdef"
    stored = {
        "type": "forward",
        "data": {
            "id": "res",
            "content": [node_seg(SELF, "Yggdrasil", [text_seg("答案")]), node_seg(SELF, "Yggdrasil", [text_seg(meta)])],
        },
    }

    result = await _expand(_ctx(), stored)

    assert result.endswith(": 答案")
    assert "[" not in result.split(": ", 1)[0]
    assert "元数据" not in result


# --- search_history ---------------------------------------------------------------------------------


def _unit(cos: float) -> list[float]:
    """A unit vector at cosine ``cos`` to the first axis."""
    vec = [0.0] * EMBED_DIM
    vec[0], vec[1] = cos, math.sqrt(1 - cos * cos)
    return vec


async def _cleanup() -> None:
    async with get_session() as s:
        await s.execute(delete(MemoryVector).where(MemoryVector.group_id == GROUP))
        await s.execute(delete(MessageChunk).where(MessageChunk.group_id == GROUP))
        await s.execute(delete(Topic).where(Topic.group_id == GROUP))
        await s.execute(delete(Message).where(Message.group_id == GROUP))


@pytest.fixture
async def history() -> AsyncIterator[None]:
    await _cleanup()
    yield
    await _cleanup()


async def _message(text: str, *, ago: timedelta, qq: int = 1) -> int:
    async with get_session() as s:
        m = Message(
            sent_at=datetime.now(UTC) - ago,
            group_id=GROUP,
            user_id=20002,
            qq_message_id=qq,
            sender_name="群友",
            plain_text=text,
            segments=[text_seg(text)],
        )
        s.add(m)
        await s.flush()
        return m.id


@pytest.fixture
async def space(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    """An embedding space whose query vector is the first axis: a vector's cosine to the query is its x."""
    async with get_session() as s:
        provider = Provider(name="test-chat-tools-embed", base_url="http://embed.example.com/v1")
        s.add(provider)
        await s.flush()
        es = EmbedSpace(provider_id=provider.id, model="test-embed", dim=EMBED_DIM)
        s.add(es)
        await s.flush()
        space_id, provider_id = es.id, provider.id

    async def query_vector(_: str) -> tuple[int, list[float]]:
        return space_id, _unit(1.0)

    monkeypatch.setattr(search, "_query_vector", query_vector)
    yield space_id
    async with get_session() as s:
        await s.execute(delete(EmbedSpace).where(EmbedSpace.id == space_id))
        await s.execute(delete(Provider).where(Provider.id == provider_id))


async def _chunk(space_id: int, text: str, cos: float, *, ago: timedelta) -> int:
    async with get_session() as s:
        c = MessageChunk(
            group_id=GROUP,
            first_msg_id=1,
            last_msg_id=2,
            start_at=datetime.now(UTC) - ago - 30 * MINUTE,
            end_at=datetime.now(UTC) - ago,
            msg_count=text.count("\n") + 1,
            text=text,
        )
        s.add(c)
        await s.flush()
        s.add(MemoryVector(kind="chunk", ref_id=c.id, space_id=space_id, group_id=GROUP, embedding=_unit(cos)))
        return c.id


async def _topic(space_id: int, title: str, summary: str, cos: float, *, ago: timedelta) -> int:
    async with get_session() as s:
        t = Topic(
            group_id=GROUP,
            title=title,
            summary=summary,
            start_at=datetime.now(UTC) - ago - 30 * MINUTE,
            end_at=datetime.now(UTC) - ago,
            first_msg_id=1,
            last_msg_id=2,
        )
        s.add(t)
        await s.flush()
        s.add(MemoryVector(kind="topic", ref_id=t.id, space_id=space_id, group_id=GROUP, embedding=_unit(cos)))
        return t.id


def _chat(i: int, text: str) -> str:
    return f"[10-01 12:{i:02d}] 群友{i}(2000{i % 10}): {text}"


async def test_search_history_leaves_out_the_request_and_shows_only_the_matching_part_of_chunks(
    history: None, space: int
) -> None:
    # The prod failures: the asking message came back as the first hit, and chunks of a whole conversation
    # (thousands of characters, the query nowhere in them) filled the rest.
    old = await _message("周末一起去爬泰山吧", ago=300 * MINUTE)
    lines = [_chat(i, f"闲聊第{i}句，" + "说点别的" * 10) for i in range(40)]
    lines[20] = _chat(20, "泰山的日出真的很好看")
    matching = await _chunk(space, "\n".join(lines), 0.55, ago=240 * MINUTE)
    unrelated = await _chunk(space, "\n".join(_chat(i, "今天中午吃什么") for i in range(40)), 0.5, ago=200 * MINUTE)
    close = await _topic(space, "登山计划", "大家商量国庆去爬山，定了路线和集合时间", 0.7, ago=180 * MINUTE)
    trigger = await _message("@Yggdrasil 泰山怎么走", ago=MINUTE, qq=777)
    later = await _message("泰山今天下雨了", ago=0 * MINUTE)

    result = str(await tools.search_history(_ctx(trigger=777), "泰山"))

    assert f"#{old}" in result
    assert f"#{trigger}" not in result
    assert "泰山怎么走" not in result
    assert f"#{later}" not in result
    assert "下雨" not in result
    # The matching chunk: the line with the word and its neighbours, not the whole conversation.
    assert f"chunk:{matching}" in result
    assert "泰山的日出真的很好看" in result
    assert "闲聊第19句" in result
    assert "闲聊第21句" in result
    assert "闲聊第0句" not in result
    assert "闲聊第39句" not in result
    # Without the word: only when the vectors are close.
    assert f"chunk:{unrelated}" not in result
    assert "中午吃什么" not in result
    assert f"topic:{close}" in result
    assert len(result) < 1500


async def test_search_history_shows_a_match_late_in_a_long_message(history: None, space: int) -> None:
    # Review finding: chunk lines were cut to 300 characters before looking for the query, so a match late in a
    # long message was missed (the chunk dropped below the similarity bar), and message hits were cut from the start.
    filler = "前面说了很多别的事情，" * 120
    lines = [_chat(i, f"闲聊第{i}句") for i in range(10)]
    lines[5] = _chat(5, f"{filler}最后提到泰山的索道票价是一百二十元")
    chunk = await _chunk(space, "\n".join(lines), 0.5, ago=240 * MINUTE)
    message = await _message(f"{filler}结论：泰山的西线人少一点", ago=200 * MINUTE)

    result = str(await tools.search_history(_ctx(), "泰山"))

    assert f"chunk:{chunk}" in result
    assert "泰山的索道票价是一百二十元" in result
    assert "[10-01 12:05] 群友5(20005): …" in result  # the sender stays, the filler before the match goes
    assert f"#{message}" in result
    assert "泰山的西线人少一点" in result
    assert len(result) < 2000


# --- recent_chat ------------------------------------------------------------------------------------


async def test_recent_chat_never_starts_inside_a_multi_line_message(history: None) -> None:
    # The prod failure: the character budget cut a long multi-line message, so the output started with its tail,
    # a line without time, number or sender.
    long = await _message("\n".join(f"第{i}行：" + "很长的一段内容" * 3 for i in range(40)), ago=20 * MINUTE)
    for i in range(10):
        await _message(f"后面的第{i}条消息" + "。" * 20, ago=(10 - i) * MINUTE)
    settings = Settings(context=ContextSettings(max_context_chars=1000))

    result = str(await tools.recent_chat(_ctx(settings=settings), minutes=60))

    assert re.match(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] #\d+ ", result)
    assert f"#{long}" not in result  # dropped whole, not cut in half
    assert "第39行" not in result
    assert "后面的第0条消息" in result
    assert "后面的第9条消息" in result
    assert len(result) <= 1000

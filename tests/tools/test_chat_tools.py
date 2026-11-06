"""The chat-history tools as the model sees them: expand_forward on the bot's own forwards, search_history,
recent_chat (rows in the local ParadeDB are removed afterwards)."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import RunContext

from tests.support.agent import run_ctx
from tests.support.db import insert_message, purge_groups, unit_vector
from tests.support.groups import fresh_group
from tests.support.onebot import FakeNapCat
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.schema import BotReply, Section, SourceRef
from yggdrasil.agent.tools.attachments import expand_forward
from yggdrasil.agent.tools.history import recent_chat, search_history
from yggdrasil.db.models import MemoryVector, MessageChunk, Topic
from yggdrasil.db.session import get_session
from yggdrasil.delivery.forward import send_forward
from yggdrasil.ingest.writer import IncomingMessage
from yggdrasil.napcat.api import node_seg, text_seg
from yggdrasil.napcat.models import ForwardResult
from yggdrasil.pipeline.record import Meta
from yggdrasil.settings import ContextSettings, InteractionSettings, Settings

pytestmark = pytest.mark.db

GROUP = fresh_group()
SELF = 10000
HEADER = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] ")  # a chat line's time
MINUTE = timedelta(minutes=1)


def _ctx(*, trigger: int | None = None, settings: Settings | None = None) -> RunContext[Deps]:
    return run_ctx(group_id=GROUP, user_id=20001, settings=settings, self_id=SELF, trigger_message_id=trigger)


# --- expand_forward on the bot's own forwards ------------------------------------------------------


async def _expand(ctx: RunContext[Deps], forward: dict[str, Any]) -> str:
    key = ctx.deps.handles.add("forward", forward["data"])
    assert key is not None
    return str(await expand_forward(ctx, key))


async def test_the_bots_own_forward_shows_its_send_time_and_no_metadata(recorded: list[IncomingMessage]) -> None:
    # The prod failure: every node of a forward the bot sent came back as "[] Yggdrasil(你): …", and the
    # "⚙ 元数据" node (model, tokens, trace id) was handed to the model as part of the answer.
    meta = Meta(model_names=["some-model"], tokens_in=1200, tokens_out=80, trace_id="0123456789abcdef0")
    reply = BotReply(
        mode="forward",
        title="解读",
        sections=[Section(content="第一段正文"), Section(content="第二段")],
        sources=[SourceRef(label="gist", url="https://example.com/gist")],
    )
    await send_forward(
        FakeNapCat({"send_group_forward_msg": ForwardResult(message_id=41, res_id="res")}),
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


@pytest.fixture
async def history() -> AsyncIterator[None]:
    await purge_groups(GROUP)
    yield
    await purge_groups(GROUP)


async def _message(text: str, *, ago: timedelta, qq: int = 1) -> int:
    return await insert_message(GROUP, text, user_id=20002, sent_at=datetime.now(UTC) - ago, qq_message_id=qq)


async def _chunk(space_id: int, text: str, cos: float, *, ago: timedelta, msgs: tuple[int, int] = (1, 2)) -> int:
    async with get_session() as s:
        c = MessageChunk(
            group_id=GROUP,
            first_msg_id=msgs[0],
            last_msg_id=msgs[1],
            start_at=datetime.now(UTC) - ago - 30 * MINUTE,
            end_at=datetime.now(UTC) - ago,
            msg_count=text.count("\n") + 1,
            text=text,
        )
        s.add(c)
        await s.flush()
        s.add(MemoryVector(kind="chunk", ref_id=c.id, space_id=space_id, group_id=GROUP, embedding=unit_vector(cos)))
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
        s.add(MemoryVector(kind="topic", ref_id=t.id, space_id=space_id, group_id=GROUP, embedding=unit_vector(cos)))
        return t.id


def _chat(i: int, text: str) -> str:
    return f"[10-01 12:{i:02d}] 群友{i}(2000{i % 10}): {text}"


async def test_search_history_leaves_out_the_request_and_shows_only_the_matching_part_of_chunks(
    history: None, search_space: int
) -> None:
    # The prod failures: the asking message came back as the first hit, and chunks of a whole conversation
    # (thousands of characters, the query nowhere in them) filled the rest.
    old = await _message("周末一起去爬泰山吧", ago=300 * MINUTE)
    lines = [_chat(i, f"闲聊第{i}句，" + "说点别的" * 10) for i in range(40)]
    lines[20] = _chat(20, "泰山的日出真的很好看")
    matching = await _chunk(search_space, "\n".join(lines), 0.55, ago=240 * MINUTE)
    unrelated = await _chunk(
        search_space, "\n".join(_chat(i, "今天中午吃什么") for i in range(40)), 0.5, ago=200 * MINUTE
    )
    close = await _topic(search_space, "登山计划", "大家商量国庆去爬山，定了路线和集合时间", 0.7, ago=180 * MINUTE)
    trigger = await _message("@Yggdrasil 泰山怎么走", ago=MINUTE, qq=777)
    later = await _message("泰山今天下雨了", ago=0 * MINUTE)

    result = str(await search_history(_ctx(trigger=777), "泰山"))

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


async def test_search_history_shows_a_match_late_in_a_long_message(history: None, search_space: int) -> None:
    # Review finding: chunk lines were cut to 300 characters before looking for the query, so a match late in a
    # long message was missed (the chunk dropped below the similarity bar), and message hits were cut from the start.
    filler = "前面说了很多别的事情，" * 120
    lines = [_chat(i, f"闲聊第{i}句") for i in range(10)]
    lines[5] = _chat(5, f"{filler}最后提到泰山的索道票价是一百二十元")
    chunk = await _chunk(search_space, "\n".join(lines), 0.5, ago=240 * MINUTE)
    message = await _message(f"{filler}结论：泰山的西线人少一点", ago=200 * MINUTE)

    result = str(await search_history(_ctx(), "泰山"))

    assert f"chunk:{chunk}" in result
    assert "泰山的索道票价是一百二十元" in result
    assert "[10-01 12:05] 群友5(20005): …" in result  # the sender stays, the filler before the match goes
    assert f"#{message}" in result
    assert "泰山的西线人少一点" in result
    assert len(result) < 2000


async def test_search_history_shows_a_line_once_and_ignores_asking_words(history: None, search_space: int) -> None:
    # Prod review: the same message came back as a hit and inside a chunk, and "是谁" matched every line asking who.
    said = await _message("老王是泰山脚下开民宿的", ago=240 * MINUTE)
    asking = await _message("这人是谁啊", ago=230 * MINUTE)
    lines = [_chat(i, f"闲聊第{i}句") for i in range(6)]
    lines[3] = _chat(3, "老王是泰山脚下开民宿的")
    chunk = await _chunk(search_space, "\n".join(lines), 0.5, ago=230 * MINUTE, msgs=(said - 3, asking))

    result = str(await search_history(_ctx(), "老王是谁"))

    assert f"chunk:{chunk}" in result
    assert result.count("老王是泰山脚下开民宿的") == 1
    assert f"#{said}" not in result
    assert f"#{asking}" not in result


async def test_search_history_keeps_message_hits_a_chunk_does_not_show_whole(history: None, search_space: int) -> None:
    # Review findings: a message was dropped when a chunk merely contained its text, and its image lost its handle.
    pic = await insert_message(
        GROUP,
        "老王的猫[图片]",
        user_id=20002,
        sent_at=datetime.now(UTC) - 240 * MINUTE,
        segments=[
            {"type": "text", "data": {"text": "老王的猫"}},
            {"type": "image", "data": {"url": "https://example.com/c.png"}},
        ],
    )
    short = await _message("老王", ago=239 * MINUTE)
    lines = [_chat(i, f"闲聊第{i}句") for i in range(4)]
    lines[1], lines[2] = _chat(1, "老王的猫[图片]"), _chat(2, "老王家又养了一只老王的猫")
    await _chunk(search_space, "\n".join(lines), 0.5, ago=230 * MINUTE, msgs=(pic - 1, short + 1))

    result = str(await search_history(_ctx(), "老王"))

    assert f"#{pic}" in result
    assert "img#" in result
    assert f"#{short}" in result


# --- recent_chat ------------------------------------------------------------------------------------


async def test_recent_chat_never_starts_inside_a_multi_line_message(history: None) -> None:
    # The prod failure: the character budget cut a long multi-line message, so the output started with its tail,
    # a line without time, number or sender.
    long = await _message("\n".join(f"第{i}行：" + "很长的一段内容" * 3 for i in range(40)), ago=20 * MINUTE)
    for i in range(10):
        await _message(f"后面的第{i}条消息" + "。" * 20, ago=(10 - i) * MINUTE)
    settings = Settings(context=ContextSettings(max_context_chars=1000))

    result = str(await recent_chat(_ctx(settings=settings), minutes=60))

    assert re.match(r"^\[\d{2}-\d{2} \d{2}:\d{2}\] #\d+ ", result)
    assert f"#{long}" not in result  # dropped whole, not cut in half
    assert "第39行" not in result
    assert "后面的第0条消息" in result
    assert "后面的第9条消息" in result
    assert len(result) <= 1000

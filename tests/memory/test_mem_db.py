"""Memory pipelines and search against the local ParadeDB (rows are removed afterwards)."""

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from sqlalchemy import delete, select, update

from yggdrasil.db.models import (
    GlossaryCandidate,
    Message,
    PipelineState,
    Topic,
    TopicMessage,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.memory import glossary, llm, profiles, search
from yggdrasil.memory.topics import archive_closed, consolidate_group

SEARCH_GROUP, OTHER_GROUP, TOPIC_GROUP = 990003, 990004, 990002
PROFILE_GROUP, GLOSSARY_GROUP = 990005, 990006
GROUPS = (SEARCH_GROUP, OTHER_GROUP, TOPIC_GROUP, PROFILE_GROUP, GLOSSARY_GROUP)

type Insert = Callable[..., Awaitable[int]]
type Respond = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


def _model(respond: Respond) -> FunctionModel:
    """Agents stream every request; replay ``respond``'s response as one chunk per part."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        for i, part in enumerate(respond(messages, info).parts):
            if isinstance(part, ToolCallPart):
                yield {i: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(respond, stream_function=stream)


async def _cleanup(groups: tuple[int, ...]) -> None:
    async with get_session() as session:
        await session.execute(delete(Topic).where(Topic.group_id.in_(groups)))
        await session.execute(delete(PipelineState).where(PipelineState.group_id.in_(groups)))
        await session.execute(delete(UserProfileFact).where(UserProfileFact.group_id.in_(groups)))
        await session.execute(delete(UserProfile).where(UserProfile.group_id.in_(groups)))
        await session.execute(delete(GlossaryCandidate).where(GlossaryCandidate.group_id.in_(groups)))
        await session.execute(delete(Message).where(Message.group_id.in_(groups)))


@pytest.fixture
async def insert() -> AsyncIterator[Insert]:
    await _cleanup(GROUPS)
    qq = iter(range(5000, 6000))

    async def _insert(group_id: int, user_id: int, text: str, *, ago: timedelta, recalled: bool = False) -> int:
        async with get_session() as session:
            msg = Message(
                sent_at=datetime.now(UTC) - ago,
                group_id=group_id,
                user_id=user_id,
                qq_message_id=next(qq),
                sender_name=f"用户{user_id}",
                plain_text=text,
                segments=[],
                recalled=recalled,
                recalled_at=datetime.now(UTC) if recalled else None,
            )
            session.add(msg)
            await session.flush()
            return msg.id

    yield _insert
    await _cleanup(GROUPS)


async def test_search_history_bm25_scoping(insert: Insert) -> None:
    m = timedelta(minutes=1)
    hike = await insert(SEARCH_GROUP, 1, "周末一起去爬泰山吧", ago=30 * m)
    sunrise = await insert(SEARCH_GROUP, 2, "泰山的日出真的很好看", ago=20 * m)
    await insert(SEARCH_GROUP, 2, "泰山门票多少钱", ago=10 * m, recalled=True)
    await insert(SEARCH_GROUP, 3, "今天中午吃什么", ago=5 * m)
    await insert(OTHER_GROUP, 1, "泰山泰山泰山", ago=5 * m)

    hits = await search.search_history(SEARCH_GROUP, "泰山")
    assert {h.msg_id for h in hits} == {hike, sunrise}
    assert all(h.kind == "message" and h.sender_name for h in hits)

    only_user = await search.search_history(SEARCH_GROUP, "泰山", user_id=2)
    assert [h.msg_id for h in only_user] == [sunrise]
    recent_only = await search.search_history(SEARCH_GROUP, "泰山", since=datetime.now(UTC) - 25 * m)
    assert [h.msg_id for h in recent_only] == [sunrise]
    assert await search.search_history(SEARCH_GROUP, "   ") == []

    recent = await search.recent_messages(SEARCH_GROUP, limit=10)
    assert [v.text for v in recent] == ["周末一起去爬泰山吧", "泰山的日出真的很好看", "今天中午吃什么"]
    around = await search.messages_around(SEARCH_GROUP, sunrise, before=1, after=5)
    assert [v.id for v in around][:2] == [hike, sunrise]
    assert len(around) == 3
    found = await search.find_by_qq_id(SEARCH_GROUP, recent[1].qq_message_id)
    assert found is not None
    assert found.id == sunrise


def _segmenting_model(bogus_topic: int) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = "".join(str(getattr(p, "content", "")) for msg in messages for p in msg.parts)
        ids = [int(i) for i in re.findall(r"^#(\d+) ", prompt, re.M)]
        ops = [
            {
                "op": "new",
                "title": "爬山计划",
                "summary": "大家讨论周末去爬山",
                "keywords": ["爬山"],
                "participants": [1, 2, 424242],
                "message_ids": [*ids[:3], 999_999_999],
            },
            {"op": "continue", "topic_id": bogus_topic, "message_ids": ids[3:]},
        ]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps({"ops": ops}))])

    return _model(respond)


async def test_consolidation_validates_ops_closes_and_archives(insert: Insert, group_settings) -> None:
    memory = {"topic_min_new_messages": 3, "topic_close_after_minutes": 60}
    # The topic below ends up with 3 messages from 2 people: exactly at the message threshold, so it stays visible.
    group_settings(
        {TOPIC_GROUP: {"memory": memory | {"topic_archive_below_messages": 3, "topic_archive_below_participants": 3}}}
    )
    h = timedelta(hours=1)
    ids = [
        await insert(TOPIC_GROUP, 1, "周末去爬山吗", ago=3 * h),
        await insert(TOPIC_GROUP, 2, "好啊去哪座山", ago=3 * h - timedelta(minutes=1)),
        await insert(TOPIC_GROUP, 1, "香山吧", ago=3 * h - timedelta(minutes=2)),
        await insert(TOPIC_GROUP, 3, "我不去了", ago=3 * h - timedelta(minutes=3)),
    ]
    with llm.override_models(memory_topic=_segmenting_model(bogus_topic=123_456_789)):
        stats = await consolidate_group(TOPIC_GROUP)
    assert stats["new"] == 1
    assert stats["continued"] == 0
    assert stats["closed"] == 1
    assert stats["archived"] == 0

    async with get_session() as session:
        topic = await session.scalar(select(Topic).where(Topic.group_id == TOPIC_GROUP))
        assert topic is not None
        linked = set(await session.scalars(select(TopicMessage.message_id).where(TopicMessage.topic_id == topic.id)))
        state = await session.get(PipelineState, ("topic", TOPIC_GROUP))
    assert linked == set(ids[:3])
    assert topic.participants == [1, 2]  # 424242 never spoke
    assert (topic.first_msg_id, topic.last_msg_id, topic.msg_count) == (ids[0], ids[2], 3)
    assert topic.status == "closed"
    assert state is not None
    assert state.last_msg_id == ids[-1]

    # A recall marks the topic dirty; recomputation drops the recalled message.
    async with get_session() as session:
        await session.execute(update(Message).where(Message.id == ids[2]).values(recalled=True))
        await session.execute(update(Topic).where(Topic.id == topic.id).values(dirty=True))

    def rewrite(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = "".join(str(getattr(p, "content", "")) for msg in messages for p in msg.parts)
        assert "香山吧" not in prompt
        args = {"title": "爬山", "summary": "讨论去哪座山", "keywords": ["山"]}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(args))])

    with llm.override_models(memory_topic=_model(rewrite)):
        stats = await consolidate_group(TOPIC_GROUP)
    assert stats["recomputed"] == 1
    async with get_session() as session:
        topic_id = topic.id
        topic = await session.get(Topic, topic_id)
        linked = set(await session.scalars(select(TopicMessage.message_id).where(TopicMessage.topic_id == topic_id)))
    assert topic is not None
    assert not topic.dirty
    assert linked == set(ids[:2])
    assert topic.msg_count == 2
    assert topic.title == "爬山"

    # The recall shrank it below the threshold; tidying archives it (and leaves open topics alone).
    async with get_session() as session:
        assert await archive_closed(session, TOPIC_GROUP) == 1
    async with get_session() as session:
        assert await session.scalar(select(Topic.status).where(Topic.id == topic_id)) == "archived"


async def _topic(group_id: int, msg_ids: list[int], *, closed_ago: timedelta | None) -> int:
    async with get_session() as session:
        sent = dict((await session.execute(select(Message.id, Message.sent_at).where(Message.id.in_(msg_ids)))).all())
        topic = Topic(
            group_id=group_id,
            title="话题",
            summary="摘要",
            keywords=[],
            participants=[1],
            start_at=min(sent.values()),
            end_at=max(sent.values()),
            first_msg_id=min(msg_ids),
            last_msg_id=max(msg_ids),
            msg_count=len(msg_ids),
            status="open" if closed_ago is None else "closed",
            closed_at=None if closed_ago is None else datetime.now(UTC) - closed_ago,
        )
        session.add(topic)
        await session.flush()
        session.add_all(TopicMessage(topic_id=topic.id, message_id=i) for i in msg_ids)
        return topic.id


async def test_profiles_consume_topics_in_close_order(insert: Insert, group_settings) -> None:
    group_settings({PROFILE_GROUP: {"memory": {"profile_min_messages": 2}}})
    h, m = timedelta(hours=1), timedelta(minutes=1)
    # The climbing topic starts first (lower id) but is still open when the cat topic is consumed.
    climbing = await _topic(
        PROFILE_GROUP,
        [
            await insert(PROFILE_GROUP, 1, "我在练攀岩", ago=5 * h),
            await insert(PROFILE_GROUP, 1, "攀岩馆周末人多", ago=h),
        ],
        closed_ago=None,
    )
    await _topic(
        PROFILE_GROUP,
        [
            await insert(PROFILE_GROUP, 1, "我养了一只猫", ago=4 * h),
            await insert(PROFILE_GROUP, 1, "猫又打翻水杯", ago=3 * h),
        ],
        closed_ago=30 * m,
    )
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append("".join(str(getattr(p, "content", "")) for msg in messages for p in msg.parts))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps({"summary": "群友"}))])

    async def run() -> int:
        with llm.override_models(memory_profile=_model(respond)):
            return (await profiles.update_group(PROFILE_GROUP))["updated"]

    assert await run() == 1
    assert "猫" in prompts[-1]
    assert "攀岩" not in prompts[-1]

    async with get_session() as session:
        await session.execute(
            update(Topic).where(Topic.id == climbing).values(status="closed", closed_at=datetime.now(UTC) - 10 * m)
        )
    assert await run() == 1
    assert "攀岩" in prompts[-1]
    assert "猫" not in prompts[-1]
    assert await run() == 0


async def test_glossary_mining_catches_up_past_one_batch(
    insert: Insert, group_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_settings({GLOSSARY_GROUP: {}})
    monkeypatch.setattr(glossary, "MINE_BATCH", 40)
    monkeypatch.setattr(glossary, "MINE_MIN_MESSAGES", 10)
    ids = [
        await insert(GLOSSARY_GROUP, 1 + i % 5, f"今晚开黑吗 第{i}局", ago=timedelta(minutes=200 - i))
        for i in range(100)
    ]
    await glossary.mine_group(GLOSSARY_GROUP)
    async with get_session() as session:
        state = await session.get(PipelineState, ("glossary", GLOSSARY_GROUP))
    assert state is not None
    assert state.last_msg_id == ids[-1]

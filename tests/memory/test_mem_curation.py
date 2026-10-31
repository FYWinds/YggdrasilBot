"""Memory curation (scores, merges, archiving, caps) and what archived items hide from: the profile tool, the
profile updater, search ranking and digests. Runs against the local ParadeDB; rows are removed afterwards."""

import json
import math
import re
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import delete, select

from tests.memory.helpers import fake_model, prompt_of
from yggdrasil.agent import tools
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.db.base import EMBED_DIM
from yggdrasil.db.models import (
    DailyDigest,
    EmbedSpace,
    MemoryVector,
    Message,
    Provider,
    Topic,
    TopicMessage,
    UserAlias,
    UserProfile,
    UserProfileFact,
)
from yggdrasil.db.session import get_session
from yggdrasil.memory import curation, digest, llm, profiles, search
from yggdrasil.memory.common import LOCAL_TZ, fact_weight, local, topic_weight
from yggdrasil.settings import Settings

GROUP = 990007
USER = 20003
NAMES = {20001: "阿明", 20002: "小红", 20003: "老王"}


async def _cleanup() -> None:
    async with get_session() as s:
        await s.execute(delete(MemoryVector).where(MemoryVector.group_id == GROUP))
        await s.execute(delete(DailyDigest).where(DailyDigest.group_id == GROUP))
        await s.execute(delete(Topic).where(Topic.group_id == GROUP))
        await s.execute(delete(UserProfileFact).where(UserProfileFact.group_id == GROUP))
        await s.execute(delete(UserProfile).where(UserProfile.group_id == GROUP))
        await s.execute(delete(UserAlias).where(UserAlias.group_id == GROUP))
        await s.execute(delete(Message).where(Message.group_id == GROUP))


@pytest.fixture
async def clean() -> AsyncIterator[None]:
    await _cleanup()
    yield
    await _cleanup()


def _answer(info: AgentInfo, output: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(output, ensure_ascii=False))])


_TOPIC_LINE = re.compile(r"^- \[(\d+)\] \S+「(.+?)」", re.M)  # a topic in the curation prompt: id, title
_FACT_LINE = re.compile(r"^- \[(\d+)\] (.+)$", re.M)  # a fact in the curation prompt: id, text

type Answer = Callable[[dict[str, int]], dict[str, Any]]


def _scripted(line: re.Pattern[str], prompts: list[str], *answers: Answer) -> FunctionModel:
    """Answers the n-th call with ``answers[n]`` applied to what that prompt showed (text -> id)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(prompt := prompt_of(messages))
        return _answer(info, answers[len(prompts) - 1]({text: int(i) for i, text in line.findall(prompt)}))

    return fake_model(respond)


def _made_up(shown: dict[str, int]) -> list[int]:
    """The ids a model numbering the items itself answers with: none of them was shown."""
    top = max(shown.values())
    return [top + n for n in range(1, len(shown) + 1)]


async def _topic(
    title: str,
    *,
    status: str = "closed",
    msgs: int = 30,
    people: Iterable[int] = (20001, 20002, 20003, 20004, 20005),
    ago: timedelta = timedelta(hours=1),
    **columns: Any,
) -> int:
    end = datetime.now(UTC) - ago
    async with get_session() as s:
        t = Topic(
            group_id=GROUP,
            title=title,
            summary=f"{title}的经过",
            keywords=[],
            participants=list(people),
            start_at=end - timedelta(hours=1),
            end_at=end,
            first_msg_id=1,
            last_msg_id=2,
            msg_count=msgs,
            status=status,
            closed_at=None if status == "open" else end,
            **columns,
        )
        s.add(t)
        await s.flush()
        return t.id


async def _fact(
    text: str,
    importance: int | None = None,
    *,
    sources: Iterable[int] = (1,),
    days: float = 0,
    archived: str | None = None,
    locked: bool = False,
) -> int:
    created = datetime.now(UTC) - timedelta(days=days)
    sources = list(sources)
    async with get_session() as s:
        f = UserProfileFact(
            group_id=GROUP,
            user_id=USER,
            fact=text,
            source_msg_ids=sources,
            importance=importance,
            weight=None if importance is None else fact_weight(importance, len(sources)),
            scored_at=None if importance is None else created,
            archived_at=created if archived else None,
            archive_reason=archived,
            locked=locked,
            created_at=created,
        )
        s.add(f)
        await s.flush()
        return f.id


async def _profile() -> None:
    async with get_session() as s:
        s.add(UserProfile(group_id=GROUP, user_id=USER, display_name="老王", summary="后端工程师"))


async def _facts() -> dict[int, UserProfileFact]:
    async with get_session() as s:
        return {f.id: f for f in await s.scalars(select(UserProfileFact).where(UserProfileFact.group_id == GROUP))}


async def _topics() -> dict[int, Topic]:
    async with get_session() as s:
        return {t.id: t for t in await s.scalars(select(Topic).where(Topic.group_id == GROUP))}


# --- topics ---------------------------------------------------------------------------------------


def _topic_scorer(importance: dict[str, int], seen: list[list[int]]) -> FunctionModel:
    """Rates topics by title."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        shown = _TOPIC_LINE.findall(prompt_of(messages))
        seen.append([int(i) for i, _ in shown])
        scores = [
            {"id": int(i), "importance": importance[title], "reason": f"{title}的理由"}
            for i, title in shown
            if title in importance
        ]
        return _answer(info, {"scores": scores})

    return fake_model(respond)


async def test_topic_curation_scores_closed_topics_in_capped_runs_and_archives_low_weight(
    clean: None, group_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_settings({GROUP: {}})
    monkeypatch.setattr(curation, "TOPIC_BATCH", 2)
    monkeypatch.setattr(curation, "MAX_TOPICS_PER_RUN", 2)
    # Created out of close order: the run must go by close time, not id.
    banter = await _topic("斗图", ago=timedelta(hours=2))
    release = await _topic("新版本发布", msgs=50, people=range(20001, 20010), ago=timedelta(hours=1))
    old = await _topic("旧闻", ago=timedelta(hours=3))
    ongoing = await _topic("进行中", status="open")
    small = await _topic("太小的话题", status="archived", msgs=5, people=[20001])
    # Reviewed before (e.g. restored by an admin): never rated or archived again.
    reviewed = await _topic("评过分的", importance=1, weight=1.5, scored_at=datetime.now(UTC))
    seen: list[list[int]] = []
    with llm.override_models(memory_topic=_topic_scorer({"新版本发布": 5, "斗图": 1, "旧闻": 3, "评过分的": 1}, seen)):
        first = await curation.curate_group(GROUP)
        second = await curation.curate_group(GROUP)
        third = await curation.curate_group(GROUP)

    # The backlog drains over runs, newest closed topics first.
    assert seen == [[release, banter], [old]]
    assert (first["topics_scored"], first["topics_archived"]) == (2, 1)
    assert (second["topics_scored"], second["topics_archived"]) == (1, 0)
    assert third["topics_scored"] == third["topics_archived"] == 0
    rows = await _topics()
    assert (rows[release].status, rows[release].importance) == ("closed", 5)
    assert rows[release].weight == topic_weight(5, 50, 9)
    assert rows[release].importance_reason == "新版本发布的理由"
    assert (rows[banter].status, rows[banter].importance) == ("archived", 1)
    assert rows[banter].weight == topic_weight(1, 30, 5)
    assert (rows[old].status, rows[old].importance) == ("closed", 3)
    assert (rows[ongoing].status, rows[ongoing].scored_at) == ("open", None)
    assert (rows[small].status, rows[small].scored_at) == ("archived", None)
    assert rows[reviewed].status == "closed"


async def test_topics_an_answer_rates_nothing_of_or_leaves_out_stay_unrated_for_the_next_run(
    clean: None, group_settings: Any
) -> None:
    group_settings({GROUP: {}})
    release = await _topic("新版本发布", ago=timedelta(hours=1))
    fix = await _topic("构建修好了", ago=timedelta(hours=2))
    banter = await _topic("斗图", ago=timedelta(hours=3))
    prompts: list[str] = []
    model = _scripted(
        _TOPIC_LINE,
        prompts,
        lambda shown: {"scores": []},
        lambda shown: {"scores": [{"id": i, "importance": 5} for i in _made_up(shown)]},
        lambda shown: {"scores": [{"id": shown["新版本发布"], "importance": 5}]},  # leaves two out
        lambda shown: {"scores": [{"id": i, "importance": 4} for i in shown.values()]},
    )
    with llm.override_models(memory_topic=model):
        empty = await curation.curate_group(GROUP)
        made_up = await curation.curate_group(GROUP)
        after_failures = await _topics()
        partial = await curation.curate_group(GROUP)
        after_partial = await _topics()
        rest = await curation.curate_group(GROUP)

    # Answers rating none of the shown topics are failures, and nothing is marked.
    assert (empty["failed"], empty["topics_scored"]) == (1, 0)
    assert (made_up["failed"], made_up["topics_scored"]) == (1, 0)
    assert all((t.status, t.importance, t.scored_at) == ("closed", None, None) for t in after_failures.values())
    # A partial answer rates what it covers; the rest stays unrated and is asked about again.
    assert (partial["failed"], partial["topics_scored"]) == (0, 1)
    assert after_partial[release].importance == 5
    assert after_partial[release].scored_at is not None
    assert all((after_partial[i].importance, after_partial[i].scored_at) == (None, None) for i in (fix, banter))
    assert [title for _, title in _TOPIC_LINE.findall(prompts[2])] == ["新版本发布", "构建修好了", "斗图"]
    assert [title for _, title in _TOPIC_LINE.findall(prompts[3])] == ["构建修好了", "斗图"]
    assert (rest["failed"], rest["topics_scored"]) == (0, 2)
    assert all(t.importance is not None for t in (await _topics()).values())


# --- facts ----------------------------------------------------------------------------------------


def _fact_reviewer(scores: dict[str, int], merges: list[tuple[str, list[str]]], prompts: list[str]) -> FunctionModel:
    """Rates facts by text and merges them by text; unknown texts become an id that was never shown."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = prompt_of(messages)
        prompts.append(prompt)
        ids = {text: int(i) for i, text in _FACT_LINE.findall(prompt)}
        return _answer(
            info,
            {
                "scores": [{"id": ids[t], "importance": v} for t, v in scores.items() if t in ids],
                "merges": [
                    {"keep": ids[k], "duplicates": [ids.get(d, 999_999_999) for d in dups]} for k, dups in merges
                ],
            },
        )

    return fake_model(respond)


async def test_fact_curation_merges_duplicates_archives_low_ones_and_caps_by_decayed_weight(
    clean: None, group_settings: Any
) -> None:
    group_settings({GROUP: {"memory": {"facts_per_user": 3}}})
    await _profile()
    engineer = await _fact("是后端工程师", sources=[11])
    backend = await _fact("做后端开发", sources=[12, 11])
    meal = await _fact("昨天吃了花菜", sources=[13])
    rust = await _fact("在学 Rust", sources=[14])
    new = await _fact("用 Neovim 写代码", 3)
    old = await _fact("用 Arch Linux", 3, days=400)  # the higher id: without decay it would win the tie
    cat = await _fact("喜欢猫", 2, locked=True)
    gone = await _fact("住在火星", 5, archived="low")
    prompts: list[str] = []
    model = _fact_reviewer(
        {"是后端工程师": 5, "做后端开发": 4, "昨天吃了花菜": 1, "在学 Rust": 4},
        [("是后端工程师", ["做后端开发", "喜欢猫", "不存在的事实"])],
        prompts,
    )
    with llm.override_models(memory_profile=model):
        stats = await curation.curate_group(GROUP)
        again = await curation.curate_group(GROUP)

    assert len(prompts) == 1  # everything is rated after one run
    to_rate, known = prompts[0].split("已评分")
    assert all(t in to_rate for t in ("是后端工程师", "做后端开发", "昨天吃了花菜", "在学 Rust"))
    assert all(t in known for t in ("用 Arch Linux", "用 Neovim 写代码", "喜欢猫"))
    assert "住在火星" not in prompts[0]
    facts = await _facts()
    # Merged: one fact keeps the union of the sources and the higher importance; the duplicate is archived.
    assert (facts[engineer].importance, facts[engineer].source_msg_ids) == (5, [11, 12])
    assert facts[engineer].weight == fact_weight(5, 2)
    assert facts[engineer].archived_at is None
    assert facts[backend].archive_reason == "duplicate"
    assert (facts[meal].importance, facts[meal].archive_reason) == (1, "low")
    assert (facts[rust].importance, facts[rust].archived_at) == (4, None)
    # Over the cap of 3: of two facts rated 3 the old one goes; the locked one stays (and isn't merged away).
    assert facts[old].archive_reason == "cap"
    assert facts[new].archived_at is None
    assert (facts[cat].archived_at, facts[cat].locked) == (None, True)
    assert facts[gone].archive_reason == "low"
    assert stats == {
        "topics_scored": 0,
        "topics_archived": 0,
        "facts_scored": 4,
        "facts_merged": 1,
        "facts_archived": 1,
        "facts_capped": 1,
        "failed": 0,
    }
    assert again["facts_capped"] == 0


async def test_facts_an_answer_rates_nothing_of_or_leaves_out_stay_unrated_and_uncapped(
    clean: None, group_settings: Any
) -> None:
    # A cap of 2 archives by age alone if facts the model never rated counted as rated.
    group_settings({GROUP: {"memory": {"facts_per_user": 2}}})
    await _profile()
    engineer = await _fact("是后端工程师", 5, sources=[11])
    rust = await _fact("在学 Rust", sources=[12])
    vim = await _fact("用 Neovim 写代码", sources=[13])
    backend = await _fact("做后端开发", sources=[14])
    prompts: list[str] = []
    model = _scripted(
        _FACT_LINE,
        prompts,
        lambda shown: {},
        lambda shown: {
            "scores": [{"id": i, "importance": 4} for i in _made_up(shown)],
            "merges": [{"keep": _made_up(shown)[0], "duplicates": _made_up(shown)[1:]}],
        },
        lambda shown: {  # leaves one out
            "scores": [{"id": shown["在学 Rust"], "importance": 4}],
            "merges": [{"keep": shown["是后端工程师"], "duplicates": [shown["做后端开发"]]}],
        },
        lambda shown: {"scores": [{"id": shown["用 Neovim 写代码"], "importance": 3}]},
    )
    with llm.override_models(memory_profile=model):
        empty = await curation.curate_group(GROUP)
        made_up = await curation.curate_group(GROUP)
        after_failures = await _facts()
        partial = await curation.curate_group(GROUP)
        after_partial = await _facts()
        rest = await curation.curate_group(GROUP)
    facts = await _facts()

    # Answers rating or merging none of the shown facts are failures: nothing is marked, nothing capped.
    for stats in (empty, made_up):
        assert (stats["failed"], stats["facts_scored"], stats["facts_merged"], stats["facts_capped"]) == (1, 0, 0, 0)
    for i in (rust, vim, backend):
        assert (after_failures[i].importance, after_failures[i].scored_at, after_failures[i].archived_at) == (
            None,
            None,
            None,
        )
    assert after_failures[engineer].archived_at is None
    # A partial answer rates and merges what it covers; the fact it left out stays unrated, so no cap yet.
    assert (partial["failed"], partial["facts_scored"], partial["facts_merged"], partial["facts_capped"]) == (
        0,
        1,
        1,
        0,
    )
    assert after_partial[rust].importance == 4
    assert after_partial[rust].scored_at is not None
    assert (after_partial[engineer].importance, after_partial[engineer].source_msg_ids) == (5, [11, 14])
    assert after_partial[backend].archive_reason == "duplicate"
    assert (after_partial[vim].importance, after_partial[vim].scored_at) == (None, None)
    # The next run asks only about it; once everything is rated the cap applies.
    to_rate, known = prompts[3].split("已评分")
    assert [text for _, text in _FACT_LINE.findall(to_rate)] == ["用 Neovim 写代码"]
    assert sorted(text for _, text in _FACT_LINE.findall(known)) == sorted(["是后端工程师", "在学 Rust"])
    assert (rest["failed"], rest["facts_scored"], rest["facts_capped"]) == (0, 1, 1)
    assert (facts[vim].importance, facts[vim].archive_reason) == (3, "cap")
    assert all(facts[i].archived_at is None for i in (engineer, rust))


def _ctx() -> RunContext[Deps]:
    deps = Deps(cast(Any, None), GROUP, 20001, Settings(), Handles(), "chat", self_id=10000)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def test_get_profile_shows_the_ten_highest_weighted_active_facts(clean: None, group_settings: Any) -> None:
    group_settings({GROUP: {}})
    await _profile()
    # Distinct weights: importance × (1 + 0.1·ln(sources)); an unrated fact ranks like importance 3, one source.
    rated = [(5, 3), (5, 2), (5, 1), (4, 3), (4, 2), (4, 1), (3, 3), (3, 2), (2, 3), (2, 2), (2, 1), (1, 1)]
    for importance, n in reversed(rated):
        await _fact(f"重要度{importance}，{n}条佐证", importance, sources=range(n))
    await _fact("还没评分")
    await _fact("归档了的身份", 5, sources=range(3), archived="duplicate")
    top = [f"重要度{i}，{n}条佐证" for i, n in rated[:8]] + ["还没评分", "重要度2，3条佐证"]

    profile = await search.get_profile(GROUP, USER)
    assert profile is not None
    assert profile.facts == top

    out = str(await tools.get_profile(_ctx(), USER))
    assert out.split("事实：\n", 1)[1].splitlines() == [f"- {f}" for f in top]
    assert "归档了的身份" not in out


async def _message(user_id: int, text: str, at: datetime) -> int:
    async with get_session() as s:
        m = Message(
            sent_at=at,
            group_id=GROUP,
            user_id=user_id,
            qq_message_id=int(at.timestamp()) % 1_000_000 + user_id,
            sender_name=NAMES.get(user_id, "群友"),
            plain_text=text,
            segments=[],
        )
        s.add(m)
        await s.flush()
        return m.id


async def test_profile_update_sees_only_active_facts_and_stores_rated_new_ones(
    clean: None, group_settings: Any
) -> None:
    group_settings({GROUP: {"memory": {"profile_min_messages": 1}}})
    await _profile()
    engineer = await _fact("是后端工程师", 5)
    arch = await _fact("用 Arch Linux", 3)
    cat = await _fact("喜欢猫", 2, locked=True)
    await _fact("住在火星", 5, archived="cap")
    said = await _message(USER, "我最近在维护一个开源的 QQ 机器人", datetime.now(UTC) - timedelta(hours=2))
    topic = await _topic("开源项目", ago=timedelta(hours=1))
    async with get_session() as s:
        s.add(TopicMessage(topic_id=topic, message_id=said))
        t = await s.get(Topic, topic)
        assert t is not None
        t.start_at = datetime.now(UTC) - timedelta(hours=3)
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(prompt_of(messages))
        return _answer(
            info,
            {
                "summary": "后端工程师，在维护开源机器人",
                "new_facts": [
                    {"fact": "在维护一个开源的 QQ 机器人", "source_msg_ids": [said], "importance": 5},
                    {"fact": "今天中午吃了面", "source_msg_ids": [said], "importance": 1},
                    {"fact": "没给重要度的事实", "source_msg_ids": [said]},
                ],
                "remove_fact_ids": [arch, cat],
            },
        )

    with llm.override_models(memory_profile=fake_model(respond)):
        assert await profiles.update_user(GROUP, USER)

    shown = prompts[0].split("<profile>", 1)[1]
    assert all(t in shown for t in ("是后端工程师", "用 Arch Linux", "喜欢猫"))
    assert "住在火星" not in shown
    facts = {f.fact: f for f in (await _facts()).values()}
    project = facts["在维护一个开源的 QQ 机器人"]
    assert (project.importance, project.weight, project.archived_at) == (5, fact_weight(5, 1), None)
    assert project.scored_at is not None
    assert (facts["今天中午吃了面"].importance, facts["今天中午吃了面"].archive_reason) == (1, "low")
    # Unrated: left to curation.
    assert (facts["没给重要度的事实"].scored_at, facts["没给重要度的事实"].archived_at) == (None, None)
    # Removed facts are archived, not deleted; a locked one stays.
    assert facts["用 Arch Linux"].archive_reason == "outdated"
    assert facts["喜欢猫"].archived_at is None
    assert facts["是后端工程师"].id == engineer
    assert facts["是后端工程师"].archived_at is None


# --- search ranking -------------------------------------------------------------------------------


def _unit(cos: float) -> list[float]:
    """A unit vector at cosine ``cos`` to the first axis."""
    vec = [0.0] * EMBED_DIM
    vec[0], vec[1] = cos, math.sqrt(1 - cos * cos)
    return vec


@pytest.fixture
async def space(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[int]:
    """An embedding space whose query vector is the first axis: a vector's cosine to the query is its x."""
    async with get_session() as s:
        provider = Provider(name="test-curation-embed", base_url="http://embed.example.com/v1")
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


async def test_search_ranks_topic_hits_by_weight_and_skips_archived_topics(
    clean: None, group_settings: Any, space: int
) -> None:
    group_settings({GROUP: {}})

    async def indexed(title: str, cos: float, importance: int, status: str = "closed") -> int:
        topic_id = await _topic(
            title,
            status=status,
            importance=importance,
            weight=topic_weight(importance, 30, 5),
            scored_at=datetime.now(UTC),
        )
        async with get_session() as s:
            s.add(MemoryVector(kind="topic", ref_id=topic_id, space_id=space, group_id=GROUP, embedding=_unit(cos)))
        return topic_id

    banter = await indexed("泰山玩梗", 0.72, 2)
    plan = await indexed("泰山路线定了", 0.70, 5)
    await indexed("泰山旧帖", 0.9, 1, status="archived")

    hits = await search.search_history(GROUP, "泰山")
    assert [(h.kind, h.ref_id) for h in hits] == [("topic", plan), ("topic", banter)]


# --- daily digest -----------------------------------------------------------------------------------


async def test_digest_is_rendered_in_the_fixed_format_from_non_archived_topics(
    clean: None, group_settings: Any
) -> None:
    group_settings({GROUP: {}})
    day = local(datetime.now(UTC)).date() - timedelta(days=1)
    noon = datetime.combine(day, time(12), LOCAL_TZ)
    async with get_session() as s:
        s.add_all(UserAlias(group_id=GROUP, user_id=u, name=n) for u, n in NAMES.items())
    for i in range(3):
        await _message(20001, f"第{i}条", noon + timedelta(minutes=i))
    release = await _topic("新版本发布", msgs=84, people=[20003, 20001, 20002], ago=datetime.now(UTC) - noon)
    hidden = await _topic("刷屏", status="archived", ago=datetime.now(UTC) - noon - timedelta(hours=1))
    fix = await _topic("修好了构建", msgs=12, people=[20002], ago=datetime.now(UTC) - noon - timedelta(hours=2))
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(prompt_of(messages))
        return _answer(
            info,
            {
                "overview": "**以版本发布为主**。",
                "events": [
                    {
                        "title": "**新版本发布**",
                        "topic_ids": [release, hidden],
                        "participants": [20003, 20001],
                        "what": "老王贴了更新日志，大家讨论升级",
                        "conclusion": "下周升级",
                    },
                    {"title": "构建修复", "topic_ids": [fix], "what": "小红修好了构建。"},
                ],
            },
        )

    with llm.override_models(memory_digest=fake_model(respond)):
        digest_id = await digest.build_digest(GROUP, day)

    assert digest_id is not None
    assert "刷屏" not in prompts[0]
    async with get_session() as s:
        row = await s.get(DailyDigest, digest_id)
    assert row is not None
    weekday = "一二三四五六日"[day.weekday()]
    assert row.summary == (
        f"{day:%Y-%m-%d}（周{weekday}）共 3 条消息。以版本发布为主。\n"
        "1. 新版本发布（84 条）：老王贴了更新日志，大家讨论升级。结论：下周升级。参与：老王、阿明。\n"
        "2. 构建修复（12 条）：小红修好了构建。参与：小红。"
    )
    assert row.topic_ids == [release, fix]

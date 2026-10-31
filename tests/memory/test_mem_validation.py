from dataclasses import replace
from datetime import date

from tests.memory.helpers import mv
from yggdrasil.db.models import Topic
from yggdrasil.memory.curation import (
    FactMerge,
    FactScore,
    TopicScore,
    valid_fact_scores,
    valid_merges,
    valid_topic_scores,
)
from yggdrasil.memory.digest import DigestDraft, DigestEvent, render_digest
from yggdrasil.memory.glossary import TermDecision, valid_decisions
from yggdrasil.memory.profiles import NewFact, ProfileUpdate, validate_update
from yggdrasil.memory.topics import ContinueTopic, NewTopic, clean_summary, clean_title, participants_of, validate_ops


def test_topic_ops_drop_ids_outside_window_and_duplicates() -> None:
    ops = [
        NewTopic(title="吃饭", summary="s", message_ids=[1, 2, 99, 2]),
        ContinueTopic(topic_id=7, message_ids=[2, 3]),  # 2 already taken by the first op
        ContinueTopic(topic_id=8, message_ids=[4]),  # unknown topic
        NewTopic(title="  ", summary="s", message_ids=[5]),  # untitled
        NewTopic(title="空", summary="s", message_ids=[100, 101]),  # nothing left
    ]
    out = validate_ops(ops, window_ids=[1, 2, 3, 4, 5], open_topic_ids=[7])
    assert [(type(o).__name__, o.message_ids) for o in out] == [("NewTopic", [1, 2]), ("ContinueTopic", [3])]


def test_participants_are_window_speakers_only() -> None:
    a, b = mv(0, user=1), mv(1, user=2)
    bot = replace(mv(2, user=3), is_self=True)
    views = {v.id: v for v in (a, b, bot)}
    # Claimed user 2 spoke in the window; 42 never spoke; the bot (3) is never a participant.
    assert participants_of([a.id, bot.id], views, [2, 42, 3]) == [1, 2]
    assert participants_of([b.id], views, existing=[1]) == [1, 2]


def test_profile_facts_need_shown_sources() -> None:
    update = ProfileUpdate(
        summary=" 爱猫 ",
        interests=["猫", " "],
        new_facts=[
            NewFact(fact="养了一只橘猫", source_msg_ids=[10, 999]),
            NewFact(fact="住在火星", source_msg_ids=[999]),
        ],
        remove_fact_ids=[5, 6],
    )
    out = validate_update(update, shown_msg_ids=[10, 11], fact_ids=[5])
    assert out.summary == "爱猫"
    assert out.interests == ["猫"]
    assert [(f.fact, f.source_msg_ids) for f in out.new_facts] == [("养了一只橘猫", [10])]
    assert out.remove_fact_ids == [5]


def test_new_facts_keep_their_importance_clamped_to_one_to_five() -> None:
    update = ProfileUpdate(
        summary="s",
        new_facts=[
            NewFact(fact="是后端工程师", source_msg_ids=[10], importance=9),
            NewFact(fact="在学 Rust", source_msg_ids=[10]),
        ],
    )
    out = validate_update(update, shown_msg_ids=[10], fact_ids=[])
    assert [(f.fact, f.importance) for f in out.new_facts] == [("是后端工程师", 5), ("在学 Rust", None)]


def test_topic_titles_collapse_repeated_prefixes() -> None:
    # The prod case: a title update stacked the old title in front of the new one.
    assert (
        clean_title("zssm工具与折叠消息闹剧：zssm工具与折叠消息闹剧：tldr 玩梗") == "zssm工具与折叠消息闹剧：tldr 玩梗"
    )
    assert clean_title("周末聚会：周末聚会") == "周末聚会"
    assert clean_title(" 版本发布:  版本发布：3.14 \n 正式版 ") == "版本发布：3.14 正式版"
    assert clean_title("周六 14:00 聚会") == "周六 14:00 聚会"
    assert clean_title("：") == ""
    assert clean_title("长" * 80) == "长" * 60
    title = "zssm工具与折叠消息闹剧"
    assert clean_summary(f"{title}：{title}：甲问 tldr 是什么", title) == "甲问 tldr 是什么"
    assert clean_summary(f" {title}。", title) == f"{title}。"


def test_digest_renders_one_fixed_plain_text_format() -> None:
    topics = {
        1: Topic(id=1, msg_count=60, participants=[20001, 20002, 20003, 20004, 20005, 20006]),
        2: Topic(id=2, msg_count=24, participants=[20002]),
        3: Topic(id=3, msg_count=12, participants=[20003, 20004]),
    }
    names = {20001: "阿明", 20002: "小红", 20003: "老王", 20004: "Neko", 20005: "路人甲", 20006: "新来的"}
    draft = DigestDraft(
        overview="**以技术讨论为主**，夹杂一些闲聊",
        events=[
            DigestEvent(
                title="**新版本发布**",
                topic_ids=[1, 2, 1, 99],
                participants=[20003, 424242, 20001, 20002, 20004, 20005, 20006],
                what="老王贴了更新日志，\n大家讨论了升级的坑",
                conclusion="下周统一升级。",
            ),
            DigestEvent(title="排查构建失败", topic_ids=[3], participants=[], what="定位到缓存配置错误！"),
            DigestEvent(title="不存在的话题", topic_ids=[42], what="编的"),
        ],
    )
    assert render_digest(date(2026, 10, 30), 3130, draft, topics, names) == (
        "2026-10-30（周五）共 3130 条消息。以技术讨论为主，夹杂一些闲聊。\n"
        "1. 新版本发布（84 条）：老王贴了更新日志， 大家讨论了升级的坑。结论：下周统一升级。"
        "参与：老王、阿明、小红、Neko、路人甲。\n"
        "2. 排查构建失败（12 条）：定位到缓存配置错误！参与：老王、Neko。"
    )
    assert render_digest(date(2026, 10, 30), 5, DigestDraft(overview=" ", events=[]), topics, names) is None


def test_curation_answers_only_touch_what_was_shown() -> None:
    scores = [
        TopicScore(id=1, importance=9, reason="  发布了\n新版本 "),
        TopicScore(id=1, importance=1),
        TopicScore(id=5, importance=3),
    ]
    assert {i: (s.importance, s.reason) for i, s in valid_topic_scores(scores, [1, 2]).items()} == {
        1: (5, "发布了 新版本")
    }
    assert valid_fact_scores([FactScore(id=1, importance=0), FactScore(id=7, importance=3)], [1, 2]) == {1: 1}
    merges = [
        FactMerge(keep=1, duplicates=[2, 2, 1, 9, 4]),  # 9 unknown, 4 locked
        FactMerge(keep=2, duplicates=[3]),  # 2 already merged away
        FactMerge(keep=3, duplicates=[5]),
    ]
    assert valid_merges(merges, fact_ids=[1, 2, 3, 4, 5], locked=[4]) == {1: [2], 3: [5]}


def test_glossary_decisions_only_for_candidates() -> None:
    decisions = [
        TermDecision(term="鸽了", accept=True, definition="爽约", aliases=["鸽", "鸽了", "咕"]),
        TermDecision(term="鸽了", accept=False),  # duplicate decision ignored
        TermDecision(term="不存在", accept=True, definition="x"),
        TermDecision(term="真香", accept=True, definition=" "),  # accepted without definition
        TermDecision(term="这波", accept=False),
    ]
    out = valid_decisions(decisions, ["鸽了", "真香", "这波"])
    assert [(d.term, d.accept, d.aliases) for d in out] == [("鸽了", True, ["鸽", "咕"]), ("这波", False, [])]

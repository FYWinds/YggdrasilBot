from dataclasses import replace

from tests.memory.helpers import mv
from yggdrasil.memory.glossary import TermDecision, valid_decisions
from yggdrasil.memory.profiles import NewFact, ProfileUpdate, validate_update
from yggdrasil.memory.topics import ContinueTopic, NewTopic, participants_of, validate_ops


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

from datetime import timedelta

from tests.memory.helpers import T0, mv
from yggdrasil.memory.chunking import split_chunks
from yggdrasil.people import People

GAP = timedelta(minutes=20)
LATER = T0 + timedelta(days=1)


def split(messages, *, now=LATER, max_messages=60, max_chars=2400, more_pending=False):
    return split_chunks(
        messages,
        People("机器人"),
        gap=GAP,
        max_messages=max_messages,
        max_chars=max_chars,
        now=now,
        more_pending=more_pending,
    )


def test_gap_splits_and_old_tail_closes() -> None:
    msgs = [mv(0), mv(5), mv(25.5), mv(30)]
    plan = split(msgs)
    assert [[m.id for m in c.messages] for c in plan.chunks] == [[msgs[0].id, msgs[1].id], [msgs[2].id, msgs[3].id]]
    assert plan.watermark == msgs[-1].id


def test_gap_of_exactly_the_limit_does_not_split() -> None:
    msgs = [mv(0), mv(20)]
    assert len(split(msgs).chunks) == 1


def test_recent_tail_stays_open() -> None:
    msgs = [mv(0), mv(1), mv(40), mv(41)]
    plan = split(msgs, now=msgs[-1].sent_at + timedelta(minutes=5))
    assert len(plan.chunks) == 1
    assert plan.chunks[0].last_id == msgs[1].id
    assert plan.watermark == msgs[1].id  # the tail is re-read next run


def test_tail_open_when_more_messages_pending() -> None:
    msgs = [mv(0), mv(1)]
    plan = split(msgs, more_pending=True)
    assert plan.chunks == []
    assert plan.watermark is None


def test_max_messages_cut() -> None:
    msgs = [mv(i) for i in range(5)]
    plan = split(msgs, max_messages=2)
    assert [len(c.messages) for c in plan.chunks] == [2, 2, 1]
    open_tail = split(msgs, max_messages=2, now=msgs[-1].sent_at)
    assert [len(c.messages) for c in open_tail.chunks] == [2, 2]
    assert open_tail.watermark == msgs[3].id


def test_max_chars_cut_and_long_line_truncated() -> None:
    msgs = [mv(0, "a" * 30), mv(1, "b" * 30), mv(2, "c" * 500)]
    plan = split(msgs, max_chars=120)  # each short line is 54 chars ("[10-01 12:00] 甲(10001): " + 30)
    assert [len(c.messages) for c in plan.chunks] == [2, 1]
    assert all(len(c.text) <= 120 for c in plan.chunks)


def test_empty_messages_skipped_but_consumed() -> None:
    msgs = [mv(0, ""), mv(1, "有内容"), mv(2, "  ")]
    plan = split(msgs)
    assert [len(c.messages) for c in plan.chunks] == [1]
    assert plan.watermark == msgs[-1].id

    leading_empty = [mv(0, ""), mv(1, "进行中")]
    open_plan = split(leading_empty, now=leading_empty[-1].sent_at)
    assert open_plan.chunks == []
    assert open_plan.watermark == leading_empty[0].id


def test_chunk_text_format_uses_shanghai_time() -> None:
    plan = split([mv(0, "你好", name="小明")])
    assert plan.chunks[0].text == "[10-01 12:00] 小明(10001): 你好"

from yggdrasil.ingest.writer import _clean_name, _strip_nul  # pyright: ignore[reportPrivateUsage]


def test_snowluma_protobuf_card_becomes_plain_name():
    # Seen in prod: a rich group card passed through as raw protobuf; the NUL stalled all ingest.
    assert _clean_name("\x12\nH-xiaoH | \n\x0c\n\napex-query\x10\x00") == "H-xiaoH | apex-query"


def test_nul_removed_from_nested_segments_but_newlines_kept():
    segments = [{"type": "text", "data": {"text": "a\x00b\nc"}}, {"type": "node", "data": {"content": ["x\x00"]}}]
    assert _strip_nul(segments) == [
        {"type": "text", "data": {"text": "ab\nc"}},
        {"type": "node", "data": {"content": ["x"]}},
    ]

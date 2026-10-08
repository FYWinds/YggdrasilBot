from yggdrasil.memory.glossary import acceptable, clean, mine_candidates


def _recent(texts: list[str]) -> list[tuple[int, str]]:
    return list(enumerate(texts, start=1))


def test_mining_keeps_new_words_and_spiking_ngrams() -> None:
    recent = _recent(
        ["这波操作yyds"] * 6
        + ["电子榨菜真香"] * 6
        + ["哈哈哈哈"] * 10
        + ["我们去吃饭吧"] * 10
        + ["今天又被老板PUA了"] * 2
    )
    history = ["我们去吃饭吧"] * 50 + ["随便聊聊"] * 50
    found = {c.term: c for c in mine_candidates(recent, history, min_count=5, limit=50, exclude=["YYDS"])}

    assert "真香" in found
    assert found["真香"].oov
    assert "电子榨菜" in found
    assert not found["电子榨菜"].oov
    assert "yyds" not in found  # already a glossary term (case-insensitive)
    assert "哈哈哈哈" not in found  # laughter
    assert "去吃饭" not in found  # frequent in history: no spike
    assert "PUA" not in found  # below min_count
    assert not any(t in found for t in ("我们", "吧"))  # stopwords
    assert found["真香"].count == 6
    assert found["真香"].examples == [7, 8, 9, 10, 11]  # capped at 5 example ids


def test_counts_are_per_message_and_ngrams_with_unknown_tokens_skipped() -> None:
    recent = _recent(["真香真香真香"] + ["电子榨菜真香"] * 4)
    found = {c.term: c.count for c in mine_candidates(recent, [], min_count=5, limit=10)}
    assert found == {"真香": 5}


def test_placeholders_mentions_and_urls_are_ignored() -> None:
    assert clean("[图片]@小明 看 https://e.com/x [表情:doge]").split() == ["看"]
    assert not acceptable("的话")  # stop char at the boundary
    assert not acceptable("2333")
    assert not acceptable("ok")
    assert acceptable("绝绝子")

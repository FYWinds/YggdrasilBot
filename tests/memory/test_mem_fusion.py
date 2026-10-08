import pytest

from yggdrasil.memory.search import rrf_fuse


def test_rrf_scores_and_order() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["c", "a"]], k=60)
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert [key for key, _ in fused] == ["a", "c", "b"]


def test_rrf_duplicate_in_one_ranking_counts_once_at_best_rank() -> None:
    scores = dict(rrf_fuse([["x", "y", "x"]], k=60))
    assert scores["x"] == pytest.approx(1 / 61)
    assert scores["y"] == pytest.approx(1 / 62)


def test_rrf_ties_keep_first_seen_order_and_empty_inputs() -> None:
    assert [key for key, _ in rrf_fuse([["p"], ["q"]])] == ["p", "q"]
    assert rrf_fuse([[], []]) == []


def test_rrf_item_in_both_lists_beats_top_of_one() -> None:
    # Rank 2 in both lists outranks rank 1 in a single list.
    fused = rrf_fuse([["solo", "both"], ["other", "both"]])
    assert fused[0][0] == "both"

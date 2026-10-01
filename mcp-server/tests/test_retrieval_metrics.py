"""Unit tests for the retrieval metrics shared by the baseline and eval harness."""

from __future__ import annotations

import pytest

from tests.retrieval_metrics import evidence_recall, first_hit_rank, group_ranks

RANKED = ["a", "b", "c", "d"]


class TestFirstHitRank:
    def test_single_group_is_rank_of_first_alternative(self) -> None:
        assert first_hit_rank(RANKED, [["d", "b"]]) == 2

    def test_any_group_counts(self) -> None:
        # Hit rate and MRR ask "did anything relevant surface, and how
        # high?", so the first member of any group sets the rank.
        assert first_hit_rank(RANKED, [["d"], ["c"]]) == 3

    def test_miss_is_none(self) -> None:
        assert first_hit_rank(RANKED, [["x"], ["y"]]) is None


class TestGroupRanks:
    def test_best_rank_per_group(self) -> None:
        assert group_ranks(RANKED, [["d", "b"], ["c"], ["x"]]) == [2, 3, None]


class TestEvidenceRecall:
    def test_alternatives_satisfy_one_group(self) -> None:
        assert evidence_recall(RANKED, [["x", "c"]], k=10) == 1.0

    def test_jointly_required_groups_are_each_counted(self) -> None:
        # One hit is a full hit-rate score but only half the evidence.
        assert evidence_recall(RANKED, [["a"], ["x"]], k=10) == 0.5

    def test_cutoff_applies(self) -> None:
        assert evidence_recall(RANKED, [["a"], ["d"]], k=3) == 0.5

    def test_no_groups_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            evidence_recall(RANKED, [], k=10)

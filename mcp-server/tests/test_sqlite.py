"""Tests for src.lib.sqlite Database query layer.

Covers pure fusion/filter logic against synthetic ThreadResult lists and
real read queries against an in-memory-style database seeded via conftest.
"""

import pytest
from src.lib.sqlite import Database


class TestReciprocalRankFusion:
    def test_single_list_preserves_ranking(self, seeded_db: Database, make_result):
        bm25 = [make_result("t1"), make_result("t2"), make_result("t3")]
        fused = seeded_db._reciprocal_rank_fusion(bm25, [])
        assert [r.thread_id for r in fused] == ["t1", "t2", "t3"]

    def test_thread_appearing_in_both_lists_scores_higher(self, seeded_db: Database, make_result):
        bm25 = [make_result("shared"), make_result("bm25-only")]
        vec = [make_result("shared"), make_result("vec-only")]
        fused = seeded_db._reciprocal_rank_fusion(bm25, vec)
        assert fused[0].thread_id == "shared"

    def test_fused_scores_are_non_increasing(self, seeded_db: Database, make_result):
        bm25 = [make_result(f"t{i}") for i in range(5)]
        vec = [make_result(f"t{i}") for i in range(4, -1, -1)]
        fused = seeded_db._reciprocal_rank_fusion(bm25, vec)
        scores = [r.score for r in fused]
        assert scores == sorted(scores, reverse=True)

    def test_empty_inputs_return_empty(self, seeded_db: Database):
        assert seeded_db._reciprocal_rank_fusion([], []) == []


class TestApplyFilters:
    def test_folder_filter(self, seeded_db: Database, make_result):
        results = [
            make_result("a", folder="INBOX"),
            make_result("b", folder="Archive"),
            make_result("c", folder="INBOX"),
        ]
        filtered = seeded_db._apply_filters(results, folders=["INBOX"])
        assert [r.thread_id for r in filtered] == ["a", "c"]

    def test_from_addr_substring_match_case_insensitive(self, seeded_db: Database, make_result):
        a = make_result("a")
        a.participants = ["Alice@EXAMPLE.com"]
        b = make_result("b")
        b.participants = ["bob@example.com"]
        filtered = seeded_db._apply_filters([a, b], from_addr="alice")
        assert [r.thread_id for r in filtered] == ["a"]

    def test_date_range_filters(self, seeded_db: Database, make_result):
        from datetime import UTC, datetime

        old = make_result("old")
        old.date_first = datetime(2023, 1, 1, tzinfo=UTC)
        old.date_last = datetime(2023, 1, 1, tzinfo=UTC)
        new = make_result("new")
        new.date_first = datetime(2024, 6, 1, tzinfo=UTC)
        new.date_last = datetime(2024, 6, 1, tzinfo=UTC)

        only_new = seeded_db._apply_filters([old, new], date_from="2024-01-01T00:00:00+00:00")
        assert [r.thread_id for r in only_new] == ["new"]

        only_old = seeded_db._apply_filters([old, new], date_to="2023-12-31T23:59:59+00:00")
        assert [r.thread_id for r in only_old] == ["old"]

    def test_has_attachments_filter(self, seeded_db: Database, make_result):
        with_att = make_result("att")
        with_att.has_attachments = True
        without = make_result("plain")
        filtered = seeded_db._apply_filters([with_att, without], has_attachments=True)
        assert [r.thread_id for r in filtered] == ["att"]
        filtered = seeded_db._apply_filters([with_att, without], has_attachments=False)
        assert [r.thread_id for r in filtered] == ["plain"]

    def test_invalid_date_raises(self, seeded_db: Database, make_result):
        with pytest.raises(ValueError, match="date_from"):
            seeded_db._apply_filters([make_result("a")], date_from="not-a-date")
        with pytest.raises(ValueError, match="date_to"):
            seeded_db._apply_filters([make_result("a")], date_to="also-bad")

    def test_iso8601_z_suffix_accepted(self, seeded_db: Database, make_result):
        seeded_db._apply_filters([make_result("a")], date_from="2024-01-01T00:00:00Z")


class TestKeywordSearch:
    def test_matches_body_token(self, seeded_db: Database):
        results = seeded_db.keyword_search("invoice")
        assert any(r.thread_id == "t-alpha" for r in results)

    def test_returns_empty_when_no_match(self, seeded_db: Database):
        assert seeded_db.keyword_search("nonexistentsearchtoken") == []

    def test_folder_filter_restricts_results(self, seeded_db: Database):
        # "meeting" appears only in the Archive thread
        all_results = seeded_db.keyword_search("meeting")
        assert any(r.folder == "Archive" for r in all_results)
        inbox_only = seeded_db.keyword_search("meeting", folders=["INBOX"])
        assert inbox_only == []

    def test_malformed_fts_query_returns_empty_not_raise(self, seeded_db: Database):
        # FTS5 treats an unbalanced quote as a syntax error; the layer is
        # expected to log and return [] rather than propagate.
        assert seeded_db.keyword_search('"unterminated') == []


class TestSemanticSearch:
    def test_nearest_neighbor_returned_first(self, seeded_db: Database):
        results = seeded_db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=3)
        assert results[0].thread_id == "t-alpha"

    def test_empty_db_returns_empty(self, empty_db: Database):
        assert empty_db.semantic_search([1.0, 0.0, 0.0, 0.0]) == []


class TestHybridSearch:
    def test_combines_keyword_and_vector_matches(self, seeded_db: Database):
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
        )
        assert results
        assert results[0].thread_id == "t-alpha"

    def test_filters_apply_after_fusion(self, seeded_db: Database):
        results = seeded_db.hybrid_search(
            query_text="meeting",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            folders=["INBOX"],
        )
        assert all(r.folder == "INBOX" for r in results)


class TestDirectLookups:
    def test_get_thread_returns_result(self, seeded_db: Database):
        thread = seeded_db.get_thread("t-alpha")
        assert thread is not None
        assert thread.subject == "invoice for march"
        assert thread.has_attachments is True

    def test_get_thread_missing_returns_none(self, seeded_db: Database):
        assert seeded_db.get_thread("does-not-exist") is None

    def test_get_thread_message_ids(self, seeded_db: Database):
        assert seeded_db.get_thread_message_ids("t-alpha") == ["t-alpha"]

    def test_get_thread_message_ids_missing(self, seeded_db: Database):
        assert seeded_db.get_thread_message_ids("missing") == []

    def test_find_thread_by_message_id(self, seeded_db: Database):
        assert seeded_db.find_thread_by_message_id("t-alpha") == "t-alpha"
        assert seeded_db.find_thread_by_message_id("missing") is None

    def test_list_threads_respects_folder_and_order(self, seeded_db: Database):
        inbox = seeded_db.list_threads(folder="INBOX")
        assert [r.thread_id for r in inbox] == ["t-beta", "t-alpha"]
        archive = seeded_db.list_threads(folder="Archive")
        assert [r.thread_id for r in archive] == ["t-gamma"]

    def test_list_threads_pagination(self, seeded_db: Database):
        first = seeded_db.list_threads(folder="INBOX", limit=1, offset=0)
        second = seeded_db.list_threads(folder="INBOX", limit=1, offset=1)
        assert [r.thread_id for r in first] == ["t-beta"]
        assert [r.thread_id for r in second] == ["t-alpha"]


class TestStatsAndFolders:
    def test_get_stats(self, seeded_db: Database):
        stats = seeded_db.get_stats()
        assert stats["total_threads"] == 3
        assert stats["total_messages"] == 3
        assert stats["oldest_message"] is not None
        assert stats["newest_message"] is not None

    def test_list_folders_ranked_by_thread_count(self, seeded_db: Database):
        folders = seeded_db.list_folders()
        names = [f["name"] for f in folders]
        assert names[0] == "INBOX"
        assert {"name": "Archive", "thread_count": 1} in folders


class TestValidateIso8601:
    def test_accepts_offset_form(self):
        Database._validate_iso8601("date_from", "2024-03-01T00:00:00+00:00")

    def test_accepts_z_suffix(self):
        Database._validate_iso8601("date_from", "2024-03-01T00:00:00Z")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError, match="date_from"):
            Database._validate_iso8601("date_from", "yesterday")

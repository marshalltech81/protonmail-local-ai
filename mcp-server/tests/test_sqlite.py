"""Tests for src.lib.sqlite Database query layer.

Covers pure fusion/filter logic against synthetic ThreadResult lists and
real read queries against an in-memory-style database seeded via conftest.
"""

import sqlite3
from contextlib import closing

import pytest
from src.lib.sqlite import Database


class TestReadOnlyConnection:
    def test_write_attempt_raises(self, seeded_db: Database):
        """The MCP reader opens SQLite via ``?mode=ro`` URI — any attempt
        to mutate the shared index must fail at the SQLite API level, not
        rely only on ``PRAGMA query_only`` being honored."""
        with closing(seeded_db._conn) as conn:
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
                conn.execute("UPDATE threads SET subject = 'hijacked' WHERE thread_id = 't-alpha'")

    def test_reads_still_work(self, seeded_db: Database):
        with closing(seeded_db._conn) as conn:
            row = conn.execute(
                "SELECT subject FROM threads WHERE thread_id = ?", ("t-alpha",)
            ).fetchone()
        assert row["subject"] == "invoice for march"


class TestPing:
    def test_ping_succeeds_on_healthy_db(self, seeded_db: Database):
        # Returns None on success; no exception is the signal.
        assert seeded_db.ping() is None

    def test_close_is_idempotent_noop_under_per_access_connections(self, seeded_db: Database):
        """Database opens and closes short-lived sqlite3 connections per read
        helper (the WAL-pinning fix), so ``close()`` no longer disables the
        handle — there is no persistent connection to close. The method is
        preserved as a no-op for API compatibility with test fixtures and
        main-shutdown paths that still call it; subsequent operations on the
        same Database continue to work, and calling ``close()`` twice is
        harmless.
        """
        seeded_db.close()
        # Subsequent reads keep working — each call opens its own
        # short-lived connection.
        assert seeded_db.ping() is None
        # Idempotent.
        seeded_db.close()
        assert seeded_db.ping() is None


class TestFailFastOnMissingIndex:
    def test_missing_parent_directory_raises(self, tmp_path):
        """MCP is a read-only consumer; if the data directory does not
        exist, the deployment is misconfigured. Fail fast with a clear
        message rather than silently creating an empty directory."""
        nonexistent = tmp_path / "no_such_dir" / "mail.db"
        with pytest.raises(FileNotFoundError, match="data directory"):
            Database(str(nonexistent))

    def test_missing_db_file_raises(self, tmp_path):
        """Parent exists but the DB file itself does not — this means the
        indexer has not yet initialized the shared index. Surface that
        specifically rather than as a cryptic 'unable to open' later."""
        (tmp_path / "data").mkdir()
        with pytest.raises(FileNotFoundError, match="index not found"):
            Database(str(tmp_path / "data" / "mail.db"))


class TestGetEmbeddingDim:
    """The DB is the source of truth for the embedding dimension.

    mcp-server must reject query vectors that don't match what the
    indexer wrote — otherwise wrong-dim vectors slip into the
    sqlite-vec MATCH path and the existing OperationalError swallow
    in ``_chunk_vector_search`` / ``_vector_search`` silently
    degrades search to keyword-only. Reading the dim from
    ``message_chunks_vec``'s CREATE statement keeps both sides in
    sync without a new env var.
    """

    def test_returns_declared_dim_for_chunk_vec_table(self, seeded_db: Database):
        # The shared test fixture declares ``message_chunks_vec`` with
        # ``FLOAT[4]`` so toy embeddings work; production schema uses
        # ``FLOAT[4096]``. Either way the integer comes back unchanged.
        assert seeded_db.get_embedding_dim() == 4

    def test_returns_none_when_vec_table_missing(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "no-vec.db"
        # Build a DB that has the file but no ``message_chunks_vec`` —
        # represents a fresh-install / pre-indexer state where
        # mcp-server starts but the indexer has not yet run its
        # schema migrations.
        sqlite3.connect(str(db_path)).close()
        db = Database(str(db_path))
        try:
            assert db.get_embedding_dim() is None
        finally:
            db.close()


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


class TestBestPerThread:
    """``_best_per_thread`` collapses chunk-lane rows to one per thread,
    keeping the row with the lowest BM25 score (best chunk match).
    """

    def test_keeps_best_score_per_thread(self, make_result):
        # Three rows for thread A (scores 0.9, 0.3, 0.7) and one for B.
        # The best for A is 0.3; the kept row should be that one.
        a1 = make_result("A")
        a1.score = 0.9
        a2 = make_result("A")
        a2.score = 0.3
        a3 = make_result("A")
        a3.score = 0.7
        b1 = make_result("B")
        b1.score = 0.5

        from src.lib.sqlite import Database

        kept = Database._best_per_thread([a1, a2, a3, b1])
        kept_ids = [r.thread_id for r in kept]
        assert kept_ids == ["A", "B"]
        a_kept = [r for r in kept if r.thread_id == "A"][0]
        assert a_kept.score == 0.3

    def test_empty_input_returns_empty(self):
        from src.lib.sqlite import Database

        assert Database._best_per_thread([]) == []


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
        a.senders = ["Alice@EXAMPLE.com"]
        b = make_result("b")
        b.senders = ["bob@example.com"]
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

    def test_date_to_includes_the_named_day(self, seeded_db: Database, make_result):
        """Regression: ``date_to="2024-12-31"`` used to be compared as a
        raw string against ISO timestamps like ``"2024-12-31T10:00:00+00:00"``,
        which excluded the entire 31st because the stored string sorts
        lexicographically greater than the bare date."""
        from datetime import UTC, datetime

        on_last_day = make_result("on_last_day")
        on_last_day.date_first = datetime(2024, 12, 31, 10, 0, tzinfo=UTC)
        on_last_day.date_last = datetime(2024, 12, 31, 10, 0, tzinfo=UTC)

        filtered = seeded_db._apply_filters([on_last_day], date_to="2024-12-31")
        assert [r.thread_id for r in filtered] == ["on_last_day"]

    def test_date_from_includes_the_named_day(self, seeded_db: Database, make_result):
        """Date-only ``date_from`` is promoted to the start of that day in
        UTC, so a message from 10:00 on the same day qualifies."""
        from datetime import UTC, datetime

        on_start_day = make_result("on_start_day")
        on_start_day.date_first = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
        on_start_day.date_last = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)

        filtered = seeded_db._apply_filters([on_start_day], date_from="2024-01-01")
        assert [r.thread_id for r in filtered] == ["on_start_day"]

    def test_date_range_excludes_prior_day(self, seeded_db: Database, make_result):
        """Messages strictly before ``date_from`` stay excluded — date-only
        promotion applies to the filter boundary, not to the data."""
        from datetime import UTC, datetime

        yesterday = make_result("yesterday")
        yesterday.date_first = datetime(2024, 12, 30, 23, 59, tzinfo=UTC)
        yesterday.date_last = datetime(2024, 12, 30, 23, 59, tzinfo=UTC)

        filtered = seeded_db._apply_filters([yesterday], date_from="2024-12-31")
        assert filtered == []


class TestKeywordSearchFilterPushdown:
    def test_keyword_search_matches_chunk_text(self, chunked_db: Database):
        """Exact terms present only in message_chunks_fts should still find
        the parent thread; otherwise precise chunk FTS rows are write-only."""
        results = chunked_db.keyword_search("12345", limit=10)
        assert [r.thread_id for r in results] == ["t-alpha"]

    def test_keyword_search_matches_attachment_filename(self, seeded_db: Database):
        """Attachment filename/MIME FTS is populated by the indexer, so MCP
        keyword search must query it as well as thread bodies."""
        results = seeded_db.keyword_search("march-statement-unique", limit=10)
        assert [r.thread_id for r in results] == ["t-alpha"]

    def test_folder_filter_pushed_into_sql(self, seeded_db: Database):
        """Regression: folder filter used to be applied in Python after the
        BM25 LIMIT. If the top candidates were all INBOX but the user
        asked for Archive, the Archive match deeper in the ranking would
        be cut. Pushdown lets the SQL WHERE filter before LIMIT."""
        results = seeded_db._keyword_search("meeting invoice lunch", limit=2, folders=["Archive"])
        assert all(r.folder == "Archive" for r in results)
        assert any(r.thread_id == "t-gamma" for r in results)

    def test_date_filter_pushed_into_sql(self, seeded_db: Database):
        """Pushing the date filter into SQL means pre-March threads never
        enter the ranked window — no need to over-fetch and drop them."""
        results = seeded_db._keyword_search(
            "march invoice lunch meeting", limit=10, date_from="2024-03-01"
        )
        assert all(r.thread_id != "t-gamma" for r in results)  # Feb thread excluded
        assert all(
            r.date_last
            >= __import__("datetime").datetime.fromisoformat("2024-03-01T00:00:00+00:00")
            for r in results
        )

    def test_has_attachments_filter_pushed_into_sql(self, seeded_db: Database):
        results = seeded_db._keyword_search("invoice lunch meeting", limit=10, has_attachments=True)
        assert all(r.has_attachments for r in results)

    def test_like_fallback_honors_filters(self, seeded_db: Database, monkeypatch):
        """Force FTS to raise so the LIKE fallback runs, and verify filters
        still apply in the fallback path."""
        from src.lib import sqlite as sqlite_mod

        monkeypatch.setattr(sqlite_mod, "_sanitize_fts_query", lambda q: "AND OR NEAR")
        results = seeded_db._keyword_search("invoice", limit=10, folders=["INBOX"])
        # t-alpha is in INBOX and matches subject/body LIKE "%invoice%"
        assert any(r.thread_id == "t-alpha" for r in results)
        assert all(r.folder == "INBOX" for r in results)

    def test_date_to_bare_day_includes_same_day_thread_in_sql(self, tmp_path, _build_thread_on):
        """Regression: date-only ``date_to`` was pushed straight into SQL
        and compared as a raw string against full ISO timestamps, so a
        thread with ``date_first = 2024-12-31T10:00:00+00:00`` was
        lexicographically greater than ``"2024-12-31"`` and excluded from
        the keyword path entirely. Normalize before pushdown."""
        db = _build_thread_on(
            tmp_path,
            subject="year end report",
            body_text="final year end report numbers",
            date_first="2024-12-31T10:00:00+00:00",
            date_last="2024-12-31T10:00:00+00:00",
        )
        results = db._keyword_search("year end report", limit=10, date_to="2024-12-31")
        assert any(r.thread_id == "on-last-day" for r in results)

    def test_date_to_bare_day_includes_same_day_thread_via_like_fallback(
        self, tmp_path, monkeypatch, _build_thread_on
    ):
        """Same regression as above, also exercised through the LIKE
        fallback where the SQL predicate is on ``threads.date_first``."""
        from src.lib import sqlite as sqlite_mod

        db = _build_thread_on(
            tmp_path,
            subject="year end report",
            body_text="final year end report numbers",
            date_first="2024-12-31T10:00:00+00:00",
            date_last="2024-12-31T10:00:00+00:00",
        )
        monkeypatch.setattr(sqlite_mod, "_sanitize_fts_query", lambda q: "AND OR NEAR")
        results = db._keyword_search("report", limit=10, date_to="2024-12-31")
        assert any(r.thread_id == "on-last-day" for r in results)


class TestSenderFilter:
    def test_from_addr_only_matches_senders(self, seeded_db: Database):
        """Regression: from_addr used to check participants (From + To + Cc),
        so "from alice" matched threads where alice was merely a recipient.
        With schema v6 senders populated, the filter now matches senders
        only.
        """
        # alice sent t-alpha; alice is only a recipient on t-beta.
        results = seeded_db.keyword_search("invoice lunch", from_addr="alice@example.com")
        ids = {r.thread_id for r in results}
        assert "t-alpha" in ids
        assert "t-beta" not in ids  # alice is a recipient here, not sender

    def test_from_addr_ignores_recipients_when_senders_populated(self):
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_sender

        modern = ThreadResult(
            thread_id="modern",
            subject="s",
            participants=["alice@example.com", "bob@example.com"],
            senders=["bob@example.com"],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
        )
        assert _matches_sender(modern, "bob")
        assert not _matches_sender(modern, "alice")

    def test_from_addr_full_address_matches_display_name_variant(self):
        """Regression: a full-address query (``bob@example.com``) was
        compared with lowercased substring, so a stored display form like
        ``Bob Smith <bob@example.com>`` matched by accident, but a
        case-mixed stored address (``Bob@Example.com``) could also slip
        through other address-in-string coincidences. Canonical equality
        normalizes both sides the same way, so the full-address query
        reliably matches every display variant of the same correspondent."""
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_sender

        result = ThreadResult(
            thread_id="t",
            subject="s",
            participants=["Bob Smith <Bob@Example.com>"],
            senders=["Bob Smith <Bob@Example.com>"],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
        )
        assert _matches_sender(result, "bob@example.com")

    def test_from_addr_full_address_does_not_partial_match(self):
        """Regression: substring matching meant ``from_addr="bob@example.com"``
        also matched ``"notbob@example.com"``. Canonical equality for
        full-address queries rejects near-misses."""
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_sender

        result = ThreadResult(
            thread_id="t",
            subject="s",
            participants=["notbob@example.com"],
            senders=["notbob@example.com"],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
        )
        assert not _matches_sender(result, "bob@example.com")

    def test_from_addr_domain_fragment_keeps_substring_behavior(self):
        """A query that cannot canonicalize (bare name, domain fragment)
        stays on the substring-match path so friendly searches like
        ``from Bob`` or domain-wide filters like ``@example.com`` still
        work against the lowercased display string."""
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_sender

        result = ThreadResult(
            thread_id="t",
            subject="s",
            participants=["Bob Smith <bob@example.com>"],
            senders=["Bob Smith <bob@example.com>"],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
        )
        assert _matches_sender(result, "bob")
        assert _matches_sender(result, "@example.com")


class TestKeywordSearch:
    def test_matches_body_token(self, seeded_db: Database):
        results = seeded_db.keyword_search("invoice")
        assert any(r.thread_id == "t-alpha" for r in results)

    def test_returns_empty_when_no_match(self, seeded_db: Database):
        assert seeded_db.keyword_search("nonexistentsearchtoken") == []

    def test_keyword_search_honors_from_addr_filter(self, seeded_db: Database):
        """Regression: keyword mode previously dropped every filter except
        ``folders``, silently returning unfiltered results."""
        # t-alpha and t-beta both have "alice@example.com" as a participant;
        # restrict by sender that only appears in t-gamma ("Archive").
        results = seeded_db.keyword_search("meeting", from_addr="dave@example.com")
        assert all(any("dave@example.com" in p.lower() for p in r.participants) for r in results)

    def test_keyword_search_honors_date_filter(self, seeded_db: Database):
        # t-gamma is in Archive with date_last 2024-02-15; restrict to
        # post-March so only t-alpha / t-beta qualify.
        results = seeded_db.keyword_search(
            "march invoice lunch meeting",
            date_from="2024-03-01T00:00:00+00:00",
        )
        assert all(r.date_last.isoformat() >= "2024-03-01T00:00:00+00:00" for r in results)
        assert not any(r.thread_id == "t-gamma" for r in results)

    def test_keyword_search_honors_has_attachments_filter(self, seeded_db: Database):
        only_with = seeded_db.keyword_search("march invoice lunch meeting", has_attachments=True)
        assert all(r.has_attachments for r in only_with)
        assert any(r.thread_id == "t-alpha" for r in only_with)

    def test_semantic_search_honors_from_addr_filter(self, seeded_db: Database):
        """Same regression as keyword mode — filter parity across all three."""
        results = seeded_db.semantic_search(
            [0.0, 0.0, 1.0, 0.0],  # nearest to t-gamma
            from_addr="dave@example.com",
        )
        assert all(any("dave@example.com" in p.lower() for p in r.participants) for r in results)

    def test_semantic_search_honors_date_filter(self, seeded_db: Database):
        results = seeded_db.semantic_search(
            [1.0, 0.0, 0.0, 0.0],
            date_from="2024-03-01T00:00:00+00:00",
        )
        assert all(r.date_last.isoformat() >= "2024-03-01T00:00:00+00:00" for r in results)

    def test_folder_filter_restricts_results(self, seeded_db: Database):
        # "meeting" appears only in the Archive thread
        all_results = seeded_db.keyword_search("meeting")
        assert any(r.folder == "Archive" for r in all_results)
        inbox_only = seeded_db.keyword_search("meeting", folders=["INBOX"])
        assert inbox_only == []

    def test_unmatched_quote_query_is_sanitized(self, seeded_db: Database):
        # Raw input with an unbalanced quote previously tripped FTS5 and
        # returned []. The sanitizer now extracts the word token so the
        # query runs — the expected result is still empty here because
        # "unterminated" does not appear in the seeded rows.
        assert seeded_db.keyword_search('"unterminated') == []


class TestSemanticSearch:
    def test_nearest_neighbor_returned_first(self, seeded_db: Database):
        results = seeded_db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=3)
        assert results[0].thread_id == "t-alpha"

    def test_empty_db_returns_empty(self, empty_db: Database):
        assert empty_db.semantic_search([1.0, 0.0, 0.0, 0.0]) == []

    def test_chunk_vec_lane_lifts_thread_with_weak_thread_vec(self, tmp_path):
        """Codex P2: ``semantic_search`` used to ignore the chunk-vec lane,
        so an MCP caller picking ``mode="semantic"`` silently lost the
        precision-evidence layer the rest of the architecture relies
        on. With chunk-vec fusion, a thread whose mean-pooled coarse
        vector points away from the query but which carries one
        strongly-aligned chunk must still surface."""
        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        db_path = tmp_path / "semantic-chunks.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        # Target thread: coarse vector orthogonal to the query, so the
        # thread-vec lane never surfaces it on its own.
        _insert_thread(
            conn,
            thread_id="t-target",
            subject="long thread",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="mixed content thread",
            snippet="mixed content",
            embedding=[0.0, 0.0, 0.0, 1.0],
        )
        # But one chunk inside the thread IS aligned with the query.
        _insert_chunk(
            conn,
            chunk_id="target-chunk",
            message_id="t-target",
            thread_id="t-target",
            text="the precise passage aligned with the query",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        # Decoy thread whose coarse vector is aligned but has no chunks.
        _insert_thread(
            conn,
            thread_id="t-decoy-vec",
            subject="decoy a",
            participants=["bob@example.com"],
            senders=["bob@example.com"],
            body_text="vec-only decoy",
            snippet="decoy",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(db_path))
        try:
            results = db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=5)
        finally:
            db.close()
        ids = [r.thread_id for r in results]
        # Both threads surface: chunk lane lifts t-target, thread-vec
        # lane lifts t-decoy-vec. Without chunk-vec fusion only the
        # decoy would appear.
        assert "t-target" in ids, (
            "semantic_search must fuse chunk-vec — without it the chunk "
            f"lane is silently dropped; got {ids!r}"
        )


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

    def test_hybrid_chunk_vec_lane_uses_shared_oversample_constant(
        self, seeded_db: Database, monkeypatch
    ):
        """Regression for the dense-chunk pool-starvation failure mode.

        A long thread with many semantically-similar chunks can fill
        the top-K of the chunk-vector lane and contribute only one
        unique thread to RRF (the best-rank-only dedupe in
        ``_reciprocal_rank_fusion`` strips its siblings). Sibling
        threads whose only signal is also a chunk-vector match never
        enter the fused result list. The keyword chunk and attachment
        lanes already use ``_CHUNK_LANE_OVERSAMPLE`` (=10) to address
        exactly this; the dense chunk lane in ``hybrid_search`` used
        ``fetch_limit * 3`` until this regression was flagged.

        Asserting the wiring directly (the chunk-vec lane is called
        with ``fetch_limit * _CHUNK_LANE_OVERSAMPLE``) keeps the test
        deterministic. The RRF-score behaviour at high oversample
        already has coverage in ``TestRRFChunkLifting`` — the gap
        flagged here was the call-site constant, not the dedupe.
        """
        from src.lib import sqlite as sqlite_module

        captured_limits: list[int] = []
        original = sqlite_module.Database._chunk_vector_search

        def _spy(self, query_embedding, limit):
            captured_limits.append(limit)
            return original(self, query_embedding, limit)

        monkeypatch.setattr(sqlite_module.Database, "_chunk_vector_search", _spy)

        seeded_db.hybrid_search(
            query_text="anything",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
        )

        assert captured_limits, "_chunk_vector_search was never invoked"
        # Default ``hybrid_search`` math: no filters / no rerank →
        # oversample = _UNFILTERED_OVERSAMPLE (2) → fetch_limit = 10 →
        # the chunk-vec lane MUST be invoked with
        # fetch_limit * _CHUNK_LANE_OVERSAMPLE (= 100), not the prior
        # ``fetch_limit * 3`` (= 30). A regression dropping the
        # constant back to ``3`` would fail this assertion immediately.
        expected = (
            5  # limit
            * sqlite_module._UNFILTERED_OVERSAMPLE
            * sqlite_module._CHUNK_LANE_OVERSAMPLE
        )
        assert captured_limits[0] == expected, (
            f"chunk-vec lane invoked with limit={captured_limits[0]}, "
            f"expected {expected} "
            f"(fetch_limit * _CHUNK_LANE_OVERSAMPLE). A smaller value "
            f"reintroduces the pool-starvation failure for long "
            f"threads with many similar chunks."
        )


class _IndexScoringReranker:
    """Test stub: scores each candidate by its position in the input
    list, using a caller-supplied score map.

    ``scores_by_index[i]`` is the rerank score for the candidate at
    position ``i``. Missing positions get 0.0. The stub captures the
    last call's ``(query, docs, top_n)`` for assertion-side inspection.
    The fake conforms to the ``RerankerBackend`` protocol structurally
    — duck-typed, no inheritance — so the sqlite layer takes it
    without test-time imports of reranker.py.
    """

    def __init__(self, scores_by_index: dict[int, float], candidates: int = 50, top_n: int = 5):
        self.candidates = candidates
        self.top_n = top_n
        self._scores = scores_by_index
        self._last_query: str | None = None
        self._last_docs: list[str] | None = None
        self._last_top_n: int | None = None

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        self._last_query = query
        self._last_docs = list(documents)
        self._last_top_n = top_n
        effective_top_n = top_n if top_n is not None else self.top_n
        scored = [(i, self._scores.get(i, 0.0)) for i in range(len(documents))]
        scored.sort(key=lambda x: -x[1])
        return scored[:effective_top_n]


class _BrokenReranker:
    """Stub that always reports failure so we can verify the RRF
    fallback path keeps results from disappearing."""

    candidates = 50
    top_n = 5

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        return []


class TestRerankInHybridSearch:
    """Reranker is wired in as an optional, post-RRF pass that
    reorders the top ``candidates`` to a final ``top_n``. Tests run
    against the seeded DB with stubbed rerankers — the rerank stage
    is library-agnostic."""

    def test_no_reranker_preserves_legacy_behavior(self, seeded_db: Database):
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
        )
        assert len(results) <= 2
        assert results[0].thread_id == "t-alpha"

    def test_reranker_reorders_and_truncates_to_top_n(self, seeded_db: Database):
        # Establish the baseline RRF order so we can promote the worst
        # candidate to top-1 via the reranker and prove the reorder is
        # actually score-driven, not order-preserving.
        baseline = seeded_db.hybrid_search(
            query_text="meeting",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            limit=10,
        )
        assert len(baseline) >= 2, "fixture must surface at least 2 candidates"
        last_idx = len(baseline) - 1
        promoted_thread_id = baseline[last_idx].thread_id

        scripted = _IndexScoringReranker(
            scores_by_index={last_idx: 9.99, 0: 0.01},
            candidates=10,
            top_n=2,
        )
        reranked = seeded_db.hybrid_search(
            query_text="meeting",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            limit=2,
            reranker=scripted,
        )
        assert reranked[0].thread_id == promoted_thread_id
        assert reranked[0].score == 9.99
        assert len(reranked) == 2

    def test_reranker_receives_subject_prefixed_doc_text(self, seeded_db: Database):
        scripted = _IndexScoringReranker(scores_by_index={}, candidates=10, top_n=5)
        seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            reranker=scripted,
        )
        assert scripted._last_query == "invoice"
        assert scripted._last_docs is not None
        assert all(d.startswith("Subject: ") for d in scripted._last_docs)

    def test_caller_limit_overrides_reranker_default_top_n(self, seeded_db: Database):
        # The reranker's ``top_n`` is a *default*, not a hard cap. A
        # caller asking for ``limit=3`` against a reranker whose
        # default ``top_n`` is 1 must still receive 3 results — the
        # rerank stage gets ``top_n=3`` for this call so it doesn't
        # silently undercut the caller. This guards against the
        # ``extract_from_emails(limit=20)`` regression Codex flagged
        # where a default top_n=10 truncated the caller's request.
        scripted = _IndexScoringReranker(
            scores_by_index={0: 0.9, 1: 0.5, 2: 0.1},
            candidates=10,
            top_n=1,  # default cap that MUST NOT win over limit=3
        )
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            reranker=scripted,
        )
        assert scripted._last_top_n == 3
        assert len(results) == min(3, len(scripted._last_docs or []))

    def test_caller_limit_overrides_reranker_candidates_floor(self, seeded_db: Database):
        # ``RERANK_CANDIDATES`` is the rerank-stage funnel size, not a
        # result cap. A caller asking for ``limit=3`` against a
        # reranker configured with ``candidates=1`` must still get 3
        # results — the candidate slice has to honour
        # ``max(limit, candidates)``. Without that ``max``, an
        # operator who tightened ``RERANK_CANDIDATES`` for latency
        # would silently cap recall for callers like
        # ``extract_from_emails(limit=50)``.
        scripted = _IndexScoringReranker(
            scores_by_index={0: 0.9, 1: 0.5, 2: 0.1},
            candidates=1,  # tiny funnel that MUST NOT win over limit=3
            top_n=10,
        )
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            reranker=scripted,
        )
        # The reranker received at least ``limit`` documents, not just
        # ``candidates``.
        assert scripted._last_docs is not None
        assert len(scripted._last_docs) >= min(3, 3)
        assert len(results) == min(3, len(scripted._last_docs))

    def test_reranker_failure_falls_back_to_rrf_order(self, seeded_db: Database):
        rrf_only = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
        )
        with_broken = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
            reranker=_BrokenReranker(),
        )
        assert [r.thread_id for r in with_broken] == [r.thread_id for r in rrf_only]


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

    def test_list_threads_rejects_unindexed_filter_types(self, seeded_db: Database):
        with pytest.raises(ValueError, match="filter_type"):
            seeded_db.list_threads(folder="INBOX", filter_type="unread")


class TestDisplaySubjectFallback:
    """``ThreadResult.subject`` surfaces ``display_subject`` when set
    (added in v13) and falls back to the normalized ``subject`` for
    legacy rows where ``display_subject`` is ``NULL``."""

    def test_uses_display_subject_when_present(self, tmp_path):
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        db_path = tmp_path / "with-display.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-display",
            subject="today's meeting",  # normalized matching key
            participants=["a@example.com"],
            display_subject="Today's Meeting",  # original-cased
        )
        conn.close()

        db = Database(db_path)
        try:
            result = db.get_thread("t-display")
            assert result is not None
            assert result.subject == "Today's Meeting"
        finally:
            db.close()

    def test_falls_back_to_normalized_subject_when_display_is_null(self, tmp_path):
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        db_path = tmp_path / "without-display.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-legacy",
            subject="legacy lowercased subject",
            participants=["a@example.com"],
            # display_subject left None — simulates a v12 row carried
            # forward through the v13 migration without a refresh.
        )
        conn.close()

        db = Database(db_path)
        try:
            result = db.get_thread("t-legacy")
            assert result is not None
            assert result.subject == "legacy lowercased subject"
        finally:
            db.close()

    def test_keyword_search_returns_display_subject(self, tmp_path):
        """Regression: the explicit column projection in
        ``_thread_keyword_search`` previously omitted ``display_subject``
        from the SELECT, so ``_row_to_result`` could not see it and
        ``ThreadResult.subject`` fell back to the normalized lowercase
        ``subject`` even when a ``display_subject`` was stored. Hybrid
        and semantic search shared the same shape."""
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        db_path = tmp_path / "kw-display.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-kw",
            subject="today's meeting",
            participants=["a@example.com"],
            body_text="agenda for today's meeting",
            display_subject="Today's Meeting",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()

        db = Database(db_path)
        try:
            results = db.keyword_search("agenda")
            assert len(results) == 1
            assert results[0].subject == "Today's Meeting"
        finally:
            db.close()

    def test_semantic_search_returns_display_subject(self, tmp_path):
        """Same regression coverage for the semantic lane."""
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        db_path = tmp_path / "sem-display.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-sem",
            subject="today's meeting",
            participants=["a@example.com"],
            body_text="agenda for today's meeting",
            display_subject="Today's Meeting",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()

        db = Database(db_path)
        try:
            results = db.semantic_search([1.0, 0.0, 0.0, 0.0])
            assert len(results) == 1
            assert results[0].subject == "Today's Meeting"
        finally:
            db.close()

    def test_hybrid_search_returns_display_subject(self, tmp_path):
        """Same regression coverage for hybrid (the actual user-facing
        path through ``search_emails``)."""
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        db_path = tmp_path / "hyb-display.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-hyb",
            subject="today's meeting",
            participants=["a@example.com"],
            body_text="agenda for today's meeting",
            display_subject="Today's Meeting",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()

        db = Database(db_path)
        try:
            results = db.hybrid_search(
                query_text="agenda",
                query_embedding=[1.0, 0.0, 0.0, 0.0],
            )
            assert len(results) == 1
            assert results[0].subject == "Today's Meeting"
        finally:
            db.close()


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


class TestFilterDateUtcNormalization:
    """Stored ``date_last`` / ``date_first`` values are UTC-normalized by
    the indexer parser and serialized with a ``+00:00`` offset. Filter
    bounds reach SQL via ``isoformat()`` too, and the comparison happens
    lexicographically. If an offset-aware filter kept its original offset,
    two strings representing the same instant would sort differently —
    e.g. ``2024-06-01T08:00:00-04:00`` vs stored ``2024-06-01T12:00:00+00:00``
    — and silently drop matching rows. Normalize to UTC first."""

    def test_offset_aware_filter_normalized_to_utc(self, seeded_db: Database, make_result):
        """Same instant as ``2024-06-01T12:00:00+00:00``, written with a
        ``-04:00`` offset, must still include a row stamped at that instant."""
        from datetime import UTC, datetime

        on_boundary = make_result("on_boundary")
        on_boundary.date_first = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        on_boundary.date_last = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)

        filtered = seeded_db._apply_filters([on_boundary], date_from="2024-06-01T08:00:00-04:00")
        assert [r.thread_id for r in filtered] == ["on_boundary"]

    def test_offset_aware_upper_bound_normalized_to_utc(self, seeded_db: Database, make_result):
        """An offset-aware ``date_to`` one minute before the stored UTC
        instant (same instant shifted by offset does not clear the row)
        must still exclude rows strictly after that instant."""
        from datetime import UTC, datetime

        after_cutoff = make_result("after_cutoff")
        after_cutoff.date_first = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)
        after_cutoff.date_last = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)

        filtered = seeded_db._apply_filters([after_cutoff], date_to="2024-06-01T08:29:00-04:00")
        assert filtered == []

    def test_normalize_date_bound_returns_utc_isoformat(self):
        """``_normalize_date_bound`` produces the string handed straight to
        SQL pushdown — it must carry a ``+00:00`` offset regardless of the
        offset the caller supplied, so lexicographic comparison against
        stored UTC timestamps is well-defined."""
        from src.lib.sqlite import _normalize_date_bound

        normalized = _normalize_date_bound(
            "2024-06-01T08:00:00-04:00", end_of_day=False, field_name="date_from"
        )
        assert normalized is not None
        assert normalized.endswith("+00:00")
        assert normalized.startswith("2024-06-01T12:00:00")


class TestFtsSanitization:
    def test_sanitizer_extracts_word_tokens(self):
        from src.lib.sqlite import _sanitize_fts_query

        assert _sanitize_fts_query("hello world") == '"hello" OR "world"'

    def test_sanitizer_preserves_email_tokens(self):
        from src.lib.sqlite import _sanitize_fts_query

        # @, ., - must survive so email addresses remain searchable.
        assert '"alice@example.com"' in _sanitize_fts_query("from alice@example.com")

    def test_sanitizer_strips_punctuation_that_would_break_fts(self):
        from src.lib.sqlite import _sanitize_fts_query

        sanitized = _sanitize_fts_query("Who's the landlord? (urgent)")
        assert "?" not in sanitized
        assert "(" not in sanitized

    def test_sanitizer_empty_for_noise_only_input(self):
        from src.lib.sqlite import _sanitize_fts_query

        assert _sanitize_fts_query("!!!") == ""
        assert _sanitize_fts_query("") == ""


class TestKeywordSearchSanitization:
    def test_punctuation_query_does_not_crash(self, seeded_db: Database):
        """A natural-language query full of punctuation previously returned
        empty due to FTS syntax errors. The sanitizer extracts the meaningful
        tokens so matches still come back."""
        results = seeded_db.keyword_search("Who sent the invoice?")
        assert any(r.thread_id == "t-alpha" for r in results)

    def test_email_address_query_returns_expected_match(self, seeded_db: Database):
        results = seeded_db.keyword_search("alice@example.com")
        # "alice@example.com" appears in participants of t-alpha and t-beta
        assert results

    def test_empty_query_returns_empty(self, seeded_db: Database):
        assert seeded_db.keyword_search("") == []
        assert seeded_db.keyword_search("!!!") == []


class TestLikeFallback:
    def test_like_fallback_returns_matches_by_subject(self, seeded_db: Database):
        """``_like_fallback`` scans subject/body_text/participants with
        ``LIKE`` and is the recovery path used when FTS rejects a
        sanitized query."""
        results = seeded_db._like_fallback("invoice", limit=10)
        assert any(r.thread_id == "t-alpha" for r in results)

    def test_like_fallback_returns_matches_by_body(self, seeded_db: Database):
        results = seeded_db._like_fallback("spot", limit=10)
        # "spot" appears in t-beta body_text "want to grab lunch tomorrow at the usual spot"
        assert any(r.thread_id == "t-beta" for r in results)

    def test_like_fallback_returns_empty_when_no_match(self, seeded_db: Database):
        assert seeded_db._like_fallback("nowhereinseededdata", limit=10) == []

    def test_keyword_search_falls_back_when_fts_raises(self, seeded_db: Database, monkeypatch):
        """Patch ``_sanitize_fts_query`` to return a deliberately invalid
        MATCH expression that FTS5 will reject — the except branch must
        invoke ``_like_fallback`` and still return matches."""
        from src.lib import sqlite as sqlite_mod

        monkeypatch.setattr(sqlite_mod, "_sanitize_fts_query", lambda q: "AND OR NEAR")
        results = seeded_db.keyword_search("invoice")
        assert any(r.thread_id == "t-alpha" for r in results)


class TestOversampleOnFilter:
    def test_fetch_limit_grows_when_filter_present(self, seeded_db: Database, monkeypatch):
        """A folder filter must trigger the higher oversample multiplier so
        filtered results deeper in the ranked list still make the page."""
        seen_limits: list[int] = []
        real_keyword = seeded_db._keyword_search

        def spy_keyword(q, limit, **kwargs):
            seen_limits.append(limit)
            return real_keyword(q, limit, **kwargs)

        monkeypatch.setattr(seeded_db, "_keyword_search", spy_keyword)

        seeded_db.hybrid_search(
            query_text="meeting",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            folders=["INBOX"],
            limit=10,
        )
        assert seen_limits == [40]

        seen_limits.clear()
        seeded_db.hybrid_search(
            query_text="meeting",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            limit=10,
        )
        assert seen_limits == [20]


class TestBodyTextLoadedIntoResult:
    def test_body_text_populated_from_fts_join(self, seeded_db: Database):
        results = seeded_db.keyword_search("invoice")
        assert results
        assert "invoice attached for march" in results[0].body_text

    def test_body_text_populated_from_vector_search(self, seeded_db: Database):
        results = seeded_db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=1)
        assert results
        assert results[0].body_text


# ---------------------------------------------------------------------------
# Schema v9 — chunk vector lane and chunk-aware hybrid search
# ---------------------------------------------------------------------------


class TestChunkVectorSearch:
    def test_returns_nearest_chunk_first(self, chunked_db: Database):
        results = chunked_db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], limit=5)
        assert results, "expected chunk hits for an aligned query"
        assert results[0].thread_id == "t-alpha"
        assert "invoice number 12345" in results[0].text

    def test_skips_threads_without_chunks(self, chunked_db: Database):
        # The third axis aligns with t-gamma's thread vector — but t-gamma
        # has NO chunks (e.g. empty body), so the chunk lane must
        # not surface it. Coarse retrieval lanes will still find it via
        # the existing thread vector + thread FTS paths.
        results = chunked_db._chunk_vector_search([0.0, 0.0, 1.0, 0.0], limit=5)
        for r in results:
            assert r.thread_id != "t-gamma"

    def test_empty_db_returns_empty_list(self, empty_db: Database):
        assert empty_db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], limit=5) == []


class TestEvidenceChunksHelper:
    def test_groups_chunks_by_requested_thread_id(self, chunked_db: Database):
        evidence = chunked_db.get_evidence_chunks_for_threads(
            thread_ids=["t-alpha", "t-beta"],
            embedding=[1.0, 0.0, 0.0, 0.0],
            per_thread_limit=3,
        )
        assert set(evidence.keys()) == {"t-alpha", "t-beta"}
        assert all(c.thread_id == "t-alpha" for c in evidence["t-alpha"])

    def test_unrequested_threads_excluded(self, chunked_db: Database):
        evidence = chunked_db.get_evidence_chunks_for_threads(
            thread_ids=["t-alpha"],
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        # Only the requested thread appears in the dict, regardless of
        # which chunks the underlying lane found.
        assert set(evidence.keys()) == {"t-alpha"}

    def test_empty_thread_ids_returns_empty_dict(self, chunked_db: Database):
        assert chunked_db.get_evidence_chunks_for_threads([], [1.0, 0.0, 0.0, 0.0]) == {}

    def test_returns_thread_chunks_even_when_global_top_k_excludes_them(self, tmp_path):
        """Regression for the ``with_evidence=True`` pool-starvation gap.

        Codex flagged that ``ask_mailbox`` could surface a carrier
        email via BM25 / metadata / thread-vector / attachment FTS
        and then hand the LLM ``body_text`` instead of the attachment
        passages — because the prior pool-reuse logic populated
        ``evidence_chunks`` only from the global chunk-vec top-K, and
        the carrier's specific chunks could rank deep enough to fall
        out of that pool entirely.

        Setup pins exactly that failure shape: thread ``t-carrier``
        carries a chunk whose embedding is FAR from the query, plus a
        cloud of distractor threads whose chunks dominate any global
        top-K. The helper must still surface ``t-carrier``'s own
        chunk when asked for that thread specifically — because real
        production callers (hybrid_search after a non-chunk-lane win)
        rely on per-thread retrieval, not pool filtering.
        """
        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        db_path = tmp_path / "carrier.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        # Carrier thread: chunk embedding orthogonal to query. A pool-
        # reuse implementation pulling top-K by similarity would never
        # see this chunk.
        _insert_thread(
            conn,
            thread_id="t-carrier",
            subject="please find attached",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="please find attached",
            snippet="please find attached",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_chunk(
            conn,
            chunk_id="carrier-chunk",
            message_id="t-carrier",
            thread_id="t-carrier",
            text="attached PDF: invoice number 99999 due june 30",
            # Deliberately ORTHOGONAL to the query so a global top-K
            # by similarity would never include this chunk.
            embedding=[0.0, 0.0, 0.0, 1.0],
        )

        # 50 distractor threads each with a chunk closer to the query
        # than the carrier's. Together they monopolise the global
        # top-K — any pool-reuse logic with a reasonable candidate
        # pool size would miss the carrier's chunk entirely.
        for i in range(50):
            tid = f"t-d-{i:02d}"
            _insert_thread(
                conn,
                thread_id=tid,
                subject=f"distractor {i}",
                participants=["bob@example.com"],
                senders=["bob@example.com"],
                body_text=f"distractor body {i}",
                snippet="d",
                embedding=[0.9, 0.4, 0.0, 0.0],
            )
            _insert_chunk(
                conn,
                chunk_id=f"d-{i:02d}-chunk",
                message_id=tid,
                thread_id=tid,
                text=f"distractor chunk content {i}",
                # Close to query — these dominate any global top-K.
                embedding=[1.0, 0.0, 0.0, 0.0],
            )

        conn.close()

        db = Database(str(db_path))
        try:
            # Ask for evidence on the carrier specifically — what
            # ``hybrid_search(with_evidence=True)`` does after the
            # carrier wins via thread-vec / BM25 / metadata.
            evidence = db.get_evidence_chunks_for_threads(
                thread_ids=["t-carrier"],
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
        finally:
            db.close()

        # The carrier's chunk MUST come back even though it's deep
        # inside the global similarity ordering — the helper scans
        # only chunks belonging to the requested thread_ids, so
        # global pool starvation can't strip it.
        assert "t-carrier" in evidence
        chunk_texts = [c.text for c in evidence["t-carrier"]]
        assert any("99999" in t for t in chunk_texts), (
            f"carrier's chunk (with sentinel ``99999``) must be "
            f"retrievable even when a global chunk-vec top-K would "
            f"never include it; got: {chunk_texts!r}"
        )
        # And the helper must NOT leak distractor chunks into the
        # carrier's bucket.
        assert all(c.thread_id == "t-carrier" for c in evidence["t-carrier"])


class TestEvidenceAttachmentProvenance:
    """Codex P1: ``ask_mailbox`` promises attachment content. The evidence
    helper must surface attachment chunks' filename + MIME and prefer
    attachment chunks when the thread won via the attachment-FTS lane —
    otherwise a filename-matched thread can hand the LLM body text
    instead of the attachment the user asked about.
    """

    def _build_attachment_carrier_db(self, tmp_path):
        """Carrier thread with one attachment chunk + one body chunk."""
        from tests.conftest import (
            _build_schema,
            _insert_attachment,
            _insert_chunk,
            _insert_thread,
        )

        db_path = tmp_path / "attachment-evidence.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-quote",
            subject="proposal cover note",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="please see attached for our latest proposal",
            snippet="please see attached",
            has_attachments=True,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_attachment(
            conn,
            message_id="t-quote",
            thread_id="t-quote",
            attachment_id="att-quote",
            filename="proposal-quote.pdf",
            content_type="application/pdf",
        )
        # Body chunk: aligned with the query, so by dense-only ranking
        # this is the chunk evidence helper would pick first.
        _insert_chunk(
            conn,
            chunk_id="t-quote-body",
            message_id="t-quote",
            thread_id="t-quote",
            text="please see attached for our latest proposal",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
        )
        # Attachment chunk: orthogonal to the query embedding so it ranks
        # AFTER the body chunk on dense similarity alone. Carries
        # the actual attachment text the user wants.
        _insert_chunk(
            conn,
            chunk_id="t-quote-att",
            message_id="t-quote",
            thread_id="t-quote",
            text="line item: solar installation total USD 18450",
            embedding=[0.0, 0.0, 0.0, 1.0],
            chunk_index=1,
            attachment_id="att-quote",
        )
        conn.close()
        return Database(str(db_path))

    def test_chunk_result_carries_attachment_provenance(self, tmp_path):
        db = self._build_attachment_carrier_db(tmp_path)
        try:
            evidence = db.get_evidence_chunks_for_threads(
                thread_ids=["t-quote"],
                embedding=[0.0, 0.0, 0.0, 1.0],
                per_thread_limit=5,
            )
        finally:
            db.close()
        chunks = evidence["t-quote"]
        att_chunk = next(c for c in chunks if c.attachment_id is not None)
        assert att_chunk.attachment_filename == "proposal-quote.pdf"
        assert att_chunk.attachment_mime == "application/pdf"
        body_chunk = next(c for c in chunks if c.attachment_id is None)
        assert body_chunk.attachment_filename is None
        assert body_chunk.attachment_mime is None

    def test_attachment_won_threads_get_attachment_chunks_first(self, tmp_path):
        """The core P1 fix: when the thread is in ``attachment_won_thread_ids``,
        attachment chunks float to the front of the per-thread evidence
        slice even though the body chunk dense-scored higher."""
        db = self._build_attachment_carrier_db(tmp_path)
        try:
            # Query embedding is aligned with the BODY chunk — pure
            # dense ranking would surface body first.
            evidence_bias = db.get_evidence_chunks_for_threads(
                thread_ids=["t-quote"],
                embedding=[1.0, 0.0, 0.0, 0.0],
                per_thread_limit=1,
                attachment_won_thread_ids={"t-quote"},
            )
            evidence_no_bias = db.get_evidence_chunks_for_threads(
                thread_ids=["t-quote"],
                embedding=[1.0, 0.0, 0.0, 0.0],
                per_thread_limit=1,
            )
        finally:
            db.close()
        # With the attachment-won bias, slot-0 is the attachment chunk.
        assert evidence_bias["t-quote"][0].attachment_id == "att-quote"
        # Without the bias, slot-0 is the body chunk (regression guard).
        assert evidence_no_bias["t-quote"][0].attachment_id is None

    def test_hybrid_search_with_evidence_biases_filename_winners(self, tmp_path):
        """End-to-end: a query whose tokens hit the attachment-FTS lane
        must produce evidence chunks with the attachment chunk first."""
        db = self._build_attachment_carrier_db(tmp_path)
        try:
            # ``proposal-quote`` matches the attachment filename FTS.
            results = db.hybrid_search(
                query_text="proposal-quote",
                query_embedding=[1.0, 0.0, 0.0, 0.0],
                limit=5,
                with_evidence=True,
            )
        finally:
            db.close()
        assert results
        top = next(r for r in results if r.thread_id == "t-quote")
        assert top.evidence_chunks
        assert top.evidence_chunks[0].attachment_id == "att-quote"
        assert top.evidence_chunks[0].attachment_filename == "proposal-quote.pdf"


class TestGetRecentChunksForThread:
    """Codex P1: ``body_text`` is front-preserved and token-capped, so once
    a thread crosses 4000 tokens the newest replies are silently dropped.
    ``summarize_thread`` must read the chunk tail directly instead.
    """

    def _build_chunked_db_with_timeline(self, tmp_path):
        """Thread with three chunks at distinct ``chunked_at`` timestamps."""
        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        db_path = tmp_path / "timeline.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-tl",
            subject="long running thread",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="oldest content only (later replies were chopped off)",
            snippet="oldest content",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        # Three chunks, oldest → newest by chunked_at.
        _insert_chunk(
            conn,
            chunk_id="c-old",
            message_id="t-tl",
            thread_id="t-tl",
            text="oldest message content",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
            chunked_at="2024-01-01T00:00:00+00:00",
        )
        _insert_chunk(
            conn,
            chunk_id="c-mid",
            message_id="t-tl",
            thread_id="t-tl",
            text="middle message content",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
            chunked_at="2024-06-01T00:00:00+00:00",
        )
        _insert_chunk(
            conn,
            chunk_id="c-new",
            message_id="t-tl",
            thread_id="t-tl",
            text="newest reply content",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
            chunked_at="2024-12-01T00:00:00+00:00",
        )
        conn.close()
        return Database(str(db_path))

    def test_returns_latest_chunks_in_chronological_order(self, tmp_path):
        db = self._build_chunked_db_with_timeline(tmp_path)
        try:
            chunks = db.get_recent_chunks_for_thread("t-tl", limit=2)
        finally:
            db.close()
        # Selection picked the two newest by ``chunked_at DESC``;
        # output reverses so the prompt reads oldest-first.
        assert [c.chunk_id for c in chunks] == ["c-mid", "c-new"]

    def test_limit_zero_returns_empty(self, tmp_path):
        db = self._build_chunked_db_with_timeline(tmp_path)
        try:
            assert db.get_recent_chunks_for_thread("t-tl", limit=0) == []
        finally:
            db.close()

    def test_thread_without_chunks_returns_empty(self, chunked_db: Database):
        # ``t-gamma`` has no chunks in chunked_db.
        assert chunked_db.get_recent_chunks_for_thread("t-gamma") == []

    def test_unknown_thread_returns_empty(self, chunked_db: Database):
        assert chunked_db.get_recent_chunks_for_thread("never-existed") == []

    def test_message_date_overrides_chunked_at_for_ordering(self, tmp_path):
        """Codex P1 regression: ``chunked_at`` is index-time, not
        message-time. After a reap-rebuild / dead-letter retry / full
        reindex, an OLD message's chunks can have a NEWER
        ``chunked_at`` than chunks for a recent reply — so the prior
        ordering surfaced stale content as "latest activity."

        With v18+ ``message_date`` populated, the query orders by
        ``COALESCE(message_date, chunked_at) DESC``. A scenario where
        the two columns disagree (old message reindexed later than a
        newer message arrived) must rank by message date, not insert
        time.
        """
        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        db_path = tmp_path / "msg_date_ordering.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-rebuild",
            subject="long thread that was reindexed out of order",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="thread body",
            snippet="...",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        # Old message (2024-01) was REINDEXED today (e.g. reap-rebuild
        # rewrote its chunks) — so chunked_at=NOW but message_date=
        # 2024-01.
        _insert_chunk(
            conn,
            chunk_id="c-old-reindexed",
            message_id="m-old",
            thread_id="t-rebuild",
            text="content from january",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
            chunked_at="2026-05-13T00:00:00+00:00",
            message_date="2024-01-01T00:00:00+00:00",
        )
        # Recent reply (2024-12) was indexed in steady state — both
        # columns match.
        _insert_chunk(
            conn,
            chunk_id="c-newer-reply",
            message_id="m-newer",
            thread_id="t-rebuild",
            text="content from december",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=0,
            chunked_at="2024-12-01T00:00:00+00:00",
            message_date="2024-12-01T00:00:00+00:00",
        )
        conn.close()

        db = Database(str(db_path))
        try:
            chunks = db.get_recent_chunks_for_thread("t-rebuild", limit=2)
        finally:
            db.close()

        # Both chunks selected; ordering must reflect MESSAGE date, not
        # insert date. Oldest-first in display order (the function
        # reverses the SELECT). The pre-fix behavior would have picked
        # c-old-reindexed as "newer" because its chunked_at is today.
        assert [c.chunk_id for c in chunks] == [
            "c-old-reindexed",
            "c-newer-reply",
        ], (
            "ordering must be by message_date (oldest-first in display), "
            "not chunked_at — see Codex P1 finding on summarize_thread"
        )

    def test_legacy_null_message_date_falls_back_to_chunked_at(self, tmp_path):
        """v17 legacy chunks have ``message_date IS NULL``; the
        downstream ``COALESCE(message_date, chunked_at)`` must keep
        the old ``chunked_at`` ordering for those rows so an unmigrated
        install still gets a stable timeline (even if not strictly
        correct after rebuilds).
        """
        from tests.conftest import _build_schema, _insert_chunk, _insert_thread

        db_path = tmp_path / "legacy_null.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-legacy",
            subject="legacy thread",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="thread body",
            snippet="...",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_chunk(
            conn,
            chunk_id="c-legacy-old",
            message_id="m-old",
            thread_id="t-legacy",
            text="legacy old",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunked_at="2024-01-01T00:00:00+00:00",
            message_date=None,
        )
        _insert_chunk(
            conn,
            chunk_id="c-legacy-new",
            message_id="m-new",
            thread_id="t-legacy",
            text="legacy new",
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunked_at="2024-12-01T00:00:00+00:00",
            message_date=None,
        )
        conn.close()

        db = Database(str(db_path))
        try:
            chunks = db.get_recent_chunks_for_thread("t-legacy", limit=2)
        finally:
            db.close()

        # With both message_date NULL, the COALESCE fallback uses
        # chunked_at — same ordering as the pre-fix behavior.
        assert [c.chunk_id for c in chunks] == ["c-legacy-old", "c-legacy-new"]

    def test_attachment_chunks_excluded(self, tmp_path):
        """Privacy contract: ``summarize_thread`` is a body summary, so
        ``get_recent_chunks_for_thread`` must return body chunks only.
        Attachment-text retrieval is reserved to ``ask_mailbox`` — a
        thread carrying both kinds of chunk must yield only the body
        chunk here, or attachment extracts would silently reach a
        remote inference endpoint via the summary path.
        """
        from tests.conftest import (
            _build_schema,
            _insert_attachment,
            _insert_chunk,
            _insert_thread,
        )

        db_path = tmp_path / "body_only.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-mixed",
            subject="thread with an attachment",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="body",
            snippet="...",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_attachment(
            conn,
            message_id="m-mixed",
            thread_id="t-mixed",
            attachment_id="content-hash-mixed",
            filename="report.pdf",
            occurrence_id="occ-mixed",
        )
        _insert_chunk(
            conn,
            chunk_id="c-body",
            message_id="m-mixed",
            thread_id="t-mixed",
            text="body chunk text",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_chunk(
            conn,
            chunk_id="c-attach",
            message_id="m-mixed",
            thread_id="t-mixed",
            text="attachment chunk text",
            embedding=[1.0, 0.0, 0.0, 0.0],
            attachment_id="content-hash-mixed",
        )
        conn.close()

        db = Database(str(db_path))
        try:
            chunks = db.get_recent_chunks_for_thread("t-mixed", limit=10)
        finally:
            db.close()

        assert [c.chunk_id for c in chunks] == ["c-body"], (
            "get_recent_chunks_for_thread must exclude attachment chunks; "
            "attachment-text retrieval is reserved to ask_mailbox"
        )
        assert chunks[0].attachment_id is None


class TestAttachmentProvenanceJoin:
    """Codex P2: when the same content hash is attached under multiple
    display filenames in one message, the indexer stores ONE chunk set
    (deduplicated by content hash) but multiple ``attachments`` rows
    (one per occurrence). A naive ``ON (attachment_id, message_id)``
    JOIN multiplies the chunk row by the occurrence count and yields
    non-deterministic filename attribution. Each attachment-joining
    chunk-fetch path (``_chunk_vector_search`` and
    ``get_evidence_chunks_for_threads``) must anchor on the SINGLE
    representative ``attachments`` row per pair (the one with the
    lowest ``attachment_occurrence_id``). ``get_recent_chunks_for_thread``
    is body-only and no longer joins ``attachments``.
    """

    def _build_duplicate_attachment_db(self, tmp_path):
        from tests.conftest import (
            _build_schema,
            _insert_attachment,
            _insert_chunk,
            _insert_thread,
        )

        db_path = tmp_path / "dupe_attach.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)

        _insert_thread(
            conn,
            thread_id="t-dupe",
            subject="duplicate attachment",
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            body_text="body",
            snippet="...",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        # Same content hash, same message_id, TWO occurrences with
        # different filenames. occurrence_id "occ-a" sorts lower than
        # "occ-b" so MIN(occurrence_id) → "occ-a" → invoice-a.pdf is
        # the canonical attribution.
        _insert_attachment(
            conn,
            message_id="m-dupe",
            thread_id="t-dupe",
            attachment_id="content-hash-dupe",
            filename="invoice-a.pdf",
            occurrence_id="occ-a",
        )
        _insert_attachment(
            conn,
            message_id="m-dupe",
            thread_id="t-dupe",
            attachment_id="content-hash-dupe",
            filename="invoice-b.pdf",
            occurrence_id="occ-b",
        )
        # One attachment chunk for the deduplicated content.
        _insert_chunk(
            conn,
            chunk_id="c-att-dupe",
            message_id="m-dupe",
            thread_id="t-dupe",
            text="invoice number 12345",
            embedding=[1.0, 0.0, 0.0, 0.0],
            attachment_id="content-hash-dupe",
        )
        conn.close()
        return Database(str(db_path))

    def test_chunk_vector_search_returns_single_row_per_chunk(self, tmp_path):
        # ``_chunk_vector_search`` is the dense-retrieval lane for the
        # chunk fusion path. The same MIN-occurrence anchor applies.
        db = self._build_duplicate_attachment_db(tmp_path)
        try:
            chunks = db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], limit=10)
        finally:
            db.close()
        assert len(chunks) == 1, (
            f"vector-search chunk row must not multiply by attachment-"
            f"occurrence count; got {len(chunks)} rows for one chunk"
        )
        assert chunks[0].attachment_filename == "invoice-a.pdf"

    def test_evidence_chunks_returns_single_row_per_chunk(self, tmp_path):
        # ``get_evidence_chunks_for_threads`` is the with_evidence=True
        # path the chunk fusion uses. Same MIN-occurrence anchor.
        db = self._build_duplicate_attachment_db(tmp_path)
        try:
            evidence = db.get_evidence_chunks_for_threads(
                ["t-dupe"],
                [1.0, 0.0, 0.0, 0.0],
                per_thread_limit=10,
            )
        finally:
            db.close()
        chunks = evidence.get("t-dupe", [])
        assert len(chunks) == 1, (
            f"per-thread evidence chunk row must not multiply by "
            f"attachment-occurrence count; got {len(chunks)} rows for "
            f"one chunk"
        )
        assert chunks[0].attachment_filename == "invoice-a.pdf"


class TestHybridSearchChunkLane:
    def test_chunk_specific_query_lifts_parent_thread(self, chunked_db: Database):
        """A query whose terms appear in a chunk but not the thread body
        must still surface the parent thread because the chunk lane lifts
        it into the merged ranking."""
        results = chunked_db.hybrid_search(
            query_text="invoice number 12345",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
        )
        assert results
        assert results[0].thread_id == "t-alpha"

    def test_with_evidence_populates_evidence_chunks(self, chunked_db: Database):
        results = chunked_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            with_evidence=True,
        )
        assert results
        top = results[0]
        assert top.evidence_chunks
        for chunk in top.evidence_chunks:
            assert chunk.thread_id == top.thread_id
            assert chunk.text

    def test_without_evidence_evidence_chunks_stays_empty(self, chunked_db: Database):
        results = chunked_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
        )
        assert results
        assert all(r.evidence_chunks == [] for r in results)

    def test_thread_without_chunks_still_searchable(self, chunked_db: Database):
        # t-gamma has no chunks (e.g. an empty-body message). A query
        # aligned with its thread vector + body keyword must still
        # return it via the BM25 + thread-vector lanes.
        results = chunked_db.hybrid_search(
            query_text="meeting notes",
            query_embedding=[0.0, 0.0, 1.0, 0.0],
            limit=5,
        )
        assert any(r.thread_id == "t-gamma" for r in results)


class TestRRFChunkLifting:
    def test_only_best_rank_per_thread_counts(self, chunked_db: Database):
        """Multiple chunks from the same thread must not double-count: the
        best (lowest rank) chunk per thread is the only one credited to
        thread ranking. Otherwise a thread with many similar chunks
        would dominate via accumulated score rather than relevance.
        """
        from src.lib.sqlite import ChunkResult

        chunks = [
            ChunkResult(
                chunk_id=f"x{i}",
                message_id="t-alpha",
                thread_id="t-alpha",
                chunk_index=i,
                text="x",
                char_start=0,
                char_end=1,
            )
            for i in range(3)
        ] + [
            ChunkResult(
                chunk_id="y0",
                message_id="t-beta",
                thread_id="t-beta",
                chunk_index=0,
                text="y",
                char_start=0,
                char_end=1,
            )
        ]

        fused = chunked_db._reciprocal_rank_fusion([], [], chunks)
        # t-alpha ranks first (its best chunk is at index 0).
        assert fused[0].thread_id == "t-alpha"
        # Score reflects dedup: 1/(k+rank+1) for rank 0, k=60. If we'd
        # accumulated all three of t-alpha's chunks, the score would be
        # 1/61 + 1/62 + 1/63 — meaningfully larger than this.
        assert fused[0].score == pytest.approx(1.0 / 61, rel=1e-6)
        beta = next(r for r in fused if r.thread_id == "t-beta")
        assert beta.score == pytest.approx(1.0 / 64, rel=1e-6)


class TestFindContact:
    """The find_contact aggregator powers the LLM's name → email lookup
    so a borderline model can resolve a display-name fragment before
    passing ``from_addr`` to search_emails. Each test pins a behavior
    the tool description implicitly promises.
    """

    def test_match_by_address_substring(self, seeded_db: Database):
        # ``alice@example.com`` appears in t-alpha (sender) and t-beta
        # (participant). The query ``"alice"`` matches both — count is 2.
        results = seeded_db.find_contact("alice")
        assert len(results) == 1
        assert results[0]["email"] == "alice@example.com"
        assert results[0]["thread_count"] == 2

    def test_match_by_domain_fragment(self, seeded_db: Database):
        # ``@example.com`` should pull every distinct address sharing
        # that domain — alice, bob, carol, dave (one each across the
        # three seeded threads, with alice doubled).
        results = seeded_db.find_contact("@example.com")
        emails = {r["email"] for r in results}
        assert emails == {
            "alice@example.com",
            "bob@example.com",
            "carol@example.com",
            "dave@example.com",
        }

    def test_match_uses_display_name_when_present(self, seeded_db: Database, tmp_path):
        # The seeded fixtures use bare addresses with no display names,
        # so reseed a tiny DB with a quoted display name + parenthetical
        # role suffix to exercise the parseaddr branch that pulls a name
        # out of the wrapper.
        from tests.conftest import _build_schema, _insert_thread

        path = tmp_path / "named.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-named",
            subject="hi",
            participants=['"Jane Smith (Acct)" <jsmith@example.com>'],
            senders=['"Jane Smith (Acct)" <jsmith@example.com>'],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        try:
            results = db.find_contact("jane")
            assert len(results) == 1
            assert results[0]["email"] == "jsmith@example.com"
            assert "Jane Smith (Acct)" in results[0]["names"]
        finally:
            db.close()

    def test_results_sorted_by_thread_count_desc(self, seeded_db: Database):
        # alice appears in 2 threads; bob, carol, dave in 1 each.
        # Sort key is ``(-count, email)`` so alice leads regardless of
        # alphabetical position.
        results = seeded_db.find_contact("@example.com")
        counts = [r["thread_count"] for r in results]
        assert counts == sorted(counts, reverse=True)
        assert results[0]["email"] == "alice@example.com"

    def test_same_thread_does_not_double_count(self, seeded_db: Database, tmp_path):
        # If a participant appears twice in one thread's JSON (Bridge
        # has been observed to emit duplicates after thread merges),
        # the contact should still count once for that thread.
        from tests.conftest import _build_schema, _insert_thread

        path = tmp_path / "dup.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t-dup",
            subject="hi",
            # Same address listed twice — the dedupe inside the loop
            # must collapse this to one increment.
            participants=["alice@example.com", "alice@example.com"],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        try:
            results = db.find_contact("alice")
            assert len(results) == 1
            assert results[0]["thread_count"] == 1
        finally:
            db.close()

    def test_no_match_returns_empty_list(self, seeded_db: Database):
        assert seeded_db.find_contact("nobodywiththisname") == []

    def test_empty_query_returns_empty_list(self, seeded_db: Database):
        # Whitespace-only or empty query is a no-op rather than a
        # full-table scan that returns every contact.
        assert seeded_db.find_contact("") == []
        assert seeded_db.find_contact("   ") == []

    def test_query_is_case_insensitive(self, seeded_db: Database):
        upper = seeded_db.find_contact("ALICE")
        lower = seeded_db.find_contact("alice")
        assert upper == lower

    def test_limit_caps_result_count(self, seeded_db: Database):
        # Four distinct ``@example.com`` contacts in seeded_db. With
        # limit=2 only the two highest-ranked should return.
        results = seeded_db.find_contact("@example.com", limit=2)
        assert len(results) == 2

    def test_malformed_participants_json_skipped(self, seeded_db: Database, tmp_path):
        # A thread with corrupt JSON in ``participants`` should be
        # skipped rather than crashing the whole aggregation. Drop in
        # a row by hand to bypass the writer's normal JSON encoding.
        from tests.conftest import _build_schema

        path = tmp_path / "bad-json.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        conn.execute(
            """INSERT INTO threads (
                thread_id, subject, participants, senders, folder,
                date_first, date_last, message_ids, snippet,
                has_attachments, body_text, fts_rowid, display_subject
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "t-broken",
                "broken",
                "{not valid json",
                "[]",
                "INBOX",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                "[]",
                "",
                0,
                "",
                None,
                None,
            ),
        )
        # Add a valid neighbor so we can confirm the loop continued
        # past the broken row instead of bailing out.
        conn.execute(
            """INSERT INTO threads (
                thread_id, subject, participants, senders, folder,
                date_first, date_last, message_ids, snippet,
                has_attachments, body_text, fts_rowid, display_subject
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "t-good",
                "ok",
                '["good@example.com"]',
                "[]",
                "INBOX",
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                "[]",
                "",
                0,
                "",
                None,
                None,
            ),
        )
        conn.commit()
        conn.close()

        db = Database(str(path))
        try:
            results = db.find_contact("good")
            assert len(results) == 1
            assert results[0]["email"] == "good@example.com"
        finally:
            db.close()


class TestFindContactSendersOnly:
    """``senders_only=True`` narrows the aggregation to From-line
    addresses. The default (False) ranks across all participants and
    can promote a recipient-only contact above the actual sender —
    correct for "find this person's address anywhere" but wrong for
    "filter to messages this person sent". Each test pins the
    distinction.
    """

    def test_senders_only_excludes_recipient_only_contact(self, tmp_path):
        # Build a small DB where one contact is ONLY a recipient,
        # never a sender. With the default search they should still
        # show up; with senders_only they should not. seeded_db's
        # fixtures are too uniform for this — we want a thread where
        # the participants list contains a contact whose address is
        # NOT in the senders list.
        from tests.conftest import _build_schema, _insert_thread

        path = tmp_path / "senders-only.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        # One thread: alice is the sender, smith is a CC. From the
        # ``threads.senders`` JSON only alice appears; from
        # ``threads.participants`` both appear.
        _insert_thread(
            conn,
            thread_id="t-cc",
            subject="quarterly",
            participants=["alice@example.com", "smith@example.com"],
            senders=["alice@example.com"],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        try:
            # Default behavior: smith shows up because they're a
            # participant on a thread.
            assert db.find_contact("smith")[0]["email"] == "smith@example.com"
            # senders_only=True: smith disappears because they were
            # never a From-line address.
            assert db.find_contact("smith", senders_only=True) == []
            # alice still resolves under both modes.
            assert db.find_contact("alice")[0]["email"] == "alice@example.com"
            assert db.find_contact("alice", senders_only=True)[0]["email"] == "alice@example.com"
        finally:
            db.close()

    def test_senders_only_default_is_false_for_back_compat(self, seeded_db: Database):
        # The standalone find_contact MCP tool relies on the broader
        # participants ranking by default — it's used for general
        # "find this person's email" lookups where recipient-only
        # matches are still useful. Pin the default explicitly so a
        # future refactor that flips it requires updating this test.
        seeded = seeded_db.find_contact("alice")
        explicit = seeded_db.find_contact("alice", senders_only=False)
        assert seeded == explicit

    def test_senders_only_thread_count_reflects_send_frequency(self, tmp_path):
        # When the same address sends some threads and only receives
        # others, senders_only's thread_count should reflect the
        # smaller "sent" count rather than the larger "appeared on"
        # count. A from_name caller wants the address with the most
        # SENT messages.
        from tests.conftest import _build_schema, _insert_thread

        path = tmp_path / "send-count.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        # alice sent two threads, was a participant on a third.
        _insert_thread(
            conn,
            thread_id="t-1",
            subject="one",
            participants=["alice@example.com", "bob@example.com"],
            senders=["alice@example.com"],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        _insert_thread(
            conn,
            thread_id="t-2",
            subject="two",
            participants=["alice@example.com", "bob@example.com"],
            senders=["alice@example.com"],
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        _insert_thread(
            conn,
            thread_id="t-3",
            subject="three",
            participants=["alice@example.com", "bob@example.com"],
            senders=["bob@example.com"],
            embedding=[0.0, 0.0, 1.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        try:
            full = db.find_contact("alice", senders_only=False)
            sent = db.find_contact("alice", senders_only=True)
            # Default: alice on 3 threads (participant count).
            assert full[0]["thread_count"] == 3
            # senders_only: alice sent 2 of those 3.
            assert sent[0]["thread_count"] == 2
        finally:
            db.close()

"""Tests for src.lib.sqlite Database query layer.

Covers pure fusion/filter logic against synthetic ThreadResult lists and
real read queries against an in-memory-style database seeded via conftest.
"""

import json
import sqlite3
from contextlib import closing

import pytest
from src.lib.sqlite import Database, address_match_mode, canonical_addr

from tests.conftest import write_ingestion


class TestReadOnlyConnection:
    def test_write_attempt_raises(self, seeded_db: Database):
        """The MCP reader opens SQLite via ``?mode=ro`` URI — any attempt
        to mutate the shared index must fail at the SQLite API level, not
        rely only on ``PRAGMA query_only`` being honored."""
        with closing(seeded_db._connect()) as conn:
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
                conn.execute("UPDATE threads SET subject = 'hijacked' WHERE thread_id = 't-alpha'")

    def test_reads_still_work(self, seeded_db: Database):
        with closing(seeded_db._connect()) as conn:
            row = conn.execute(
                "SELECT subject FROM threads WHERE thread_id = ?", ("t-alpha",)
            ).fetchone()
        assert row["subject"] == "invoice for march"


class TestUriSpecialCharactersInPath:
    """The filesystem path is encoded into the SQLite file URI, so
    URI-special characters in SQLITE_PATH name the file rather than
    starting a fragment or query that drops ``mode=ro`` (#311)."""

    @pytest.mark.parametrize(
        "name",
        ["mail#copy.db", "mail?copy.db", "mail%23copy.db", "mail copy.db", "mäil-コピー.db"],
    )
    def test_opens_intended_file_read_only_without_creating_sibling(self, tmp_path, name):
        target = tmp_path / name
        with closing(sqlite3.connect(target)) as seed:
            seed.execute("CREATE TABLE intended_marker (x INTEGER)")
            seed.commit()
        before = sorted(p.name for p in tmp_path.iterdir())

        with closing(Database(str(target))._connect()) as conn:
            files = [row["file"] for row in conn.execute("PRAGMA database_list")]
            tables = [
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
                conn.execute("CREATE TABLE should_fail (x INTEGER)")

        assert files == [str(target)]
        assert "intended_marker" in tables
        assert sorted(p.name for p in tmp_path.iterdir()) == before


class TestPing:
    def test_ping_succeeds_on_healthy_db(self, seeded_db: Database):
        # Returns None on success; no exception is the signal.
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
        assert db.get_embedding_dim() is None


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
        results = db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=5)
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

    def __init__(self, scores_by_index: dict[int, float], candidates: int = 50):
        self.candidates = candidates
        self._scores = scores_by_index
        self._last_query: str | None = None
        self._last_docs: list[str] | None = None
        self._last_top_n: int | None = None

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[tuple[int, float]]:
        self._last_query = query
        self._last_docs = list(documents)
        self._last_top_n = top_n
        scored = [(i, self._scores.get(i, 0.0)) for i in range(len(documents))]
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]


class _BrokenReranker:
    """Stub that always reports failure so we can verify the RRF
    fallback path keeps results from disappearing."""

    candidates = 50

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
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
        scripted = _IndexScoringReranker(scores_by_index={}, candidates=10)
        seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=3,
            reranker=scripted,
        )
        assert scripted._last_query == "invoice"
        assert scripted._last_docs is not None
        assert all(d.startswith("Subject: ") for d in scripted._last_docs)

    def test_caller_limit_is_passed_to_reranker_as_top_n(self, seeded_db: Database):
        # The rerank stage's cutoff is the caller's ``limit``: a caller
        # asking for ``limit=3`` gets ``top_n=3`` on the rerank call, so
        # the reranker never undercuts the caller's requested result
        # set (e.g. ``extract_from_emails(limit=20)``).
        scripted = _IndexScoringReranker(
            scores_by_index={0: 0.9, 1: 0.5, 2: 0.1},
            candidates=10,
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

    @pytest.mark.parametrize(
        "scored",
        [
            [(99, 0.9)],  # out of range
            [(-1, 0.9)],  # negative
            [(0, 0.9), (0, 0.8)],  # duplicate
            [(1, 0.9), (99, 0.8)],  # one valid, one out of range
        ],
    )
    def test_invalid_rerank_indices_fall_back_to_rrf_order(self, seeded_db: Database, scored):
        # #225: an invalid ranking is a failed rerank. Applying it anyway
        # dropped every result (out of range) or returned one thread twice
        # (duplicate); the whole RRF slice must survive, scores untouched.
        rrf_only = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
        )
        assert len(rrf_only) == 2, "fixture must surface at least 2 candidates"
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
            reranker=_ScriptedReranker(scored),
        )
        assert [r.thread_id for r in results] == [r.thread_id for r in rrf_only]
        assert [r.score for r in results] == [r.score for r in rrf_only]
        assert all("rerank" not in r.lane_ranks for r in results)

    def test_invalid_cohere_indices_fall_back_to_rrf_order(self, seeded_db: Database):
        # Same path through the real client, with the SDK call stubbed to
        # return a duplicate index as a malformed gateway response would.
        from types import SimpleNamespace

        from src.lib.reranker import CohereReranker, RerankConfig

        reranker = CohereReranker(
            RerankConfig(
                base_url="",
                model="rerank-v4.0-pro",
                api_key="ck-test",  # pragma: allowlist secret
                candidates=50,
            )
        )
        item = SimpleNamespace(index=0, relevance_score=0.9)
        reranker.client.rerank = lambda **_: SimpleNamespace(results=[item, item])  # type: ignore[method-assign]
        rrf_only = seeded_db.hybrid_search(
            query_text="invoice", query_embedding=[1.0, 0.0, 0.0, 0.0], limit=2
        )
        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=2,
            reranker=reranker,
        )
        assert [r.thread_id for r in results] == [r.thread_id for r in rrf_only]


class _ScriptedReranker:
    """Stub returning a fixed ``[(index, score), ...]`` regardless of input."""

    candidates = 50

    def __init__(self, scored: list[tuple[int, float]]):
        self._scored = scored

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[tuple[int, float]]:
        return list(self._scored)


class TestDirectLookups:
    def test_get_thread_returns_result(self, seeded_db: Database):
        thread = seeded_db.get_thread("t-alpha")
        assert thread is not None
        assert thread.subject == "invoice for march"
        assert thread.has_attachments is True

    def test_get_thread_missing_returns_none(self, seeded_db: Database):
        assert seeded_db.get_thread("does-not-exist") is None

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
    and falls back to the normalized ``subject`` while it is ``NULL``."""

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
        result = db.get_thread("t-display")
        assert result is not None
        assert result.subject == "Today's Meeting"

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
        result = db.get_thread("t-legacy")
        assert result is not None
        assert result.subject == "legacy lowercased subject"

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
        results = db.keyword_search("agenda")
        assert len(results) == 1
        assert results[0].subject == "Today's Meeting"

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
        results = db.semantic_search([1.0, 0.0, 0.0, 0.0])
        assert len(results) == 1
        assert results[0].subject == "Today's Meeting"

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
        results = db.hybrid_search(
            query_text="agenda",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
        )
        assert len(results) == 1
        assert results[0].subject == "Today's Meeting"


class TestStatsAndFolders:
    def test_get_mailbox_status(self, seeded_db: Database):
        write_ingestion(
            seeded_db.path,
            sync_completed_at="2026-09-28T12:00:00+00:00",
            sync_interval_secs=60,
            indexer_seen_at="2026-09-28T12:00:10+00:00",
            jobs=(
                ("queued", 0, None),
                ("queued", 0, None),
                ("queued", 2, "retryable"),
                # Deferred during an embedder outage: failed, but no
                # attempt was spent.
                ("queued", 0, "operator_action_required"),
                ("dead", 5, "retryable"),
            ),
        )
        stats = seeded_db.get_mailbox_status()
        assert stats["total_threads"] == 3
        assert stats["total_messages"] == 3
        assert stats["oldest_message"] is not None
        assert stats["newest_message"] is not None
        assert stats["queue"] == {"pending": 2, "retrying": 2, "dead": 1}
        assert stats["ingestion"] == {
            "sync_completed_at": "2026-09-28T12:00:00+00:00",
            "sync_interval_secs": 60,
            "indexer_seen_at": "2026-09-28T12:00:10+00:00",
        }

    def test_get_mailbox_status_before_the_indexer_reports(self, empty_db: Database):
        stats = empty_db.get_mailbox_status()
        assert stats["queue"] == {"pending": 0, "retrying": 0, "dead": 0}
        assert stats["ingestion"] is None

    def test_list_folders_ranked_by_thread_count(self, seeded_db: Database):
        folders = seeded_db.list_folders()
        names = [f["name"] for f in folders]
        assert names[0] == "INBOX"
        assert {"name": "Archive", "thread_count": 1} in folders


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


def _nfd(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFD", text)


# (indexed text, query) pairs every keyword lane must match. Combining
# marks are word characters to unicode61, so the sanitizer must keep them
# inside a token rather than split the word around them.
_COMBINING_MARK_CASES = [
    ("r\u00e9sum\u00e9", "r\u00e9sum\u00e9"),
    ("r\u00e9sum\u00e9", _nfd("r\u00e9sum\u00e9")),
    (_nfd("r\u00e9sum\u00e9"), "r\u00e9sum\u00e9"),
    ("na\u00efve", _nfd("na\u00efve") + "?"),
    ("Vi\u1ec7t", "Vi\u1ec7t"),
    (_nfd("Vi\u1ec7t"), _nfd("Vi\u1ec7t")),
    # Known gap, not a query-side one: the indexes tokenize with
    # unicode61 remove_diacritics=1, which leaves a precomposed letter
    # with two diacritics (U+1EC7) unfolded while its decomposed spelling
    # folds to "viet". Matching across the two needs index-side
    # normalization or remove_diacritics=2, i.e. a reindex.
    pytest.param(
        ("Vi\u1ec7t", _nfd("Vi\u1ec7t")),
        marks=pytest.mark.xfail(strict=True, reason="needs index-side folding (reindex)"),
    ),
    pytest.param(
        (_nfd("Vi\u1ec7t"), "Vi\u1ec7t"),
        marks=pytest.mark.xfail(strict=True, reason="needs index-side folding (reindex)"),
    ),
    # Hangul is not folded by unicode61 at all, so composed syllables and
    # their conjoining jamo are different tokens: the same gap.
    pytest.param(
        ("\ud55c\uad6d", _nfd("\ud55c\uad6d")),
        marks=pytest.mark.xfail(strict=True, reason="needs index-side folding (reindex)"),
    ),
    # No precomposed form exists for q + combining tilde.
    ("q\u0303uux", "q\u0303uux"),
    # Devanagari vowel signs and virama are marks (Mn / Mc).
    ("\u0939\u093f\u0928\u094d\u0926\u0940", "\u0939\u093f\u0928\u094d\u0926\u0940"),
    ("jos\u00e9@example.com", _nfd("jos\u00e9@example.com")),
]


class TestFtsSanitizationCombiningMarks:
    def test_marks_stay_inside_their_token(self):
        from src.lib.sqlite import _sanitize_fts_query

        assert _sanitize_fts_query(_nfd("r\u00e9sum\u00e9 cv")) == (
            '"' + _nfd("r\u00e9sum\u00e9") + '" OR "cv"'
        )
        hindi = "\u0939\u093f\u0928\u094d\u0926\u0940"
        assert _sanitize_fts_query(hindi) == f'"{hindi}"'

    def test_token_characters_unchanged_outside_marks(self):
        # Differential over every code point: apart from combining marks,
        # a character is a token character exactly when the previous
        # ``[\w@.\-]`` class said so.
        import re
        import unicodedata

        from src.lib.sqlite import _is_fts_query_token_char

        old = re.compile(r"[\w@.\-]")
        diverging = [
            cp
            for cp in range(0x110000)
            if not unicodedata.category(chr(cp)).startswith("M")
            and bool(old.fullmatch(chr(cp))) != _is_fts_query_token_char(chr(cp))
        ]
        assert diverging == []

    def test_lone_mark_query_runs(self, seeded_db: Database):
        # A token of marks alone tokenizes to nothing in FTS; the query
        # must still run rather than raise.
        assert seeded_db.keyword_search("\u0301") == []

    @pytest.fixture(params=_COMBINING_MARK_CASES, ids=ascii)
    def lanes_db(self, request, tmp_path):
        from tests.conftest import _insert_attachment, _insert_message, _insert_thread

        indexed, query = request.param
        conn, path = _open_built_db_conn(tmp_path, "marks.db")
        _insert_thread(conn, thread_id="t-thread", subject=indexed, participants=[])
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t-chunk",
            sent_at="2024-01-01T00:00:00+00:00",
            body=f"about {indexed} here",
        )
        _insert_attachment(
            conn,
            message_id="m1",
            thread_id="t-chunk",
            attachment_id="a1",
            filename=f"{indexed}.pdf",
        )
        conn.close()
        return Database(str(path)), query

    def test_thread_lane(self, lanes_db):
        db, query = lanes_db
        assert [r.thread_id for r in db._thread_keyword_search(query, 10)] == ["t-thread"]

    def test_chunk_lane(self, lanes_db):
        db, query = lanes_db
        assert [r.thread_id for r in db._chunk_keyword_search(query, 10)] == ["t-chunk"]

    def test_attachment_lane(self, lanes_db):
        db, query = lanes_db
        assert [r.thread_id for r in db._attachment_keyword_search(query, 10)] == ["t-chunk"]

    def test_search_attachments(self, lanes_db):
        db, query = lanes_db
        assert [a.filename for a in db.search_attachments(query)] != []


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


class TestNonFiniteStoredVectors:
    """A NaN vector stored before the indexer rejected them (#232) gets a
    NULL distance from sqlite-vec. It must be skipped, not crash the
    thread lane or rank as a perfect match in the chunk lanes."""

    @staticmethod
    def _poison(db: Database) -> None:
        import sqlite3

        import sqlite_vec

        nan = sqlite_vec.serialize_float32([float("nan"), 0.0, 0.0, 0.0])
        conn = sqlite3.connect(db.path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.execute("UPDATE threads_vec SET embedding = ? WHERE thread_id = 't-alpha'", (nan,))
        conn.execute(
            "UPDATE message_chunks_vec SET embedding = ? WHERE chunk_id = 'alpha-c1'", (nan,)
        )
        conn.commit()
        conn.close()

    def test_thread_lane_skips_null_distance(self, chunked_db: Database):
        self._poison(chunked_db)
        results = chunked_db.semantic_search([0.0, 1.0, 0.0, 0.0], limit=50)
        ids = [r.thread_id for r in results]
        assert "t-beta" in ids
        assert "t-alpha" not in ids

    def test_chunk_lane_skips_null_distance(self, chunked_db: Database):
        self._poison(chunked_db)
        results = chunked_db._chunk_vector_search([0.0, 1.0, 0.0, 0.0], limit=50)
        assert results
        assert all(r.chunk_id != "alpha-c1" for r in results)

    def test_evidence_skips_null_distance(self, chunked_db: Database):
        self._poison(chunked_db)
        grouped = chunked_db.get_evidence_chunks_for_threads(
            ["t-alpha", "t-beta"], [0.0, 1.0, 0.0, 0.0]
        )
        assert grouped["t-alpha"] == []
        assert grouped["t-beta"]


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
        # Ask for evidence on the carrier specifically — what
        # ``hybrid_search(with_evidence=True)`` does after the
        # carrier wins via thread-vec / BM25 / metadata.
        evidence = db.get_evidence_chunks_for_threads(
            thread_ids=["t-carrier"],
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

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
        evidence = db.get_evidence_chunks_for_threads(
            thread_ids=["t-quote"],
            embedding=[0.0, 0.0, 0.0, 1.0],
            per_thread_limit=5,
        )
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
        # Query embedding is aligned with the BODY chunk — pure
        # dense ranking would surface body first.
        evidence_bias = db.get_evidence_chunks_for_threads(
            thread_ids=["t-quote"],
            embedding=[1.0, 0.0, 0.0, 0.0],
            per_thread_limit=1,
            matched_attachments={"t-quote": ["att-quote"]},
        )
        evidence_no_bias = db.get_evidence_chunks_for_threads(
            thread_ids=["t-quote"],
            embedding=[1.0, 0.0, 0.0, 0.0],
            per_thread_limit=1,
        )
        # With the attachment-won bias, slot-0 is the attachment chunk.
        assert evidence_bias["t-quote"][0].attachment_id == "att-quote"
        # Without the bias, slot-0 is the body chunk (regression guard).
        assert evidence_no_bias["t-quote"][0].attachment_id is None

    def _add_competing_attachments(self, db):
        """Three more attachments in t-quote whose chunks sit closer to
        the query embedding than the matched attachment's chunk."""
        import sqlite_vec

        from tests.conftest import _insert_attachment, _insert_chunk

        with closing(sqlite3.connect(db.path)) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            for i in range(3):
                _insert_attachment(
                    conn,
                    message_id="t-quote",
                    thread_id="t-quote",
                    attachment_id=f"att-other-{i}",
                    filename=f"exhibit-{i}.pdf",
                )
                _insert_chunk(
                    conn,
                    chunk_id=f"t-quote-other-{i}",
                    message_id="t-quote",
                    thread_id="t-quote",
                    text=f"exhibit {i} boilerplate",
                    embedding=[0.9, 0.1, 0.0, 0.0],
                    chunk_index=10 + i,
                    attachment_id=f"att-other-{i}",
                )

    def test_matched_attachment_leads_over_other_attachments(self, tmp_path):
        """Regression (#215): only the thread was remembered, so every
        attachment chunk in it was floated and the cap could keep three
        unrelated exhibits instead of the document the filename matched."""
        db = self._build_attachment_carrier_db(tmp_path)
        self._add_competing_attachments(db)
        evidence = db.get_evidence_chunks_for_threads(
            thread_ids=["t-quote"],
            embedding=[1.0, 0.0, 0.0, 0.0],
            per_thread_limit=3,
            matched_attachments={"t-quote": ["att-quote"]},
        )
        assert evidence["t-quote"][0].attachment_id == "att-quote"

    def test_filename_hit_contributes_evidence_end_to_end(self, tmp_path):
        db = self._build_attachment_carrier_db(tmp_path)
        self._add_competing_attachments(db)
        results = db.hybrid_search(
            query_text="proposal-quote",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
            with_evidence=True,
        )
        quote = next(r for r in results if r.thread_id == "t-quote")
        assert "18450" in quote.evidence_chunks[0].text

    def test_a_generic_mime_word_does_not_dilute_the_named_file(self, tmp_path):
        """Review round 1: attachments_fts indexes MIME types and query
        words are OR'd, so "proposal-quote pdf" also matched every other
        PDF in the thread; the named file must still lead."""
        db = self._build_attachment_carrier_db(tmp_path)
        self._add_competing_attachments(db)
        results = db.hybrid_search(
            query_text="proposal-quote pdf",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
            with_evidence=True,
        )
        quote = next(r for r in results if r.thread_id == "t-quote")
        assert "18450" in quote.evidence_chunks[0].text

    def test_hybrid_search_with_evidence_biases_filename_winners(self, tmp_path):
        """End-to-end: a query whose tokens hit the attachment-FTS lane
        must produce evidence chunks with the attachment chunk first."""
        db = self._build_attachment_carrier_db(tmp_path)
        # ``proposal-quote`` matches the attachment filename FTS.
        results = db.hybrid_search(
            query_text="proposal-quote",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
            with_evidence=True,
        )
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
        chunks = db.get_recent_chunks_for_thread("t-tl", limit=2)
        # Selection picked the two newest by ``chunked_at DESC``;
        # output reverses so the prompt reads oldest-first.
        assert [c.chunk_id for c in chunks] == ["c-mid", "c-new"]

    def test_limit_zero_returns_empty(self, tmp_path):
        db = self._build_chunked_db_with_timeline(tmp_path)
        assert db.get_recent_chunks_for_thread("t-tl", limit=0) == []

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

        The query orders by ``message_date DESC``. A scenario where
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
        chunks = db.get_recent_chunks_for_thread("t-rebuild", limit=2)

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
        chunks = db.get_recent_chunks_for_thread("t-mixed", limit=10)

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
        chunks = db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], limit=10)
        assert len(chunks) == 1, (
            f"vector-search chunk row must not multiply by attachment-"
            f"occurrence count; got {len(chunks)} rows for one chunk"
        )
        assert chunks[0].attachment_filename == "invoice-a.pdf"

    def test_evidence_chunks_returns_single_row_per_chunk(self, tmp_path):
        # ``get_evidence_chunks_for_threads`` is the with_evidence=True
        # path the chunk fusion uses. Same MIN-occurrence anchor.
        db = self._build_duplicate_attachment_db(tmp_path)
        evidence = db.get_evidence_chunks_for_threads(
            ["t-dupe"],
            [1.0, 0.0, 0.0, 0.0],
            per_thread_limit=10,
        )
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


class TestRRFChunkOnlyMaterialization:
    """#334: threads found only by the chunk lane are loaded in batched
    lookups over one connection, not one connection per candidate."""

    @staticmethod
    def _chunks(thread_ids: list[str]):
        from src.lib.sqlite import ChunkResult

        return [
            ChunkResult(
                chunk_id=f"c{i}",
                message_id=tid,
                thread_id=tid,
                chunk_index=0,
                text="x",
                char_start=0,
                char_end=1,
            )
            for i, tid in enumerate(thread_ids)
        ]

    def test_one_connection_and_bounded_lookups(self, seeded_db: Database, monkeypatch):
        import src.lib.sqlite as sqlite_mod

        # A batch of two forces the three found threads plus a missing
        # one across two IN-list lookups.
        monkeypatch.setattr(sqlite_mod, "_IN_CLAUSE_BATCH_SIZE", 2)
        connect = seeded_db._connect
        connections = 0
        lookups: list[str] = []

        def counting_connect():
            nonlocal connections
            connections += 1
            conn = connect()
            conn.set_trace_callback(
                lambda sql: lookups.append(sql) if "FROM threads" in sql else None
            )
            return conn

        monkeypatch.setattr(seeded_db, "_connect", counting_connect)
        order = ["t-gamma", "does-not-exist", "t-alpha", "t-beta"]
        fused = seeded_db._reciprocal_rank_fusion([], [], self._chunks(order))

        assert connections == 1
        assert len(lookups) == 2
        # Missing rows are skipped; the others keep chunk-lane order and
        # the same rows and scores a per-thread fetch gives.
        assert [r.thread_id for r in fused] == ["t-gamma", "t-alpha", "t-beta"]
        assert [r.score for r in fused] == pytest.approx([1 / 61, 1 / 63, 1 / 64])
        for r in fused:
            expected = Database(seeded_db.path).get_thread(r.thread_id)
            assert expected is not None
            assert (r.subject, r.message_ids, r.body_text) == (
                expected.subject,
                expected.message_ids,
                expected.body_text,
            )
            assert r.lane_ranks == {"chunk_vec": order.index(r.thread_id)}

    def test_threads_already_in_a_lane_are_not_fetched(self, seeded_db: Database, monkeypatch):
        bm25 = [seeded_db.get_thread("t-alpha")]
        assert bm25[0] is not None
        connections = 0
        connect = seeded_db._connect

        def counting_connect():
            nonlocal connections
            connections += 1
            return connect()

        monkeypatch.setattr(seeded_db, "_connect", counting_connect)
        fused = seeded_db._reciprocal_rank_fusion(bm25, [], self._chunks(["t-alpha"]))
        assert connections == 0
        assert [r.thread_id for r in fused] == ["t-alpha"]


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
        results = db.find_contact("jane")
        assert len(results) == 1
        assert results[0]["email"] == "jsmith@example.com"
        assert "Jane Smith (Acct)" in results[0]["names"]

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
        results = db.find_contact("alice")
        assert len(results) == 1
        assert results[0]["thread_count"] == 1

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

    def test_reads_participant_index_not_thread_json(self, tmp_path):
        # find_contact aggregates ``message_participants``: a contact
        # present only there (the thread's JSON participant list is
        # empty) is still found, and names from every message collect.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "participants.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Jane Doe <jane@example.com>"],
        )
        _insert_message(
            conn,
            message_id="m2",
            thread_id="t1",
            sent_at="2024-01-02T00:00:00+00:00",
            to=["J. Doe <JANE@example.com>"],
        )
        conn.close()
        db = Database(str(path))
        results = db.find_contact("jane")
        assert results == [
            {"email": "jane@example.com", "names": ["J. Doe", "Jane Doe"], "thread_count": 1}
        ]

    def test_name_match_reports_the_whole_contact(self, tmp_path):
        # The name selects the address; names and thread_count then
        # describe every row for that address, not just the rows whose
        # name matched. A repeat within one thread still counts once.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "aliases.db")
        for n, name in enumerate(["Jane Smith", "J. Smith", "Janet Doe"]):
            _insert_message(
                conn,
                message_id=f"m{n}",
                thread_id=f"t{n}",
                sent_at="2024-01-01T00:00:00+00:00",
                from_=[f"{name} <person@example.test>"],
            )
        _insert_message(
            conn,
            message_id="m3",
            thread_id="t0",
            sent_at="2024-01-02T00:00:00+00:00",
            to=["Jane Smith <person@example.test>"],
        )
        conn.close()
        db = Database(str(path))
        whole = {
            "email": "person@example.test",
            "names": ["J. Smith", "Jane Smith", "Janet Doe"],
            "thread_count": 3,
        }
        assert db.find_contact("Jane Smith") == [whole]
        assert db.find_contact("person@example.test") == [whole]

    def test_ranking_counts_unmatched_aliases(self, tmp_path):
        # alpha matched as "Pat" on one thread but appears on three; beta
        # matched on two. Ranking on matching rows alone put pat-b first.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "ranking.db")
        for n, name in enumerate(["Pat Alpha", "A. Alpha", "Alpha"]):
            _insert_message(
                conn,
                message_id=f"a{n}",
                thread_id=f"ta{n}",
                sent_at="2024-01-01T00:00:00+00:00",
                from_=[f"{name} <alpha@example.test>"],
            )
        for n in range(2):
            _insert_message(
                conn,
                message_id=f"b{n}",
                thread_id=f"tb{n}",
                sent_at="2024-01-01T00:00:00+00:00",
                from_=["Pat Beta <beta@example.test>"],
            )
        conn.close()
        db = Database(str(path))
        assert [(c["email"], c["thread_count"]) for c in db.find_contact("pat")] == [
            ("alpha@example.test", 3),
            ("beta@example.test", 2),
        ]

    def test_non_ascii_name_matches_case_insensitively(self, tmp_path):
        # SQLite's own lower() folds ASCII only; the match must fold
        # "JOSÉ" to "josé" the way Python does.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "unicode.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["José Álvarez <jose@example.com>"],
        )
        conn.close()
        db = Database(str(path))
        assert [c["email"] for c in db.find_contact("JOSÉ ÁLVAREZ")] == ["jose@example.com"]


class TestFindContactSendersOnly:
    """``senders_only=True`` narrows the aggregation to From-line
    addresses. The default (False) ranks across all participants and
    can promote a recipient-only contact above the actual sender —
    correct for "find this person's address anywhere" but wrong for
    "filter to messages this person sent". Each test pins the
    distinction.
    """

    def test_senders_only_counts_primary_author_only(self, tmp_path):
        # search_emails(from_name=...) plugs the resolved address into a
        # filter over each thread's primary From authors, so resolution
        # must rank on the same authors: a secondary author in a
        # multi-author From must not outrank a real primary sender.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "authors.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Pat Solo <solo@example.com>"],
        )
        for n in (2, 3):
            _insert_message(
                conn,
                message_id=f"m{n}",
                thread_id=f"t{n}",
                sent_at="2024-01-02T00:00:00+00:00",
                from_=["Lead <lead@example.com>", "Pat Joint <joint@example.com>"],
            )
        conn.close()
        db = Database(str(path))
        assert [c["email"] for c in db.find_contact("pat", senders_only=True)] == [
            "solo@example.com"
        ]
        # Without senders_only every role counts, secondary authors too.
        assert db.find_contact("pat")[0]["email"] == "joint@example.com"

    def test_senders_only_skips_author_behind_an_unparseable_primary(self, tmp_path):
        # ``From: invalid, Pat Joint <joint@...>``: the primary author is
        # ``invalid`` (recorded in threads.senders), which the participant
        # writer skips, so Joint is the first stored From row. Joint is
        # still not an address the search sender filter can match, so it
        # must not outrank the real sender.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "unparseable-primary.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Pat Solo <solo@example.com>"],
        )
        for n in (2, 3):
            _insert_message(
                conn,
                message_id=f"m{n}",
                thread_id=f"t{n}",
                sent_at="2024-01-02T00:00:00+00:00",
                from_=["invalid", "Pat Joint <joint@example.com>"],
            )
        conn.close()
        db = Database(str(path))
        assert [c["email"] for c in db.find_contact("pat", senders_only=True)] == [
            "solo@example.com"
        ]

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
        # Default behavior: smith shows up because they're a
        # participant on a thread.
        assert db.find_contact("smith")[0]["email"] == "smith@example.com"
        # senders_only=True: smith disappears because they were
        # never a From-line address.
        assert db.find_contact("smith", senders_only=True) == []
        # alice still resolves under both modes.
        assert db.find_contact("alice")[0]["email"] == "alice@example.com"
        assert db.find_contact("alice", senders_only=True)[0]["email"] == "alice@example.com"

    @staticmethod
    def _keep_first_sender_per_address(conn, thread_id: str) -> None:
        # The indexer dedupes ``threads.senders`` by canonical address,
        # keeping the first display string; the fixture helper dedupes by
        # exact string, so collapse the later duplicates here.
        senders = json.loads(
            conn.execute(
                "SELECT senders FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        )
        kept: dict[str, str] = {}
        for entry in senders:
            kept.setdefault(canonical_addr(entry), entry)
        conn.execute(
            "UPDATE threads SET senders = ? WHERE thread_id = ?",
            (json.dumps(list(kept.values())), thread_id),
        )

    @pytest.mark.parametrize(
        "first_from",
        ["Old Name <person@example.test>", "person@example.test"],
        ids=["renamed", "bare-then-named"],
    )
    def test_senders_only_matches_a_later_display_name(self, tmp_path, first_from):
        # threads.senders keeps one display string per address, so a
        # name first used on a later reply is only in that message's
        # From participant row.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "later-name.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=[first_from],
        )
        _insert_message(
            conn,
            message_id="m2",
            thread_id="t1",
            sent_at="2024-01-02T00:00:00+00:00",
            from_=["New Name <person@example.test>"],
        )
        self._keep_first_sender_per_address(conn, "t1")
        conn.commit()
        conn.close()
        db = Database(str(path))
        contacts = db.find_contact("new name", senders_only=True)
        assert [(c["email"], c["thread_count"]) for c in contacts] == [("person@example.test", 1)]
        assert "New Name" in contacts[0]["names"]

    def test_senders_only_ignores_a_name_used_only_as_secondary_author(self, tmp_path):
        # person is a primary sender in t1, but "Alias" only names them
        # as the second author of a t2 message: that thread is not one
        # the sender filter matches, so the alias must not resolve.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "secondary-alias.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Person <person@example.test>"],
        )
        _insert_message(
            conn,
            message_id="m2",
            thread_id="t2",
            sent_at="2024-01-02T00:00:00+00:00",
            from_=["Lead <lead@example.test>", "Alias <person@example.test>"],
        )
        conn.close()
        db = Database(str(path))
        assert db.find_contact("alias", senders_only=True) == []
        # The primary sender's own name still resolves, counting t1 only.
        assert [
            (c["email"], c["thread_count"]) for c in db.find_contact("person", senders_only=True)
        ] == [("person@example.test", 1)]

    def test_senders_only_eligibility_is_per_thread(self, tmp_path):
        # Documented limitation: the index records no author order per
        # message, so a name is eligible on any thread the address
        # primarily sent. Once person@ sent m1, an "Alias" written for
        # them as a second author of m2 in the same thread resolves.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "same-thread-alias.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Person <person@example.test>"],
        )
        _insert_message(
            conn,
            message_id="m2",
            thread_id="t1",
            sent_at="2024-01-02T00:00:00+00:00",
            from_=["Lead <lead@example.test>", "Alias <person@example.test>"],
        )
        conn.close()
        db = Database(str(path))
        assert [c["email"] for c in db.find_contact("alias", senders_only=True)] == [
            "person@example.test"
        ]

    def test_senders_only_work_is_scoped_to_candidates(self, tmp_path, monkeypatch):
        # A name that matches nobody must not parse every thread's
        # senders; a name that matches one sender parses only that
        # sender's threads.
        from src.lib import sqlite as sqlite_mod

        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "large.db")
        for n in range(2000):
            _insert_message(
                conn,
                message_id=f"m{n}",
                thread_id=f"t{n}",
                sent_at="2024-01-01T00:00:00+00:00",
                from_=[f"Sender {n} <s{n}@example.test>"],
            )
        for n in range(3):
            _insert_message(
                conn,
                message_id=f"z{n}",
                thread_id=f"tz{n}",
                sent_at="2024-01-01T00:00:00+00:00",
                from_=["Zed Target <zed@example.test>"],
            )
        conn.commit()
        conn.close()
        db = Database(str(path))

        calls = {"parseaddr": 0, "json": 0}
        real_parseaddr, real_loads = sqlite_mod.parseaddr, sqlite_mod.json.loads

        def counting_parseaddr(value):
            calls["parseaddr"] += 1
            return real_parseaddr(value)

        class _CountingJson:
            def __getattr__(self, name):
                return getattr(json, name)

            @staticmethod
            def loads(value):
                calls["json"] += 1
                return real_loads(value)

        monkeypatch.setattr(sqlite_mod, "parseaddr", counting_parseaddr)
        monkeypatch.setattr(sqlite_mod, "json", _CountingJson())

        assert db.find_contact("nobody-by-this-name", senders_only=True) == []
        assert calls == {"parseaddr": 0, "json": 0}

        contacts = db.find_contact("zed target", senders_only=True)
        assert [(c["email"], c["thread_count"]) for c in contacts] == [("zed@example.test", 3)]
        assert calls["json"] == 3
        assert calls["parseaddr"] == 3

    def test_senders_only_reads_one_snapshot(self, tmp_path, monkeypatch):
        # Every query of one lookup runs on one connection inside one
        # read transaction, so an indexer commit between them cannot mix
        # two database states.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "snapshot.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["Pat <pat@example.test>"],
        )
        conn.close()
        db = Database(str(path))
        opened = []
        real_connect = db._connect

        def counting_connect():
            c = real_connect()
            opened.append(c)
            return c

        monkeypatch.setattr(db, "_connect", counting_connect)
        for senders_only in (True, False):
            opened.clear()
            assert db.find_contact("pat", senders_only=senders_only)
            assert len(opened) == 1

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
        full = db.find_contact("alice", senders_only=False)
        sent = db.find_contact("alice", senders_only=True)
        # Default: alice on 3 threads (participant count).
        assert full[0]["thread_count"] == 3
        # senders_only: alice sent 2 of those 3.
        assert sent[0]["thread_count"] == 2


def _open_built_db_conn(tmp_path, name="lib-test.db"):
    """Open a writable sqlite3 connection with the MCP schema built.

    The caller inserts fixture rows, closes this connection, then opens
    a read-only ``Database`` on the returned path. Mirrors the inline
    DB-build boilerplate the older tests in this file repeat.
    """
    import sqlite_vec

    from tests.conftest import _build_schema

    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    return conn, path


class TestParticipantFilter:
    """``participant`` filters threads by anyone in From/To/Cc — distinct
    from ``from_addr``, which is sender-only."""

    @staticmethod
    def _thread(thread_id, participants, senders=None):
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult

        return ThreadResult(
            thread_id=thread_id,
            subject="s",
            participants=participants,
            senders=senders if senders is not None else [],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 2, tzinfo=UTC),
            message_ids=[thread_id],
            snippet="",
            has_attachments=False,
        )

    def test_apply_filters_matches_participant_any_role(self, seeded_db: Database):
        a = self._thread("a", ["alice@example.com", "bob@example.com"])
        b = self._thread("b", ["carol@example.com"])
        filtered = seeded_db._apply_filters([a, b], participant="bob@example.com")
        assert [r.thread_id for r in filtered] == ["a"]

    def test_participant_matches_recipient_not_just_sender(self, seeded_db: Database):
        # bob is a participant but not a sender — from_addr would miss him.
        t = self._thread(
            "a", ["alice@example.com", "bob@example.com"], senders=["alice@example.com"]
        )
        assert seeded_db._apply_filters([t], participant="bob@example.com")
        assert seeded_db._apply_filters([t], from_addr="bob@example.com") == []

    def test_participant_domain_fragment(self, seeded_db: Database):
        t = self._thread("a", ["alice@example.com"])
        assert seeded_db._apply_filters([t], participant="@example.com")
        assert seeded_db._apply_filters([t], participant="@other.org") == []

    def test_has_post_fusion_filter_counts_participant(self):
        assert Database._has_post_fusion_filter(participant="x@example.com") is True
        assert Database._has_post_fusion_filter() is False

    def test_hybrid_search_applies_participant(self, seeded_db: Database):
        # carol is a participant only of t-beta in the seeded fixture.
        results = seeded_db.hybrid_search(
            query_text="invoice lunch meeting",
            query_embedding=[0.0, 1.0, 0.0, 0.0],
            participant="carol@example.com",
        )
        assert results
        assert all("carol@example.com" in r.participants for r in results)

    def test_keyword_search_applies_participant(self, seeded_db: Database):
        results = seeded_db.keyword_search("invoice lunch meeting", participant="dave@example.com")
        assert results
        assert all("dave@example.com" in r.participants for r in results)

    def test_semantic_search_applies_participant(self, seeded_db: Database):
        results = seeded_db.semantic_search([0.0, 0.0, 1.0, 0.0], participant="dave@example.com")
        assert results
        assert all("dave@example.com" in r.participants for r in results)


class TestAddrMatchHelpers:
    def test_addr_matches_full_address_is_canonical(self):
        from src.lib.sqlite import _addr_matches

        assert _addr_matches(["Bob Smith <bob@example.com>"], "bob@example.com")
        assert not _addr_matches(["Bob Smith <bob@example.com>"], "alice@example.com")

    def test_addr_matches_domain_fragment_is_substring(self):
        from src.lib.sqlite import _addr_matches

        assert _addr_matches(["bob@example.com"], "@example.com")

    def test_matches_participant_vs_matches_sender(self):
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_participant, _matches_sender

        r = ThreadResult(
            thread_id="t",
            subject="s",
            participants=["a@example.com", "b@example.com"],
            senders=["a@example.com"],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=["t"],
            snippet="",
            has_attachments=False,
        )
        # Recipient-only address: participant matches, sender does not.
        assert _matches_participant(r, "b@example.com")
        assert not _matches_sender(r, "b@example.com")


class TestLaneProvenance:
    """RRF fusion records which lanes lifted each thread into ranking, as
    pure observability for get_evidence(include_scores=True). These tests
    pin that the provenance is captured — TestReciprocalRankFusion and the
    rerank suite already pin that scoring/ordering are unchanged."""

    def test_hybrid_search_records_lane_ranks(self, chunked_db: Database):
        results = chunked_db.hybrid_search(
            query_text="invoice", query_embedding=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        top = next(r for r in results if r.thread_id == "t-alpha")
        assert top.lane_ranks
        assert all(isinstance(v, int) for v in top.lane_ranks.values())

    def test_keyword_sub_lane_name_recorded(self, chunked_db: Database):
        results = chunked_db.keyword_search("invoice", limit=5)
        top = next(r for r in results if r.thread_id == "t-alpha")
        # t-alpha's subject "invoice for march" matches the thread-FTS lane.
        assert "thread_fts" in top.lane_ranks

    def test_chunk_vec_lane_recorded(self, chunked_db: Database):
        results = chunked_db.hybrid_search(
            query_text="invoice", query_embedding=[1.0, 0.0, 0.0, 0.0], limit=5
        )
        top = next(r for r in results if r.thread_id == "t-alpha")
        # t-alpha carries chunk alpha-c1 aligned to the query embedding.
        assert "chunk_vec" in top.lane_ranks

    def test_thread_vec_lane_recorded(self, chunked_db: Database):
        results = chunked_db.semantic_search([1.0, 0.0, 0.0, 0.0], limit=5)
        top = next(r for r in results if r.thread_id == "t-alpha")
        assert "thread_vec" in top.lane_ranks

    def test_rerank_lane_recorded(self, seeded_db: Database):
        class _StubReranker:
            candidates = 10

            def rerank(self, query, documents, top_n):
                # Identity order, descending score.
                return [(i, float(len(documents) - i)) for i in range(len(documents))]

        results = seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            limit=5,
            reranker=_StubReranker(),
        )
        assert results
        assert results[0].lane_ranks.get("rerank") == 0


class TestMessageDateOnChunks:
    """``ChunkResult.message_date`` is populated from ``message_chunks`` by
    ``get_evidence_chunks_for_threads`` so get_evidence can show when a
    cited passage arrived."""

    def test_evidence_chunks_carry_message_date(self, tmp_path):
        from tests.conftest import _insert_chunk

        conn, path = _open_built_db_conn(tmp_path, "msgdate.db")
        _insert_chunk(
            conn,
            chunk_id="c1",
            message_id="m1",
            thread_id="t1",
            text="hello world",
            embedding=[1.0, 0.0, 0.0, 0.0],
            message_date="2024-05-09T08:00:00+00:00",
        )
        conn.close()
        db = Database(str(path))
        grouped = db.get_evidence_chunks_for_threads(["t1"], [1.0, 0.0, 0.0, 0.0])
        assert grouped["t1"][0].message_date == "2024-05-09T08:00:00+00:00"


class TestSearchAttachments:
    """``search_attachments`` fuses a filename/MIME FTS lane and an
    extracted-text FTS lane, with structured filters and a no-query scan."""

    def test_filename_lane_match(self, attachments_db: Database):
        # "budget" is in the filename annual-budget.xlsx and in no chunk.
        results = attachments_db.search_attachments(query="budget")
        assert [a.attachment_id for a in results] == ["att-budget"]

    def test_extracted_text_lane_match(self, attachments_db: Database):
        # "wage" appears only in the W-2's extracted text, not its filename.
        results = attachments_db.search_attachments(query="wage")
        assert [a.attachment_id for a in results] == ["att-w2"]

    def test_long_document_does_not_crowd_out_other_matches(self, attachments_db: Database):
        """Regression (#220): the text lane applied its LIMIT to chunk
        rows and deduplicated attachments afterwards, so one long
        document with many strong hits filled every slot and another
        matching attachment was never returned, even at the maximum
        limit, with no sign the list was cut."""
        import sqlite_vec

        from tests.conftest import _insert_chunk

        with closing(sqlite3.connect(attachments_db.path)) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            for i in range(60):
                _insert_chunk(
                    conn,
                    chunk_id=f"quote-long-{i}",
                    message_id="t-quote",
                    thread_id="t-quote",
                    text="zebraword " * 20,
                    embedding=[0.0, 0.0, 0.0, 1.0],
                    chunk_index=10 + i,
                    attachment_id="att-quote",
                )
            _insert_chunk(
                conn,
                chunk_id="w2-one-hit",
                message_id="t-tax",
                thread_id="t-tax",
                text="a single zebraword among many other words in this passage",
                embedding=[0.0, 0.0, 0.0, 1.0],
                chunk_index=10,
                attachment_id="att-w2",
            )

        results = attachments_db.search_attachments(query="zebraword", limit=20)

        assert {a.attachment_id for a in results} == {"att-quote", "att-w2"}

    def test_text_lane_filters_before_grouping(self, attachments_db: Database, monkeypatch):
        """Review round 2: filters ran only after every matching chunk in
        the mailbox was grouped; they now narrow the rows being grouped."""
        captured: list[str] = []
        real_fetchall = attachments_db._fetchall

        def spy(sql, params=()):
            captured.append(sql)
            return real_fetchall(sql, params)

        monkeypatch.setattr(attachments_db, "_fetchall", spy)
        results = attachments_db.search_attachments(query="acme", content_type="application/pdf")

        assert {a.attachment_id for a in results} == {"att-quote"}
        text_sql = next(sql for sql in captured if "MATERIALIZED" in sql)
        grouped = text_sql[: text_sql.index("GROUP BY")]
        assert "a.content_type = ?" in grouped

    def test_filename_and_text_match_deduped(self, attachments_db: Database):
        # "acme" hits the filename AND the extracted text of att-quote;
        # the occurrence must be surfaced exactly once.
        results = attachments_db.search_attachments(query="acme")
        quote_hits = [a for a in results if a.attachment_id == "att-quote"]
        assert len(quote_hits) == 1

    def test_content_type_filter(self, attachments_db: Database):
        results = attachments_db.search_attachments(content_type="application/pdf")
        assert {a.attachment_id for a in results} == {"att-quote", "att-w2"}

    def test_from_addr_filter(self, attachments_db: Database):
        results = attachments_db.search_attachments(from_addr="alice@example.com")
        assert {a.attachment_id for a in results} == {"att-quote"}

    def test_date_from_filter(self, attachments_db: Database):
        results = attachments_db.search_attachments(date_from="2024-03-01")
        assert {a.attachment_id for a in results} == {"att-quote"}

    def test_date_to_filter(self, attachments_db: Database):
        results = attachments_db.search_attachments(date_to="2024-01-31")
        assert {a.attachment_id for a in results} == {"att-budget"}

    def test_extracted_only_excludes_failed(self, attachments_db: Database):
        results = attachments_db.search_attachments(extracted_only=True)
        ids = {a.attachment_id for a in results}
        assert "att-budget" not in ids
        assert ids == {"att-quote", "att-w2"}

    def test_no_query_scans_newest_first(self, attachments_db: Database):
        results = attachments_db.search_attachments()
        assert [a.attachment_id for a in results] == ["att-quote", "att-w2", "att-budget"]

    def test_extraction_status_and_snippet_surfaced(self, attachments_db: Database):
        from src.lib.sqlite import AttachmentResult

        results = attachments_db.search_attachments(query="acme")
        a = next(a for a in results if a.attachment_id == "att-quote")
        assert isinstance(a, AttachmentResult)
        assert a.extraction_status == "success"
        assert "Acme Corporation" in a.text_snippet
        assert a.senders == ["alice@example.com"]

    def test_failed_extraction_has_status_and_no_snippet(self, attachments_db: Database):
        results = attachments_db.search_attachments(query="budget")
        a = results[0]
        assert a.extraction_status == "failed"
        assert a.text_snippet == ""

    def test_display_subject_preferred_over_normalized(self, attachments_db: Database):
        # t-quote carries display_subject "Acme Quotation"; t-tax does not.
        quote = attachments_db.search_attachments(query="acme")[0]
        assert quote.subject == "Acme Quotation"
        tax = attachments_db.search_attachments(query="wage")[0]
        assert tax.subject == "payroll documents"

    def test_no_match_returns_empty(self, attachments_db: Database):
        assert attachments_db.search_attachments(query="zzznosuchterm") == []

    def test_punctuation_only_query_returns_empty(self, attachments_db: Database):
        # A query that sanitizes to nothing is an explicit no-match, not
        # a silent unfiltered scan.
        assert attachments_db.search_attachments(query="!!!") == []

    def test_limit_caps_results(self, attachments_db: Database):
        assert len(attachments_db.search_attachments(limit=1)) == 1

    def test_bad_date_filter_raises_value_error(self, attachments_db: Database):
        with pytest.raises(ValueError):
            attachments_db.search_attachments(date_from="not-a-date")

    def test_lanes_degrade_when_attachments_table_missing(self, tmp_path):
        # Every lane JOINs/scans ``attachments``; dropping it exercises the
        # OperationalError branch in all three lanes — the search degrades
        # to an empty result rather than raising.
        conn, path = _open_built_db_conn(tmp_path, "no-attach.db")
        conn.execute("DROP TABLE attachments")
        conn.commit()
        conn.close()
        db = Database(str(path))
        assert db.search_attachments(query="acme") == []
        assert db.search_attachments() == []


class TestFindContactHostileParticipants:
    def test_unparseable_stored_entry_does_not_break_lookup(self, tmp_path):
        """One indexed message whose participant string blows up parseaddr
        (nested-comment recursion) must not take down every find_contact
        call. The writer skips such entries, so the lookup never sees it."""
        from tests.conftest import _insert_thread

        conn, path = _open_built_db_conn(tmp_path, "hostile.db")
        _insert_thread(
            conn,
            thread_id="t-hostile",
            subject="hostile",
            participants=[
                "(" * 1200 + ")" * 1200 + " <mallory@example.com>",
                "Bob <bob@example.com>",
            ],
        )
        conn.close()
        db = Database(str(path))
        contacts = db.find_contact("bob")
        assert [c["email"] for c in contacts] == ["bob@example.com"]


_NESTED_COMMENT_SENDER = "(" * 1200 + "nested" + ")" * 1200 + " mallory@example.com"


class TestAddressFilterHostileSenders:
    """#233: one stored sender string that makes ``parseaddr`` recurse must
    count as a non-match, not abort a search whose filter is valid."""

    def test_canonical_addr_degrades_to_no_address(self):
        assert canonical_addr(_NESTED_COMMENT_SENDER) == ""
        assert address_match_mode(_NESTED_COMMENT_SENDER) == "substring"

    @pytest.fixture
    def hostile_db(self, tmp_path):
        from tests.conftest import _insert_attachment, _insert_thread

        conn, path = _open_built_db_conn(tmp_path, "hostile-senders.db")
        for thread_id, sender in (
            ("t-healthy", "Alice <alice@example.com>"),
            ("t-hostile", _NESTED_COMMENT_SENDER),
        ):
            _insert_thread(
                conn,
                thread_id=thread_id,
                subject="invoice",
                participants=[sender, "buyer@example.com"],
                senders=[sender],
                body_text="synthetic invoice text",
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
            _insert_attachment(
                conn,
                message_id=thread_id,
                thread_id=thread_id,
                attachment_id=f"att-{thread_id}",
                filename="invoice.pdf",
            )
        conn.close()
        db = Database(str(path))
        yield db

    def test_keyword_search_sender_filter(self, hostile_db):
        results = hostile_db.keyword_search("invoice", from_addr="alice@example.com")
        assert [r.thread_id for r in results] == ["t-healthy"]

    def test_hybrid_search_sender_filter(self, hostile_db):
        results = hostile_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            from_addr="alice@example.com",
        )
        assert [r.thread_id for r in results] == ["t-healthy"]

    def test_hybrid_search_participant_filter(self, hostile_db):
        results = hostile_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            participant="alice@example.com",
        )
        assert [r.thread_id for r in results] == ["t-healthy"]

    def test_search_attachments_sender_filter(self, hostile_db):
        results = hostile_db.search_attachments(query="invoice", from_addr="alice@example.com")
        assert [r.thread_id for r in results] == ["t-healthy"]


def _ids(page) -> list[str]:
    return [m.message_id for m in page.messages]


class TestQueryMessages:
    """``query_messages`` enumerates: every message matching all given
    predicates, newest first, with an exact total and a cursor."""

    def test_no_predicates_enumerates_everything_newest_first(self, messages_db):
        page = messages_db.query_messages()
        # m4/m5 tie on sent_at; message_id DESC breaks it.
        assert _ids(page) == ["m5", "m4", "m3", "m2", "m1"]
        assert page.total_matches == 5
        assert page.has_more is False
        assert page.next_cursor is None
        assert page.offset == 0

    @pytest.mark.parametrize(
        "value",
        ["jane@example.com", "JANE@Example.COM", "Jane Doe <jane@example.com>"],
    )
    def test_sender_full_address_is_exact(self, messages_db, value):
        page = messages_db.query_messages(sender=value)
        assert _ids(page) == ["m5", "m3", "m1"]
        assert page.total_matches == 3

    def test_sender_domain_is_substring(self, messages_db):
        assert _ids(messages_db.query_messages(sender="@other.org")) == ["m4"]

    def test_sender_name_fragment_matches_display_name(self, messages_db):
        # m3 and m5 are from jane@example.com with no display name, so
        # the name fragment only reaches m1.
        assert _ids(messages_db.query_messages(sender="doe")) == ["m1"]

    def test_sender_name_match_folds_non_ascii_case(self, messages_db):
        assert _ids(messages_db.query_messages(sender="ÁLVAREZ")) == ["m4"]

    def test_recipient_covers_to_and_cc(self, messages_db):
        assert _ids(messages_db.query_messages(recipient="jane@example.com")) == ["m4", "m2"]
        assert _ids(messages_db.query_messages(recipient="carol@other.org")) == ["m3", "m2"]

    def test_participant_covers_every_role(self, messages_db):
        page = messages_db.query_messages(participant="jane@example.com")
        assert page.total_matches == 5

    def test_subject_is_case_insensitive_substring(self, messages_db):
        assert _ids(messages_db.query_messages(subject="BUDGET")) == ["m2", "m1"]

    def test_text_matches_body_not_attachment(self, messages_db):
        assert _ids(messages_db.query_messages(text="budget")) == ["m2", "m1"]
        assert messages_db.query_messages(text="spreadsheet").total_matches == 0

    def test_text_requires_every_term(self, messages_db):
        assert _ids(messages_db.query_messages(text="budget approved")) == ["m1"]

    def test_text_terms_may_fall_in_different_chunks(self, messages_db):
        assert _ids(messages_db.query_messages(text="lunch noodle")) == ["m3"]

    def test_text_without_words_is_rejected(self, messages_db):
        with pytest.raises(ValueError, match="text"):
            messages_db.query_messages(text="!!! ???")

    def test_text_underscore_separates_words_as_fts_does(self, tmp_path):
        # FTS treats "_" as a separator, so "budget_approved" is the two
        # words budget and approved, each required, in any order.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "underscore.db")
        for n, body in enumerate(["budget is approved", "approved budget", "budget only"], 1):
            _insert_message(
                conn,
                message_id=f"m{n}",
                thread_id=f"t{n}",
                sent_at=f"2024-01-0{n}T00:00:00+00:00",
                body=body,
            )
        conn.close()
        db = Database(str(path))
        assert _ids(db.query_messages(text="budget_approved")) == ["m2", "m1"]

    def test_text_private_use_character_stays_inside_the_word(self, tmp_path):
        # FTS treats private-use characters (category Co) as word
        # characters: "alphabeta" is one word, not alpha + beta.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "private-use.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            body="alphabeta",
        )
        _insert_message(
            conn,
            message_id="m2",
            thread_id="t2",
            sent_at="2024-01-02T00:00:00+00:00",
            body="alpha beta",
        )
        conn.close()
        db = Database(str(path))
        assert _ids(db.query_messages(text="alphabeta")) == ["m1"]

    def test_folder_is_exact(self, messages_db):
        assert _ids(messages_db.query_messages(folder="Archive")) == ["m3"]

    def test_date_bounds_are_inclusive_whole_days(self, messages_db):
        assert _ids(messages_db.query_messages(date_from="2024-02-01")) == ["m5", "m4", "m3"]
        # m2 at 10:00 on the 11th is inside a date-only upper bound.
        assert _ids(messages_db.query_messages(date_to="2024-01-11")) == ["m2", "m1"]

    def test_invalid_date_is_rejected(self, messages_db):
        with pytest.raises(ValueError, match="date_from"):
            messages_db.query_messages(date_from="last tuesday")

    def test_has_attachments_both_ways(self, messages_db):
        assert _ids(messages_db.query_messages(has_attachments=True)) == ["m4", "m2"]
        assert _ids(messages_db.query_messages(has_attachments=False)) == ["m5", "m3", "m1"]

    def test_predicates_combine_with_and(self, messages_db):
        page = messages_db.query_messages(
            sender="jane@example.com", folder="INBOX", date_from="2024-03-01"
        )
        assert _ids(page) == ["m5"]

    def test_blank_predicates_are_ignored(self, messages_db):
        page = messages_db.query_messages(sender="", subject="  ", text="")
        assert page.total_matches == 5

    def test_returns_headers_and_participants_by_role(self, messages_db):
        (m2,) = messages_db.query_messages(subject="Re: Budget").messages
        assert m2.thread_id == "t1"
        assert m2.subject == "Re: Budget review"
        assert m2.sent_at == "2024-01-11T10:00:00+00:00"
        assert m2.folder == "INBOX"
        assert m2.has_attachments is True
        assert [(p.name, p.address) for p in m2.from_] == [(None, "bob@example.com")]
        assert [(p.name, p.address) for p in m2.to] == [("Jane Doe", "jane@example.com")]
        assert [(p.name, p.address) for p in m2.cc] == [(None, "carol@other.org")]


class TestQueryMessagesUnicodeText:
    """Composed and decomposed spellings of a word are the same word to
    FTS (unicode61 folds diacritics); the ``text`` terms must agree, or
    an exhaustive count silently misses messages."""

    @pytest.mark.parametrize(
        ("body", "query"),
        [
            ("r\u00e9sum\u00e9 attached", "re\u0301sume\u0301"),
            ("re\u0301sume\u0301 attached", "r\u00e9sum\u00e9"),
            ("re\u0301sume\u0301 attached", "re\u0301sume\u0301"),
            ("a nai\u0308ve plan", "nai\u0308ve"),
            # Scripts unicode61 does not fold: the query must reach FTS in
            # the indexed form, not NFC-composed.
            (
                "\u03ba\u03bf\u0301\u03c3\u03bc\u03bf\u03c2",
                "\u03ba\u03bf\u0301\u03c3\u03bc\u03bf\u03c2",
            ),
            ("\u1112\u1161\u11ab\u1100\u1173\u11af", "\u1112\u1161\u11ab\u1100\u1173\u11af"),
        ],
    )
    def test_composed_and_decomposed_forms_match(self, tmp_path, body, query):
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "unicode-text.db")
        _insert_message(
            conn, message_id="m1", thread_id="t1", sent_at="2024-01-01T00:00:00+00:00", body=body
        )
        conn.close()
        db = Database(str(path))
        assert db.query_messages(text=query).total_matches == 1

    def test_bare_combining_mark_is_not_a_term(self, messages_db):
        with pytest.raises(ValueError, match="text"):
            messages_db.query_messages(text="\u0301")


# (stored header text, query) pairs that are equal under Unicode caseless
# matching. ``lower()`` misses the expansions (\u00df -> ss, the \ufb01 ligature).
_CASELESS_PAIRS = [
    ("Jane", "JANE"),
    ("Jos\u00e9", "JOS\u00c9"),
    ("Stra\u00dfe", "STRASSE"),
    ("STRASSE", "stra\u00dfe"),
    ("\u038c\u03c3\u03bf\u03c2", "\u038c\u03a3\u039f\u03a3"),
    ("\ufb01le", "FILE"),
]


class TestUnicodeCaselessMatching:
    """Every case-insensitive name and subject match folds both sides the
    same way, with ``casefold``. One fixture per pair, checked through
    each matching site, so a site left on ``lower`` shows up here."""

    @pytest.fixture(params=_CASELESS_PAIRS, ids=lambda p: ascii(p[1]))
    def case(self, request, tmp_path):
        from tests.conftest import _insert_message

        stored, query = request.param
        conn, path = _open_built_db_conn(tmp_path, "caseless.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            subject=f"re {stored} notes",
            from_=[f"{stored} Person <person@example.test>"],
            to=[f"{stored} Recipient <recipient@example.test>"],
        )
        conn.close()
        return Database(str(path)), stored, query

    def test_find_contact(self, case):
        db, _, query = case
        assert [c["email"] for c in db.find_contact(query)] == [
            "person@example.test",
            "recipient@example.test",
        ]

    def test_find_contact_senders_only(self, case):
        db, _, query = case
        emails = [c["email"] for c in db.find_contact(query, senders_only=True)]
        assert emails == ["person@example.test"]

    def test_query_messages_subject(self, case):
        db, _, query = case
        assert db.query_messages(subject=query).total_matches == 1

    @pytest.mark.parametrize("field", ["sender", "recipient", "participant"])
    def test_query_messages_name_fragment(self, case, field):
        db, _, query = case
        assert db.query_messages(**{field: query}).total_matches == 1

    def test_thread_filter_name_fragment(self, case):
        from src.lib.sqlite import _addr_matches

        _, stored, query = case
        # Callers lowercase the filter value before matching.
        assert _addr_matches([f"{stored} Person <person@example.test>"], query.lower())

    def test_address_needle_keeps_matching_stored_addresses(self, tmp_path):
        # Addresses are stored lowercased; a fragment of one must still
        # match after the name side moved to casefold.
        from tests.conftest import _insert_message

        conn, path = _open_built_db_conn(tmp_path, "addr.db")
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["stra\u00dfe@example.test"],
        )
        conn.close()
        db = Database(str(path))
        assert [c["email"] for c in db.find_contact("STRA\u00dfE@")] == ["stra\u00dfe@example.test"]
        assert db.query_messages(sender="STRA\u00dfE@").total_matches == 1


class TestQueryMessagesPaging:
    def test_cursor_walks_every_match_exactly_once(self, messages_db):
        seen: list[str] = []
        cursor = None
        offsets = []
        while True:
            page = messages_db.query_messages(limit=2, cursor=cursor)
            assert page.total_matches == 5
            offsets.append(page.offset)
            seen += _ids(page)
            if not page.has_more:
                assert page.next_cursor is None
                break
            cursor = page.next_cursor
        assert seen == ["m5", "m4", "m3", "m2", "m1"]
        assert offsets == [0, 2, 4]

    def test_page_boundary_inside_a_sent_at_tie(self, messages_db):
        first = messages_db.query_messages(limit=1)
        assert _ids(first) == ["m5"]
        second = messages_db.query_messages(limit=1, cursor=first.next_cursor)
        assert _ids(second) == ["m4"]

    def test_exact_final_page_has_no_more(self, messages_db):
        page = messages_db.query_messages(sender="jane@example.com", limit=3)
        assert page.has_more is False
        assert page.next_cursor is None

    def test_cursor_is_bound_to_its_filters(self, messages_db):
        first = messages_db.query_messages(sender="jane@example.com", limit=1)
        with pytest.raises(ValueError, match="cursor"):
            messages_db.query_messages(sender="bob@example.com", cursor=first.next_cursor)

    @pytest.mark.parametrize("cursor", ["not-a-cursor", "e30", "eyJ2IjogOX0"])
    def test_malformed_cursor_is_rejected(self, messages_db, cursor):
        with pytest.raises(ValueError, match="cursor"):
            messages_db.query_messages(cursor=cursor)


class TestFindContactSendersOnlyHostileEntries:
    def test_bad_sender_entries_cost_only_themselves(self, tmp_path):
        """senders_only parses each thread's ``senders`` JSON; a corrupt
        array, a non-string entry, or a string that makes parseaddr
        recurse must be skipped, not abort the lookup."""
        from tests.conftest import _insert_thread

        conn, path = _open_built_db_conn(tmp_path, "hostile-senders.db")
        _insert_thread(
            conn,
            thread_id="t-hostile",
            subject="hostile",
            participants=["Bob <bob@example.com>"],
            senders=["(" * 1200 + ")" * 1200 + " <mallory@example.com>", "Bob <bob@example.com>"],
        )
        _insert_thread(conn, thread_id="t-corrupt", subject="c", participants=[])
        conn.execute("UPDATE threads SET senders = '{not json' WHERE thread_id = 't-corrupt'")
        _insert_thread(conn, thread_id="t-nonstr", subject="n", participants=[])
        conn.execute("UPDATE threads SET senders = '[42]' WHERE thread_id = 't-nonstr'")
        conn.commit()
        conn.close()
        db = Database(str(path))
        contacts = db.find_contact("bob", senders_only=True)
        assert [c["email"] for c in contacts] == ["bob@example.com"]


class TestThreadPage:
    """``get_thread_page`` reads one page of a thread's messages, oldest
    first, with each message's own headers and its body rebuilt from its
    body chunks — all from one read snapshot."""

    def test_messages_are_chronological(self, messages_db):
        page = messages_db.get_thread_page("t1", offset=0, limit=10, body_char_limit=4000)
        assert [m.message_id for m in page.messages] == ["m1", "m2"]
        assert page.total_messages == 2

    def test_sent_at_ties_break_by_message_id(self, messages_db):
        page = messages_db.get_thread_page("t3", offset=0, limit=10, body_char_limit=4000)
        assert [m.message_id for m in page.messages] == ["m4", "m5"]

    def test_records_carry_their_own_headers(self, messages_db):
        m2 = messages_db.get_thread_page("t1", offset=0, limit=10, body_char_limit=4000).messages[1]
        assert m2.subject == "Re: Budget review"
        assert m2.sent_at == "2024-01-11T10:00:00+00:00"
        assert m2.has_attachments is True
        assert m2.in_reply_to == "m1"
        assert m2.references == ["m1"]
        assert [(p.name, p.address) for p in m2.from_] == [(None, "bob@example.com")]
        assert [(p.name, p.address) for p in m2.to] == [("Jane Doe", "jane@example.com")]
        assert [p.address for p in m2.cc] == ["carol@other.org"]

    def test_pages_by_offset_and_limit(self, messages_db):
        first = messages_db.get_thread_page("t1", offset=0, limit=1, body_char_limit=4000)
        second = messages_db.get_thread_page("t1", offset=1, limit=1, body_char_limit=4000)
        assert [m.message_id for m in first.messages] == ["m1"]
        assert [m.message_id for m in second.messages] == ["m2"]
        assert first.total_messages == second.total_messages == 2
        # Bodies are read only for the page's messages.
        assert set(first.bodies) == {"m1"}

    def test_bodies_exclude_attachment_text(self, messages_db):
        page = messages_db.get_thread_page("t1", offset=0, limit=10, body_char_limit=4000)
        assert page.bodies["m2"].text == "thanks, budget noted"

    def test_overlapping_chunks_rebuild_the_body_exactly(self, overlap_db):
        from tests.conftest import OVERLAP_BODY

        body = overlap_db.get_thread_page("t-ov", offset=0, limit=10, body_char_limit=4000).bodies[
            "ov1"
        ]
        # Each overlapped paragraph once; the paragraph that really
        # repeats at two offsets stays twice.
        assert body.text == OVERLAP_BODY
        assert body.text.count("Same line again.") == 2
        assert body.omitted_chars == 0

    def test_body_is_cut_at_the_limit(self, overlap_db):
        from tests.conftest import OVERLAP_BODY

        body = overlap_db.get_thread_page("t-ov", offset=0, limit=10, body_char_limit=300).bodies[
            "ov1"
        ]
        assert body.text == OVERLAP_BODY[:300]
        assert body.omitted_chars == len(OVERLAP_BODY) - 300

    def test_separate_chunks_join_at_a_paragraph_break(self, messages_db):
        page = messages_db.get_thread_page("t2", offset=0, limit=10, body_char_limit=4000)
        assert page.bodies["m3"].text == "lunch friday?\n\nat the noodle place"

    def test_has_bodies_reflects_the_whole_thread(self, messages_db, seeded_db):
        page = messages_db.get_thread_page("t1", offset=5, limit=10, body_char_limit=4000)
        assert page.messages == []
        assert page.has_bodies is True
        page = seeded_db.get_thread_page("t-alpha", offset=0, limit=10, body_char_limit=4000)
        assert page.has_bodies is False

    def test_unknown_thread_is_none(self, messages_db):
        assert messages_db.get_thread_page("nope", offset=0, limit=10, body_char_limit=4000) is None

    def test_reads_one_snapshot(self, tmp_path):
        # A reply committed after the thread row is read must not appear
        # in the page, or the page mixes two database states.
        db, writer = _snapshot_db(tmp_path)
        _commit_between_reads(
            db,
            writer,
            """
            INSERT INTO message_thread_map VALUES ('b', 't', '/maildir/INBOX/cur/b');
            INSERT INTO messages VALUES ('b', 't', '/maildir/INBOX/cur/b', 'INBOX', 's',
                '2024-01-02T00:00:00+00:00', 'a', '["a"]', 1, 1, 'h', 'x');
            UPDATE threads SET date_last = '2024-01-02T00:00:00+00:00' WHERE thread_id = 't';
            """,
        )
        try:
            page = db.get_thread_page("t", offset=0, limit=10, body_char_limit=4000)
            assert page is not None
            assert [m.message_id for m in page.messages] == ["a"]
            assert page.total_messages == 1
        finally:
            writer.close()


class TestMessageView:
    """``get_message_view`` reads one message's headers, its thread, and
    its full body from one read snapshot."""

    def test_headers(self, messages_db):
        view = messages_db.get_message_view("m2")
        assert view is not None
        assert view.record.in_reply_to == "m1"
        assert [p.address for p in view.record.cc] == ["carol@other.org"]
        assert view.thread.thread_id == "t1"

    def test_message_without_reply_headers(self, messages_db):
        view = messages_db.get_message_view("m1")
        assert view is not None
        assert view.record.in_reply_to is None
        assert view.record.references == []

    def test_full_body_without_overlap(self, overlap_db):
        from tests.conftest import OVERLAP_BODY

        view = overlap_db.get_message_view("ov1")
        assert view is not None and view.body is not None
        assert view.body.text == OVERLAP_BODY

    def test_body_excludes_attachment_text(self, messages_db):
        view = messages_db.get_message_view("m2")
        assert view is not None and view.body is not None
        assert view.body.text == "thanks, budget noted"

    def test_no_chunks_means_no_body(self, seeded_db):
        view = seeded_db.get_message_view("t-alpha")
        assert view is not None
        assert view.body is None

    def test_unknown_message_is_none(self, messages_db):
        assert messages_db.get_message_view("nope") is None

    def test_reads_one_snapshot(self, tmp_path):
        db, writer = _snapshot_db(tmp_path)
        _commit_between_reads(
            db, writer, "UPDATE threads SET display_subject = 'changed' WHERE thread_id = 't';"
        )
        try:
            view = db.get_message_view("a")
            assert view is not None
            assert view.thread.subject == "s"
        finally:
            writer.close()


def _snapshot_db(tmp_path):
    """A WAL database holding thread "t" with message "a", plus an open
    writer connection (which keeps the WAL files in place for the
    read-only reader)."""
    from tests.conftest import _build_schema, _insert_message

    path = tmp_path / "snapshot.db"
    writer = sqlite3.connect(str(path))
    writer.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(writer)
    writer.enable_load_extension(False)
    writer.execute("PRAGMA journal_mode=WAL")
    _build_schema(writer)
    _insert_message(
        writer, message_id="a", thread_id="t", subject="s", sent_at="2024-01-01T00:00:00+00:00"
    )
    return Database(str(path)), writer


def _commit_between_reads(db, writer, sql: str) -> None:
    """Have ``writer`` commit ``sql`` just before ``db`` runs its second
    SELECT, the way an indexer commit can land between two reads."""
    connect = db._connect
    selects = []

    def traced_connect():
        conn = connect()

        def trace(statement: str) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)
                if len(selects) == 2:
                    writer.executescript(sql)

        conn.set_trace_callback(trace)
        return conn

    db._connect = traced_connect


class TestVectorLaneKLimit:
    """#223: sqlite-vec rejects k > 4096. A large RERANK_CANDIDATES times
    the oversampling factors exceeded it, the error was caught, and the
    lane silently returned nothing — raising the candidate window lost
    the precision lane."""

    def test_chunk_lane_survives_a_window_past_the_k_limit(self, chunked_db: Database):
        assert chunked_db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], limit=8000)

    def test_thread_lane_survives_a_window_past_the_k_limit(self, chunked_db: Database):
        assert chunked_db._vector_search([1.0, 0.0, 0.0, 0.0], limit=8000)

    def test_filtered_rerank_window_keeps_the_chunk_lane(self, chunked_db: Database, monkeypatch):
        """The issue's case: 200 candidates with a folder filter asked for
        k = 200 x 4 x 10 = 8000."""

        class _PassThroughReranker:
            candidates = 200

            def rerank(self, query, documents, top_n):
                return [(i, 1.0 - i / 1000) for i in range(len(documents))][:top_n]

        calls: list[int] = []
        real = chunked_db._chunk_vector_search

        def spy(embedding, limit):
            results = real(embedding, limit)
            calls.append(len(results))
            return results

        monkeypatch.setattr(chunked_db, "_chunk_vector_search", spy)
        chunked_db.hybrid_search(
            query_text="zzzz-no-keyword-hit",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            folders=["INBOX"],
            limit=5,
            reranker=_PassThroughReranker(),
        )
        assert calls and calls[0] > 0


# A synthetic stand-in for query text or mail content. It is a bare word
# so FTS5 accepts it as a column name and quotes it back in its error.
_ERROR_MARKER = "privatemarkerq7z"
_VEC = [0.0, 0.0, 0.0, 0.0]


class TestFallbackErrorTextWithheld:
    """Every read-path fallback logs the exception type, never its text:
    an SQLite error can quote the query (FTS5 reports ``<term>:foo`` as
    ``no such column: <term>``), and the query is withheld from the
    tool-call log (#257)."""

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            pytest.param(
                lambda db: db._attachment_filename_lane("x", [], [], 5), [], id="att-name"
            ),
            pytest.param(lambda db: db._attachment_text_lane("x", [], [], 5), [], id="att-text"),
            pytest.param(lambda db: db._attachment_scan([], [], 5), [], id="att-scan"),
            pytest.param(lambda db: db._thread_keyword_search("x", 5), [], id="thread-fts"),
            pytest.param(lambda db: db._chunk_keyword_search("x", 5), [], id="chunk-fts"),
            pytest.param(lambda db: db._attachment_keyword_search("x", 5), [], id="att-fts"),
            pytest.param(lambda db: db._matched_attachments("x", ["t-alpha"]), {}, id="att-match"),
            pytest.param(lambda db: db._like_fallback("x", 5), [], id="like"),
            pytest.param(lambda db: db._chunk_vector_search(_VEC, 5), None, id="chunk-vec"),
            pytest.param(
                lambda db: db.get_evidence_chunks_for_threads(["t-alpha"], _VEC),
                {"t-alpha": []},
                id="evidence",
            ),
            pytest.param(
                lambda db: db.get_recent_chunks_for_thread("t-alpha"), [], id="recent-chunks"
            ),
            pytest.param(lambda db: db._vector_search(_VEC, 5), None, id="thread-vec"),
        ],
    )
    def test_fallback_logs_type_not_text(self, seeded_db, monkeypatch, caplog, call, expected):
        calls = []

        def boom(*_args, **_kwargs):
            calls.append(1)
            raise sqlite3.OperationalError(f"no such column: {_ERROR_MARKER}")

        monkeypatch.setattr(seeded_db, "_fetchall", boom)
        with caplog.at_level("DEBUG"):
            assert call(seeded_db) == expected
        assert calls, "the patched query must have run"
        assert _ERROR_MARKER not in caplog.text
        assert "OperationalError" in caplog.text

    def test_recent_chunks_failure_omits_thread_id(self, seeded_db, monkeypatch, caplog):
        """A thread id is a Message-ID, which the tool-call log withholds."""

        def boom(*_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(seeded_db, "_fetchall", boom)
        with caplog.at_level("DEBUG"):
            seeded_db.get_recent_chunks_for_thread(f"<{_ERROR_MARKER}@example.com>")
        assert _ERROR_MARKER not in caplog.text

    def test_real_fts5_error_quotes_term_but_log_does_not(self, seeded_db, monkeypatch, caplog):
        """An unsanitized ``<term>:foo`` makes FTS5 quote ``<term>``; the
        keyword lane still falls back to LIKE and logs only the type."""
        import src.lib.sqlite as sqlite_mod

        query = f"{_ERROR_MARKER}:foo"
        with pytest.raises(sqlite3.OperationalError, match=_ERROR_MARKER):
            seeded_db._fetchall("SELECT rowid FROM threads_fts WHERE threads_fts MATCH ?", [query])

        monkeypatch.setattr(sqlite_mod, "_sanitize_fts_query", lambda q: q)
        sentinel = [object()]
        seen = []

        def like(q, *_args, **_kwargs):
            seen.append(q)
            return sentinel

        monkeypatch.setattr(seeded_db, "_like_fallback", like)
        with caplog.at_level("DEBUG"):
            result = seeded_db._thread_keyword_search(query, 5)
        assert result is sentinel
        assert seen == [query]
        assert _ERROR_MARKER not in caplog.text
        assert "OperationalError" in caplog.text

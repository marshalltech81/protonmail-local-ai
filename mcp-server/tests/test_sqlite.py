"""Tests for src.lib.sqlite Database query layer.

Covers pure fusion/filter logic against synthetic ThreadResult lists and
real read queries against an in-memory-style database seeded via conftest.
"""

import sqlite3

import pytest
from src.lib.sqlite import Database


class TestReadOnlyConnection:
    def test_write_attempt_raises(self, seeded_db: Database):
        """The MCP reader opens SQLite via ``?mode=ro`` URI — any attempt
        to mutate the shared index must fail at the SQLite API level, not
        rely only on ``PRAGMA query_only`` being honored."""
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            seeded_db._conn.execute(
                "UPDATE threads SET subject = 'hijacked' WHERE thread_id = 't-alpha'"
            )

    def test_reads_still_work(self, seeded_db: Database):
        row = seeded_db._conn.execute(
            "SELECT subject FROM threads WHERE thread_id = ?", ("t-alpha",)
        ).fetchone()
        assert row["subject"] == "invoice for march"


class TestPing:
    def test_ping_succeeds_on_healthy_db(self, seeded_db: Database):
        # Returns None on success; no exception is the signal.
        assert seeded_db.ping() is None

    def test_ping_raises_when_connection_closed(self, seeded_db: Database):
        seeded_db._conn.close()
        with pytest.raises(sqlite3.ProgrammingError):
            seeded_db.ping()


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

    def test_from_addr_falls_back_to_participants_for_legacy_rows(self):
        """Pre-v6 rows have an empty senders list. The filter must still
        match them via participants so existing indexes do not lose
        recall on upgrade day."""
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult, _matches_sender

        legacy = ThreadResult(
            thread_id="legacy",
            subject="s",
            participants=["alice@example.com", "bob@example.com"],
            senders=[],  # empty senders -> legacy row
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
        )
        assert _matches_sender(legacy, "alice")
        assert _matches_sender(legacy, "bob")

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

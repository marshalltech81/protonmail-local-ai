"""``search_attachments(sender=...)``: the ``indeterminate`` count (#1204).

The ``sender`` leaf is unknown for a carrying message whose
``sender_ambiguous`` is not 0 (#1153), so its attachments are neither
matched nor missed. The tool counts them: every candidate the lanes
reach, over every lane identity and with no limit, whose carrying
message the leaf leaves undecided, with every other filter applied.
The lanes and the count read one snapshot. All data is synthetic.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest
from src.lib.sqlite import _FILTERED_OVERSAMPLE, Database

from tests import test_attachment_sender as sender_tests
from tests.conftest import _insert_attachment, _insert_extraction, _insert_message, claimant_of
from tests.test_attachment_sender import (
    _SHAPES,
    COLLEAGUE,
    MARKER,
    VENDOR,
    _add,
    _handler,
    _ids,
    _real_server,
)
from tests.test_sqlite import _open_built_db_conn

# The sender tests' fixtures, shared so both files read the same data.
carrier_db = sender_tests.carrier_db
shapes_db = sender_tests.shapes_db


def _counted(db: Database, **kw):
    return db.search_attachments_with_count(**kw)


def _child_count_rss(path, call_args: str) -> tuple[int, int]:
    """``indeterminate`` and the peak-RSS growth (MB) of one
    ``search_attachments_with_count(<call_args>, limit=1)`` on ``path``,
    run in a child process: SQLite's allocations are not visible to
    ``tracemalloc``. ``call_args`` is a fixed literal from the test."""
    import subprocess
    import sys
    from pathlib import Path

    child = (
        "import resource, sys\n"
        "from src.lib.sqlite import Database\n"
        "db = Database(sys.argv[1])\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        f"found = db.search_attachments_with_count({call_args}, limit=1)\n"
        "grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before\n"
        "scale = 1 if sys.platform == 'darwin' else 1024\n"
        "print(found.indeterminate, grown * scale // 2**20)\n"
    )
    out = subprocess.run(  # noqa: S603 (fixed interpreter and literal code)
        [sys.executable, "-c", child, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    indeterminate, grown_mb = (int(v) for v in out.stdout.split())
    return indeterminate, grown_mb


@pytest.fixture
def undecided_db(tmp_path) -> Database:
    """Thread ``t``: the vendor's decided ``v1``, the vendor's undecided
    ``u1`` (``sender_ambiguous`` NULL) and ``u2`` (1), and a colleague's
    decided ``c1``. Filenames hold ``ledger``, texts ``bravo``."""
    conn, path = _open_built_db_conn(tmp_path, "undecided.db")
    _add(conn, "v1", "t", "2024-01-10T00:00:00+00:00", VENDOR)
    _add(conn, "u1", "t", "2024-01-11T00:00:00+00:00", VENDOR, sender_ambiguous=None)
    _add(conn, "u2", "t", "2024-01-12T00:00:00+00:00", VENDOR, sender_ambiguous=1)
    _add(conn, "c1", "t", "2024-01-13T00:00:00+00:00", COLLEAGUE)
    conn.close()
    return Database(str(path))


class TestAttachmentIndeterminateCount:
    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_counts_attachments_the_leaf_left_undecided(self, undecided_db, query):
        found = _counted(undecided_db, query=query, sender="vendor@example.com")
        assert _ids(found.results) == {"v1-att"}
        assert found.indeterminate == 2

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_no_count_without_a_sender(self, undecided_db, blank):
        found = _counted(undecided_db, sender=blank)
        assert len(found.results) == 4
        assert found.indeterminate is None

    def test_list_api_is_unchanged(self, undecided_db):
        assert _ids(undecided_db.search_attachments(sender="vendor@example.com")) == {"v1-att"}

    def test_a_query_that_sanitizes_to_nothing_has_no_candidates(self, undecided_db):
        found = _counted(undecided_db, query="!!!", sender="vendor@example.com")
        assert found.results == []
        assert found.indeterminate == 0

    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_count_is_independent_of_the_limit(self, tmp_path, query):
        conn, path = _open_built_db_conn(tmp_path, "many.db")
        many = _FILTERED_OVERSAMPLE * 3
        for i in range(many):
            _add(
                conn,
                f"u{i}",
                "t",
                f"2024-02-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                VENDOR,
                sender_ambiguous=None,
            )
        conn.close()
        found = _counted(Database(str(path)), query=query, sender="vendor", limit=1)
        assert found.results == []
        assert found.indeterminate == many

    def test_an_attachment_both_lanes_match_is_counted_once(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "overlap.db")
        _add(
            conn, "u1", "t", "2024-01-10T00:00:00+00:00", VENDOR, text="ledger", sender_ambiguous=1
        )
        conn.close()
        db = Database(str(path))
        # Both lanes reach it on their own.
        assert db._attachment_filename_lane('"ledger"', [], [], 10)
        assert db._attachment_text_lane('"ledger"', None, [], [], 10)
        assert _counted(db, query="ledger", sender="vendor").indeterminate == 1

    def test_text_lane_counts_its_anchored_occurrence(self, tmp_path):
        """One payload carried twice by one undecided message: the text
        lane reports one occurrence (the lowest that passes
        ``content_type`` with extracted text), so a text match is counted
        once, and under ``content_type`` only when an occurrence passes.
        The filename lane reports occurrences, so each copy is its own."""
        conn, path = _open_built_db_conn(tmp_path, "anchor.db")
        _insert_message(
            conn,
            message_id="u1",
            thread_id="t",
            sent_at="2024-01-10T00:00:00+00:00",
            subject="synthetic",
            from_=[VENDOR],
            has_attachments=True,
            attachment_text="bravo",
            sender_ambiguous=None,
        )
        for occurrence, filename, content_type in [
            ("o1", "copy-a.pdf", "application/pdf"),
            ("o2", "copy-b.dat", "application/octet-stream"),
        ]:
            _insert_attachment(
                conn,
                message_id="u1",
                thread_id="t",
                attachment_id="u1-att",
                filename=filename,
                content_type=content_type,
                occurrence_id=occurrence,
            )
        _insert_extraction(conn, attachment_id="u1-att", extracted_text="bravo")
        conn.close()
        db = Database(str(path))
        assert len(db.search_attachments(query="bravo")) == 1
        assert _counted(db, query="bravo", sender="vendor").indeterminate == 1
        octet = "application/octet-stream"
        assert _counted(db, query="bravo", sender="vendor", content_type=octet).indeterminate == 1
        csv = "text/csv"
        assert _counted(db, query="bravo", sender="vendor", content_type=csv).indeterminate == 0
        assert len(db.search_attachments(query="copy")) == 2
        assert _counted(db, query="copy", sender="vendor").indeterminate == 2

    @pytest.mark.parametrize(("query", "listed"), [(None, 2), ("ledger", 2), ("bravo", 1)])
    def test_counts_as_the_results_list_repeated_occurrences(self, tmp_path, query, listed):
        """One payload carried twice under one filename (Codex round 1):
        the filename lane and the scan list each occurrence, the text lane
        one. Decided, the results list ``listed`` rows; undecided, the
        count is the same number. With both lanes matching, the text row
        is the filename rows' identity and adds nothing."""
        path = tmp_path / "repeat.db"

        def build(ambiguous: int | None) -> Database:
            if path.exists():
                path.unlink()
            conn, _ = _open_built_db_conn(tmp_path, "repeat.db")
            _insert_message(
                conn,
                message_id="u1",
                thread_id="t",
                sent_at="2024-01-10T00:00:00+00:00",
                subject="synthetic",
                from_=[VENDOR],
                has_attachments=True,
                attachment_text="bravo ledger",
                sender_ambiguous=ambiguous,
            )
            for occurrence in ("o1", "o2"):
                _insert_attachment(
                    conn,
                    message_id="u1",
                    thread_id="t",
                    attachment_id="u1-att",
                    filename="ledger.pdf",
                    occurrence_id=occurrence,
                )
            _insert_extraction(conn, attachment_id="u1-att", extracted_text="bravo ledger")
            conn.close()
            return Database(str(path))

        decided = _counted(build(0), query=query, sender="vendor")
        assert (len(decided.results), decided.indeterminate) == (listed, 0)
        undecided = _counted(build(None), query=query, sender="vendor")
        assert (undecided.results, undecided.indeterminate) == ([], listed)

    @pytest.mark.parametrize("query", [None, "ledger"])
    def test_without_from_addr_the_count_is_one_scalar(self, undecided_db, query):
        """Codex round 1: without ``from_addr`` nothing per thread is
        fetched (no ``senders`` JSON); with it, one row per thread."""
        connect = undecided_db._connect
        counts: list[tuple[str, int]] = []

        def traced_connect():
            conn = connect()
            real_execute = conn.execute

            class _Conn:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, params=()):
                    cursor = real_execute(sql, params)
                    if "IS NULL)" in sql and "COUNT(*)" in sql:
                        rows = cursor.fetchall()
                        counts.append((sql, len(rows)))
                        return _Rows(rows)
                    return cursor

            return _Conn()

        class _Rows:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0]

            def __iter__(self):
                return iter(self._rows)

        undecided_db._connect = traced_connect  # type: ignore[method-assign]
        assert _counted(undecided_db, query=query, sender="vendor").indeterminate == 2
        ((sql, rows),) = counts
        assert rows == 1
        assert "senders" not in sql
        counts.clear()
        found = _counted(undecided_db, query=query, sender="vendor", from_addr="vendor")
        assert found.indeterminate == 2
        ((sql, rows),) = counts
        assert "t.senders" in sql
        assert rows == 1  # one thread

    def test_count_work_grows_linearly_with_the_candidates(self, tmp_path):
        """Both lanes reach every undecided attachment, the worst case for
        removing the text lane's rows the filename lane already lists.
        SQLite VM steps for the whole call at 2N candidates stay well under
        the 4x of a quadratic plan. The test schema lacks the indexer's
        ``fts_rowid`` and attachment indexes, without which SQLite 3.46
        (the image's) plans the existing lanes quadratically, so they
        are created here as ``indexer/src/database.py`` creates them."""

        def steps(n: int) -> tuple[int, int | None]:
            conn, path = _open_built_db_conn(tmp_path, f"work-{n}.db")
            conn.executescript(
                "CREATE INDEX idx_attachments_attachment_id ON attachments(attachment_id);"
                "CREATE INDEX idx_attachments_thread ON attachments(thread_id);"
                "CREATE INDEX idx_attachments_claimant ON attachments(claimant_id);"
                "CREATE INDEX idx_attachments_fts_rowid ON attachments(fts_rowid);"
                "CREATE INDEX idx_message_chunks_fts_rowid ON message_chunks(fts_rowid);"
            )
            for i in range(n):
                _add(
                    conn,
                    f"u{i}",
                    f"t{i}",
                    "2024-01-10T00:00:00+00:00",
                    VENDOR,
                    text="alpha ledger",
                    sender_ambiguous=None,
                )
            conn.close()
            db = Database(str(path))
            connect = db._connect
            ticks = [0]

            def counted_connect():
                reader = connect()

                def tick() -> int:
                    ticks[0] += 1
                    return 0

                reader.set_progress_handler(tick, 100)
                return reader

            db._connect = counted_connect  # type: ignore[method-assign]
            found = _counted(db, query="ledger", sender="vendor", limit=1)
            return ticks[0], found.indeterminate

        small, small_count = steps(400)
        large, large_count = steps(800)
        assert (small_count, large_count) == (400, 800)
        assert large < 3 * small

    def test_a_long_filename_is_not_copied_per_matching_chunk(self, tmp_path):
        """Codex round 2: the text-lane candidates grouped matching chunk
        rows by the sender-controlled filename, so one 1 MB name on 300
        matching chunks of an undecided message held ~300 MB in SQLite's
        sorter. Chunks now reduce to occurrence IDs first. Peak RSS is
        measured in a child process, since SQLite's allocations are not
        visible to ``tracemalloc``; the old shape measured 1 GB at 500
        chunks x 2 MB, this one 84 MB."""
        from tests.conftest import _insert_chunk

        conn, path = _open_built_db_conn(tmp_path, "long-name.db")
        _insert_message(
            conn,
            message_id="u1",
            thread_id="t",
            sent_at="2024-01-10T00:00:00+00:00",
            subject="synthetic",
            from_=[VENDOR],
            has_attachments=True,
            sender_ambiguous=1,
        )
        _insert_attachment(
            conn, message_id="u1", thread_id="t", attachment_id="u1-att", filename="n" * 2**20
        )
        _insert_extraction(conn, attachment_id="u1-att", extracted_text="ledger")
        for i in range(300):
            _insert_chunk(
                conn,
                chunk_id=f"c{i}",
                message_id="u1",
                thread_id="t",
                text=f"ledger part {i}",
                embedding=[1.0, 0.0, 0.0, 0.0],
                chunk_index=i,
                attachment_id="u1-att",
            )
        conn.close()
        indeterminate, grown_mb = _child_count_rss(path, "query='ledger', sender='vendor'")
        assert indeterminate == 1
        assert grown_mb < 120

    def test_a_long_senders_list_is_not_copied_per_candidate(self, tmp_path):
        """The ``from_addr`` sibling: the per-thread count joined each
        thread's ``senders`` JSON before grouping, copying it once per
        candidate (300 x 1 MB here). It now groups first."""
        import json

        conn, path = _open_built_db_conn(tmp_path, "long-senders.db")
        for i in range(300):
            _add(conn, f"u{i}", "t", "2024-01-10T00:00:00+00:00", VENDOR, sender_ambiguous=1)
        senders = [VENDOR, "x" * 2**20 + "@example.com"]
        conn.execute("UPDATE threads SET senders = ?", (json.dumps(senders),))
        conn.commit()
        conn.close()
        indeterminate, grown_mb = _child_count_rss(path, "sender='vendor', from_addr='vendor'")
        assert indeterminate == 300
        assert grown_mb < 120

    def test_from_addr_rows_are_streamed_not_collected(self, tmp_path):
        """Codex round 3: with ``from_addr`` the per-thread rows (each
        carrying its ``senders`` JSON) were collected with ``fetchall``
        before summing; 200 threads x 1 MB of senders held them all at
        once. They are read from the cursor one at a time."""
        import json

        conn, path = _open_built_db_conn(tmp_path, "many-senders.db")
        for i in range(200):
            _add(conn, f"u{i}", f"t{i}", "2024-01-10T00:00:00+00:00", VENDOR, sender_ambiguous=1)
        senders = [VENDOR, "x" * 2**20 + "@example.com"]
        conn.execute("UPDATE threads SET senders = ?", (json.dumps(senders),))
        conn.commit()
        conn.close()
        indeterminate, grown_mb = _child_count_rss(path, "sender='vendor', from_addr='vendor'")
        assert indeterminate == 200
        assert grown_mb < 120

    def test_repeated_count_failures_are_rate_limited(self, undecided_db, monkeypatch, caplog):
        """Codex round 3: a count that keeps failing logs its WARNING
        once per window, then one summary line with the count; every
        call still reports unavailable and marks its own timing line."""
        from src.lib.rate_limited_log import RateLimitedLog

        now = [0.0]
        undecided_db._count_failures = RateLimitedLog(
            logging.getLogger("mcp.sqlite"),
            undecided_db._count_failures._keys,
            60.0,
            first_msg=undecided_db._count_failures._first_msg,
            summary_msg=undecided_db._count_failures._summary_msg,
            clock=lambda: now[0],
        )

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(undecided_db, "_attachment_undecided_count", boom)
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                found = _counted(undecided_db, sender="vendor")
                assert found.indeterminate is None
            now[0] = 61.0
            _counted(undecided_db, sender="vendor")
        lines = [r.getMessage() for r in caplog.records if r.name == "mcp.sqlite"]
        assert lines == [
            "Attachment search indeterminate count unavailable: OperationalError",
            "Attachment search indeterminate count unavailable in the last 61s: OperationalError=3",
            "Attachment search indeterminate count unavailable: OperationalError",
        ]
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_every_other_filter_applies(self, tmp_path, query):
        conn, path = _open_built_db_conn(tmp_path, "filters.db")
        _add(conn, "u1", "t", "2024-01-10T00:00:00+00:00", VENDOR, sender_ambiguous=None)
        _add(conn, "u2", "t", "2024-06-10T00:00:00+00:00", VENDOR, sender_ambiguous=None)
        _add(
            conn,
            "u3",
            "t",
            "2024-06-11T00:00:00+00:00",
            VENDOR,
            content_type="text/csv",
            sender_ambiguous=1,
        )
        _add(
            conn,
            "u4",
            "t",
            "2024-06-12T00:00:00+00:00",
            VENDOR,
            status="failed",
            sender_ambiguous=1,
        )
        _add(conn, "u5", "t-x", "2024-06-13T00:00:00+00:00", VENDOR, sender_ambiguous=1)
        _add(conn, "u6", "t-x", "2024-06-14T00:00:00+00:00", VENDOR, sender_ambiguous=1)
        conn.execute(
            "UPDATE messages SET folder = 'Trash' WHERE claimant_id = ?", (claimant_of("u6"),)
        )
        conn.commit()
        conn.close()
        db = Database(str(path))

        def count(**kw) -> int | None:
            return _counted(db, query=query, sender="vendor@example.com", **kw).indeterminate

        # u4's failed extraction has no text, so the text lane cannot
        # reach it; u6 is in Trash, left out as its result would be.
        reach = 5 if query != "bravo" else 4
        assert count() == reach
        assert count(date_from="2024-03-01") == reach - 1
        assert count(date_to="2024-03-01") == 1
        assert count(content_type="text/csv") == 1
        assert count(extracted_only=True) == 4
        assert count(from_addr="vendor@example.com") == reach
        assert count(from_addr="nobody@example.com") == 0

    def test_from_addr_counts_only_the_selected_threads(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "from.db")
        _add(conn, "v1", "t-v", "2024-01-10T00:00:00+00:00", VENDOR)
        _add(conn, "u1", "t-v", "2024-01-11T00:00:00+00:00", COLLEAGUE, sender_ambiguous=None)
        _add(conn, "u2", "t-o", "2024-01-12T00:00:00+00:00", COLLEAGUE, sender_ambiguous=None)
        _add(conn, "u3", "t-o", "2024-01-13T00:00:00+00:00", COLLEAGUE, sender_ambiguous=1)
        conn.close()
        db = Database(str(path))
        for query in (None, "ledger", "bravo"):
            assert _counted(db, query=query, sender="colleague").indeterminate == 3
            found = _counted(db, query=query, sender="colleague", from_addr="vendor@example.com")
            assert found.indeterminate == 1

    @pytest.mark.parametrize("value", _SHAPES)
    @pytest.mark.parametrize("query", [None, "ledger", "bravo"])
    def test_count_matches_query_messages_indeterminate(self, shapes_db, value, query):
        """One attachment per message, each reached by every query: the
        count is ``query_messages``' indeterminate for the same leaf."""
        expected = shapes_db.query_messages(sender=value, limit=100).indeterminate
        assert _counted(shapes_db, query=query, sender=value).indeterminate == expected

    def test_the_differential_catalogue_has_undecided_messages(self, shapes_db):
        assert _counted(shapes_db, sender="vendor@example.com").indeterminate == 4

    def test_results_and_count_share_one_snapshot(self, tmp_path):
        """A reparse that decides ``u1`` commits between the filename
        lane and the rest of the call. In one read snapshot ``u1`` is
        still undecided for the count, so results plus the count still
        cover it; separate reads would lose it from both."""
        conn, path = _open_built_db_conn(tmp_path, "snap.db")
        conn.execute("PRAGMA journal_mode=WAL")
        _add(conn, "u1", "t", "2024-01-10T00:00:00+00:00", VENDOR, sender_ambiguous=None)
        db = Database(str(path))
        connect = db._connect
        statements: list[str] = []

        def traced_connect():
            reader = connect()

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    statements.append(statement)
                    if len(statements) == 2:
                        conn.execute("UPDATE messages SET sender_ambiguous = 0")
                        conn.commit()

            reader.set_trace_callback(trace)
            return reader

        db._connect = traced_connect  # type: ignore[method-assign]
        try:
            found = _counted(db, query="ledger", sender="vendor@example.com")
            assert len(statements) >= 3
            assert found.results == []
            assert found.indeterminate == 1
            # The commit landed: the next call sees u1 decided.
            again = _counted(db, query="ledger", sender="vendor@example.com")
            assert _ids(again.results) == {"u1-att"}
            assert again.indeterminate == 0
        finally:
            conn.close()

    def test_a_failed_statement_keeps_the_read_transaction(self, tmp_path, caplog):
        """A lane's OperationalError is caught per statement; the shared
        read transaction and its snapshot outlive it."""
        conn, path = _open_built_db_conn(tmp_path, "stmt.db")
        conn.execute("PRAGMA journal_mode=WAL")
        _add(conn, "v1", "t", "2024-01-10T00:00:00+00:00", VENDOR)
        db = Database(str(path))
        try:
            with closing(db._connect()) as reader:
                reader.execute("BEGIN")
                before = _ids(db._attachment_scan([], [], 10, conn=reader))
                _add(conn, "v2", "t", "2024-01-11T00:00:00+00:00", VENDOR)
                # An FTS5 column filter naming no column fails in SQLite.
                bad = "nosuchcolumn:x"
                with caplog.at_level(logging.WARNING):
                    assert db._attachment_filename_lane(bad, [], [], 10, conn=reader) == []
                    assert db._attachment_text_lane(bad, None, [], [], 10, conn=reader) == []
                assert "Attachment filename search unavailable: OperationalError" in caplog.text
                assert "Attachment text search unavailable: OperationalError" in caplog.text
                assert reader.in_transaction
                assert _ids(db._attachment_scan([], [], 10, conn=reader)) == before == {"v1-att"}
                reader.rollback()
            assert _ids(db.search_attachments()) == {"v1-att", "v2-att"}
        finally:
            conn.close()

    def test_a_failed_count_is_unavailable_not_zero(self, undecided_db, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(undecided_db, "_attachment_undecided_count", boom)
        with caplog.at_level(logging.WARNING):
            found = _counted(undecided_db, query="ledger", sender="vendor@example.com")
        assert _ids(found.results) == {"v1-att"}
        assert found.indeterminate is None
        records = [r for r in caplog.records if "indeterminate count unavailable" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "OperationalError" in records[0].getMessage()
        assert MARKER not in caplog.text


class TestSearchAttachmentsIndeterminateTool:
    @staticmethod
    def _call(fake_server, fake_embed, db, **kw):
        return asyncio.run(_handler(fake_server, fake_embed, db)(**kw))

    @staticmethod
    def _timing_line(caplog) -> str:
        lines = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert len(lines) == 1, lines
        return lines[0]

    def test_count_in_prose_and_structured_output(
        self, fake_server, fake_embed, undecided_db, caplog
    ):
        with caplog.at_level(logging.INFO):
            out = self._call(fake_server, fake_embed, undecided_db, sender="vendor@example.com")
        assert out.structured_content["indeterminate"] == 2
        assert [r["attachment_id"] for r in out.structured_content["results"]] == ["v1-att"]
        lines = out.content[0].text.splitlines()
        assert lines[0] == "Found 1 attachment(s):"
        assert lines[1].startswith("indeterminate: 2 (")
        # A full address is never undecided by display names.
        assert "sender ambiguous or not yet checked" in lines[1]
        assert "display names" not in lines[1]
        assert "'indeterminate': 2" in self._timing_line(caplog)

    def test_count_on_empty_results(self, fake_server, fake_embed, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "empty.db")
        _add(conn, "u1", "t", "2024-01-10T00:00:00+00:00", VENDOR, sender_ambiguous=None)
        conn.close()
        out = self._call(fake_server, fake_embed, Database(str(path)), sender="vendor")
        assert out.structured_content["results"] == []
        assert out.structured_content["indeterminate"] == 1
        lines = out.content[0].text.splitlines()
        assert lines[0] == "No attachments are known to match."
        assert lines[1].startswith("indeterminate: 1 (")

    def test_unindexed_display_names_are_counted_and_named(self, fake_server, fake_embed, tmp_path):
        """#1140 (merged after this count was written) makes a substring
        ``sender`` that matches nothing unknown while the message's display
        names are not all indexed. The count includes it, and the fixed
        text names that cause beside the sender flag."""
        conn, path = _open_built_db_conn(tmp_path, "names.db")
        _add(conn, "n1", "t", "2024-01-10T00:00:00+00:00", COLLEAGUE)
        _add(conn, "n2", "t", "2024-01-11T00:00:00+00:00", COLLEAGUE)
        conn.execute(
            "UPDATE messages SET participant_names_complete = 0 WHERE claimant_id = ?",
            (claimant_of("n1"),),
        )
        conn.commit()
        conn.close()
        db = Database(str(path))
        # A full address is always decided; a name fragment is not.
        assert _counted(db, sender="vendor@example.com").indeterminate == 0
        assert _counted(db, sender="Vendor").indeterminate == 1
        out = self._call(fake_server, fake_embed, db, sender="Vendor")
        assert out.structured_content["indeterminate"] == 1
        line = out.content[0].text.splitlines()[1]
        assert "display names not all indexed" in line
        assert "sender ambiguous or not yet checked" in line

    def test_zero_is_stated_with_a_sender(self, fake_server, fake_embed, carrier_db):
        out = self._call(fake_server, fake_embed, carrier_db, sender="nobody@nowhere.test")
        assert out.structured_content["indeterminate"] == 0
        assert out.content[0].text == "No attachments found.\nindeterminate: 0"

    def test_no_count_without_a_sender(self, fake_server, fake_embed, undecided_db, caplog):
        with caplog.at_level(logging.INFO):
            out = self._call(fake_server, fake_embed, undecided_db, sender="  ")
        assert out.structured_content["indeterminate"] is None
        assert "indeterminate" not in out.content[0].text
        assert "indeterminate" not in self._timing_line(caplog)

    def test_unavailable_count(self, fake_server, fake_embed, undecided_db, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(undecided_db, "_attachment_undecided_count", boom)
        with caplog.at_level(logging.INFO):
            out = self._call(fake_server, fake_embed, undecided_db, sender="vendor@example.com")
        assert out.structured_content["indeterminate"] is None
        assert "indeterminate: unavailable" in out.content[0].text
        line = self._timing_line(caplog)
        assert "outcome=ok" in line
        assert "'degraded_attachment_indeterminate': 1" in line
        assert "'indeterminate':" not in line
        assert MARKER not in caplog.text

    def test_unavailable_count_on_empty_results(
        self, fake_server, fake_embed, carrier_db, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(carrier_db, "_attachment_undecided_count", boom)
        out = self._call(fake_server, fake_embed, carrier_db, sender="nobody@nowhere.test")
        lines = out.content[0].text.splitlines()
        assert lines[0] == "No attachments are known to match."
        assert lines[1].startswith("indeterminate: unavailable")

    def test_indeterminate_is_published_in_the_output_schema(self, undecided_db):
        from tests.test_tool_annotations import _wire_tools

        schema = _wire_tools(_real_server(undecided_db))["search_attachments"]["outputSchema"]
        prop = schema["properties"]["indeterminate"]
        assert {v.get("type") for v in prop["anyOf"]} == {"integer", "null"}
        doc = " ".join(prop["description"].split())
        assert "sender" in doc
        assert "unavailable" in doc

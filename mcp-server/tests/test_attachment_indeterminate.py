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

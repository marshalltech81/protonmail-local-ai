"""Attachments whose extraction the indexer deferred (#1236).

The indexer's per-message extraction budget defers an attachment's
extraction to a later pass and marks the occurrence. Its status is read
from that mark, before the payload's extraction row: the row may be an
older result or another message's, and its text is not indexed for this
occurrence. ``query_attachments`` reports ``deferred`` in
``extraction_status`` and ``status_counts`` (and filters on it),
``get_attachment`` gives a fixed reason and no text, and
``search_attachments`` shows the status without a snippet. All data is
synthetic.
"""

import asyncio
import logging

import pytest
from src.lib.security import _LOGGABLE_TOOL_PARAMS
from src.lib.sqlite import EXTRACTION_STATUS_FILTERS, Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import (
    FakeMCPServer,
    _insert_attachment,
    _insert_chunk,
    _insert_extraction,
    _insert_message,
    claimant_of,
)
from tests.test_sqlite import _open_built_db_conn

MARKER = "zq-synthetic-deferred-marker"


def _occ(n: int) -> str:
    return f"{claimant_of('carrier@example.com')}:occ-{n}"


# n -> (deferred, payload row status or None, row text)
_ROWS = {
    # Deferred, while the payload's row holds an older success.
    0: (True, "success", f"older text {MARKER}"),
    # Deferred, no row.
    1: (True, None, None),
    2: (False, "success", f"current text {MARKER}"),
    3: (False, None, None),
    4: (False, "failed", None),
}


@pytest.fixture
def db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "deferred.db")
    _insert_message(
        conn,
        message_id="carrier@example.com",
        thread_id="t1",
        sent_at="2024-02-01T09:00:00+00:00",
        has_attachments=True,
    )
    for n, (deferred, status, text) in _ROWS.items():
        _insert_attachment(
            conn,
            message_id="carrier@example.com",
            thread_id="t1",
            attachment_id=f"p-{n}",
            filename=f"report-{n}.txt",
            content_type="text/plain",
            occurrence_id=_occ(n),
            extractor_module="text",
            deferred=deferred,
        )
        if status is not None:
            _insert_extraction(
                conn,
                attachment_id=f"p-{n}",
                status=status,
                extracted_text=text,
                extractor="text@3",
                extractor_module="text",
            )
    conn.commit()
    conn.close()
    return Database(str(path))


def _tools(db) -> dict:
    server = FakeMCPServer()
    register_retrieval_tools(server, db)
    return server.tools


class TestQueryAttachments:
    def test_status_comes_from_the_occurrence_before_the_payload_row(self, db):
        page = db.query_attachments(limit=50)
        by_occ = {a.attachment_occurrence_id: a for a in page.attachments}
        assert by_occ[_occ(0)].extraction_status == "deferred"
        assert by_occ[_occ(1)].extraction_status == "deferred"
        # The payload row's result is not shown for a deferred occurrence.
        assert (by_occ[_occ(0)].extractor, by_occ[_occ(0)].extracted_at) == (None, None)
        assert by_occ[_occ(2)].extraction_status == "success"
        assert by_occ[_occ(2)].extractor == "text@3"
        assert by_occ[_occ(3)].extraction_status is None
        assert page.status_counts == dict.fromkeys(EXTRACTION_STATUS_FILTERS, 0) | {
            "deferred": 2,
            "success": 1,
            "none": 1,
            "failed": 1,
        }
        assert page.total_matches == 5

    @pytest.mark.parametrize(
        ("status", "occurrences", "indeterminate"),
        [
            ("deferred", {0, 1}, 0),
            # The deferred occurrence with an older success row is not one.
            ("success", {2}, 1),
            ("none", {3}, 0),
            ("failed", {4}, 1),
        ],
    )
    def test_the_status_filter(self, db, status, occurrences, indeterminate):
        page = db.query_attachments(extraction_status=status, limit=50)
        assert {a.attachment_occurrence_id for a in page.attachments} == {
            _occ(n) for n in occurrences
        }
        assert page.total_matches == len(occurrences)
        assert page.indeterminate == indeterminate
        assert sum(page.status_counts.values()) == page.total_matches

    def test_the_tool_reports_and_filters_deferred(self, db, caplog):
        caplog.set_level(logging.INFO)
        tool = _tools(db)["query_attachments"]
        out = asyncio.run(tool(extraction_status="deferred"))
        data = out.structured_content
        assert data["total_matches"] == 2
        assert data["indeterminate"] == 0
        assert data["status_counts"]["deferred"] == 2
        assert {a["extraction_status"] for a in data["attachments"]} == {"deferred"}
        text = out.content[0].text
        assert "Extraction: deferred" in text
        assert "deferred=2" in text
        assert "no extraction recorded yet" not in text
        # The status is an allowlisted value, logged; no mail value is.
        assert "tool=query_attachments {'extraction_status': 'deferred'" in caplog.text
        assert MARKER not in caplog.text

    def test_deferred_is_an_accepted_and_loggable_status(self):
        assert "deferred" in EXTRACTION_STATUS_FILTERS
        assert _LOGGABLE_TOOL_PARAMS["extraction_status"]("deferred")


class TestGetAttachment:
    def test_a_deferred_occurrence_has_a_fixed_reason_and_no_text(self, db, caplog):
        caplog.set_level(logging.INFO)
        tool = _tools(db)["get_attachment"]
        for n in (0, 1):
            out = asyncio.run(tool(attachment_occurrence_id=_occ(n)))
            data = out.structured_content
            assert data["text"] is None
            assert data["text_total_chars"] == 0
            assert data["attachment"]["extraction_status"] == "deferred"
            assert data["unavailable_reason"] == (
                "the indexer deferred this attachment's extraction to a later pass (its "
                "per-message extraction budget was reached); its text is not indexed yet"
            )
            assert MARKER not in str(data) + out.content[0].text
        lines = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert all("'text_unavailable_deferred': 1" in line for line in lines)
        assert MARKER not in caplog.text

    def test_a_resolved_occurrence_still_reads_its_text(self, db):
        out = asyncio.run(_tools(db)["get_attachment"](attachment_occurrence_id=_occ(2)))
        assert out.structured_content["text"] == f"current text {MARKER}"


class TestSearchAttachments:
    def test_scan_shows_deferred_without_a_snippet(self, db):
        results = db.search_attachments(limit=50)
        by_name = {r.filename: r for r in results}
        assert by_name["report-0.txt"].extraction_status == "deferred"
        assert by_name["report-0.txt"].text_snippet == ""
        assert by_name["report-2.txt"].extraction_status == "success"
        assert MARKER in by_name["report-2.txt"].text_snippet

    def test_extracted_only_leaves_deferred_out(self, db):
        names = {r.filename for r in db.search_attachments(extracted_only=True, limit=50)}
        assert names == {"report-2.txt"}

    def test_text_lane_never_anchors_on_a_deferred_occurrence(self, tmp_path):
        """Codex round 6 on #1355: chunks a deferred occurrence kept (an
        older text) are not reported through it, and a copy of the same
        payload that resolved takes the hit instead."""
        conn, path = _open_built_db_conn(tmp_path, "deferred-lane.db")
        _insert_message(
            conn,
            message_id="carrier@example.com",
            thread_id="t1",
            sent_at="2024-02-01T09:00:00+00:00",
            has_attachments=True,
        )
        rows = (
            # Deferred alone: its old chunk must not be reported.
            ("p-old", "occ-a", "alone.txt", "text", True),
            # The same payload twice: the deferred copy has the lower ID.
            ("p-pair", "occ-b", "pair.txt", "text", True),
            ("p-pair", "occ-c", "pair.htm", "html", False),
        )
        for payload, occ, name, module, deferred in rows:
            _insert_attachment(
                conn,
                message_id="carrier@example.com",
                thread_id="t1",
                attachment_id=payload,
                filename=name,
                content_type="text/plain",
                occurrence_id=f"{claimant_of('carrier@example.com')}:{occ}",
                extractor_module=module,
                deferred=deferred,
            )
            _insert_extraction(
                conn,
                attachment_id=payload,
                status="success",
                extracted_text=f"zqoldterm {payload}",
                extractor=f"{module}@1",
                extractor_module=module,
            )
        for n, payload in enumerate(("p-old", "p-pair")):
            _insert_chunk(
                conn,
                chunk_id=f"chunk-{n}",
                message_id="carrier@example.com",
                thread_id="t1",
                text=f"zqoldterm {payload}",
                embedding=[0.1, 0.2, 0.3, 0.4],
                attachment_id=payload,
                kind="attachment",
            )
        conn.commit()
        conn.close()
        results = Database(str(path)).search_attachments(query="zqoldterm", limit=50)
        assert [(r.filename, r.extraction_status) for r in results] == [("pair.htm", "success")]

    def test_filename_lane_shows_deferred(self, db):
        [result] = db.search_attachments(query="report-0", limit=50)
        assert (result.extraction_status, result.text_snippet) == ("deferred", "")

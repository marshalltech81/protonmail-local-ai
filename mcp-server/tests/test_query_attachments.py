"""``query_attachments``: exhaustive enumeration of attachment occurrences (#796).

One row per occurrence (one attachment on one message), never folded
by payload; exact ``total_matches``, ``indeterminate`` and whole-match
``status_counts``; keyset paging on descending ``(effective_at,
claimant_id, attachment_occurrence_id)``. The expected sets below are
built from the fixture's own inserts, never from the tool. All data is
synthetic.
"""

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.lib.predicates import InvalidFilterError
from src.lib.security import _LOGGABLE_TOOL_PARAMS
from src.lib.sqlite import ATTACHMENT_META_CHARS, EXTRACTION_STATUS_FILTERS, Database
from src.tools.outputs import HEADER_CHAR_LIMIT
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _insert_attachment, _insert_extraction, _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn, _snapshot_db

MARKER = "zq-synthetic-attachment-marker"

# Five stored statuses the per-message payloads cycle through.
_STATUSES = ("success", "failed", "empty", "unsupported", "too_large")
_TIE = "2024-03-01T09:00:00+00:00"


def _occ(message_id: str, n: int) -> str:
    return f"{claimant_of(message_id)}:occ-{n}"


def _build_corpus(conn) -> list[tuple[str, str, str]]:
    """Twenty INBOX messages, three occurrences each, and one Trash
    message with two. Returns the INBOX occurrences as ``(effective_at,
    claimant_id, occurrence_id)``.

    Messages ``m00``-``m09`` share one effective time (ties); the rest
    have their own. Each message carries the payload ``p-shared``
    (the same bytes on every message) and its own payload twice: once
    under the ``pdf`` module, once under no extractor (``''``), so one
    message holds the same bytes twice with different modules.
    """
    expected = []
    _insert_extraction(conn, attachment_id="p-shared", status="success", extracted_text="x")
    for i in range(20):
        mid = f"m{i:02d}@example.com"
        sent = _TIE if i < 10 else f"2024-04-{i:02d}T09:00:00+00:00"
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t{i % 4}",
            sent_at=sent,
            from_=["Jane Sender <jane@example.com>" if i % 2 else "bob@example.org"],
            to=["carol@example.net"],
            has_attachments=True,
        )
        claimant = claimant_of(mid)
        for n, (payload, name, module) in enumerate(
            (
                ("p-shared", f"shared-{i}.pdf", "pdf"),
                (f"p-{i}", f"own-{i}.pdf", "pdf"),
                (f"p-{i}", f"own-{i}.bin", ""),
            )
        ):
            _insert_attachment(
                conn,
                message_id=mid,
                thread_id=f"t{i % 4}",
                attachment_id=payload,
                filename=name,
                occurrence_id=_occ(mid, n),
                extractor_module=module,
            )
            expected.append((sent, claimant, _occ(mid, n)))
        status = _STATUSES[i % len(_STATUSES)]
        _insert_extraction(
            conn,
            attachment_id=f"p-{i}",
            status=status,
            extracted_text="text" if status == "success" else None,
        )
    _insert_message(
        conn,
        message_id="trash@example.com",
        thread_id="t-trash",
        sent_at="2024-05-01T09:00:00+00:00",
        folder="Trash",
        from_=["bob@example.org"],
        has_attachments=True,
    )
    for n in range(2):
        _insert_attachment(
            conn,
            message_id="trash@example.com",
            thread_id="t-trash",
            attachment_id="p-shared",
            filename=f"trashed-{n}.pdf",
            occurrence_id=_occ("trash@example.com", n),
        )
    conn.commit()
    return sorted(expected, reverse=True)


@pytest.fixture
def corpus(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "attachments-corpus.db")
    expected = _build_corpus(conn)
    conn.close()
    return Database(str(path)), expected


def _all_pages(db: Database, limits, **filters) -> tuple[list[str], list]:
    """Every occurrence ID over all pages, following ``next_cursor``,
    with page sizes taken in turn from ``limits`` (cycled)."""
    ids: list[str] = []
    pages = []
    cursor = None
    turn = 0
    while True:
        page = db.query_attachments(**filters, limit=limits[turn % len(limits)], cursor=cursor)
        turn += 1
        pages.append(page)
        ids += [a.attachment_occurrence_id for a in page.attachments]
        if not page.has_more:
            assert page.next_cursor is None
            return ids, pages
        cursor = page.next_cursor
        assert turn < 1000


class TestEnumeration:
    @pytest.mark.parametrize("limits", [(1,), (7,), (20,), (50,), (3, 50, 1)])
    def test_every_occurrence_once_in_order_at_any_page_size(self, corpus, limits):
        db, expected = corpus
        ids, pages = _all_pages(db, limits)
        assert len(expected) == 60
        assert ids == [occ for _, _, occ in expected]
        assert len(set(ids)) == len(ids)
        assert {p.total_matches for p in pages} == {60}
        offsets = [p.offset for p in pages]
        assert offsets == sorted(offsets) and offsets[0] == 0

    def test_same_payload_is_one_row_per_occurrence(self, corpus):
        db, _ = corpus
        page = db.query_attachments(limit=100)
        shared = [a for a in page.attachments if a.attachment_id == "p-shared"]
        assert len(shared) == 20
        own = [a for a in page.attachments if a.attachment_id == "p-3"]
        # The same bytes twice on one message, under two modules: two rows,
        # each with its own module's extraction (none for '').
        assert {(a.extractor_module, a.extraction_status) for a in own} == {
            ("pdf", "unsupported"),
            ("", None),
        }
        assert {a.claimant_id for a in own} == {claimant_of("m03@example.com")}

    def test_row_carries_identity_dates_and_source(self, corpus):
        db, _ = corpus
        [row] = db.query_attachments(
            claimant_id=claimant_of("m12@example.com"), filename="own-12.pdf"
        ).attachments
        assert row.attachment_occurrence_id == _occ("m12@example.com", 1)
        assert row.message_id == "m12@example.com"
        assert row.thread_id == "t0"
        assert row.folder == "INBOX"
        assert row.sent_at == "2024-04-12T09:00:00+00:00"
        assert row.occurred_at is None
        assert row.size_bytes == 1234
        assert row.content_type == "application/pdf"
        assert not row.filename_clipped and not row.content_type_clipped
        assert row.extraction_status == "empty"
        assert row.extractor == "pdf"
        assert row.extracted_at == "2024-01-01T00:00:00+00:00"
        assert row.source_file is not None
        assert row.source_file.locator.endswith("m12@example.com")


class TestTrash:
    def test_trash_is_left_out_by_default(self, corpus):
        db, _ = corpus
        page = db.query_attachments(limit=100)
        assert all(a.folder == "INBOX" for a in page.attachments)
        assert page.total_matches == 60

    def test_trash_is_listed_when_selected(self, corpus):
        db, _ = corpus
        page = db.query_attachments(folder="Trash", limit=100)
        assert [a.attachment_occurrence_id for a in page.attachments] == [
            _occ("trash@example.com", 1),
            _occ("trash@example.com", 0),
        ]
        assert page.total_matches == 2


class TestCursor:
    def test_cursor_from_other_filters_is_rejected(self, corpus):
        db, _ = corpus
        page = db.query_attachments(limit=5)
        with pytest.raises(InvalidFilterError, match="different filters"):
            db.query_attachments(filename="own", limit=5, cursor=page.next_cursor)

    def test_cursor_does_not_bind_the_page_size(self, corpus):
        db, expected = corpus
        first = db.query_attachments(limit=5)
        second = db.query_attachments(limit=9, cursor=first.next_cursor)
        assert second.offset == 5
        assert [a.attachment_occurrence_id for a in second.attachments] == [
            occ for _, _, occ in expected[5:14]
        ]

    def test_query_messages_cursors_are_not_accepted_and_vice_versa(self, corpus):
        db, _ = corpus
        message_cursor = db.query_messages(limit=1).next_cursor
        attachment_cursor = db.query_attachments(limit=1).next_cursor
        with pytest.raises(InvalidFilterError, match="invalid cursor"):
            db.query_attachments(limit=1, cursor=message_cursor)
        with pytest.raises(InvalidFilterError, match="invalid cursor"):
            db.query_messages(limit=1, cursor=attachment_cursor)

    @pytest.mark.parametrize("cursor", ["%%%", "e30", "bm90LWpzb24", MARKER])
    def test_malformed_cursor_is_rejected(self, corpus, cursor):
        db, _ = corpus
        with pytest.raises(InvalidFilterError, match="invalid cursor") as exc:
            db.query_attachments(cursor=cursor)
        assert exc.value.field_name == "cursor"
        assert MARKER not in str(exc.value)


class TestAttachmentFilters:
    def test_filename_is_a_literal_casefolded_substring(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "names.db")
        _insert_message(conn, message_id="a@x.test", thread_id="t", sent_at=_TIE)
        for n, name in enumerate(("STRASSE-Plan.PDF", "100%_done.txt", "1000x_done.txt")):
            _insert_attachment(
                conn,
                message_id="a@x.test",
                thread_id="t",
                attachment_id=f"p{n}",
                filename=name,
                occurrence_id=f"o{n}",
            )
        conn.close()
        db = Database(str(path))

        def names(value):
            return sorted(a.filename for a in db.query_attachments(filename=value).attachments)

        assert names("straße-plan.pdf") == ["STRASSE-Plan.PDF"]
        assert names("%_") == ["100%_done.txt"]
        assert names("_done") == ["100%_done.txt", "1000x_done.txt"]
        assert names("   ") == names(None)
        assert len(names(None)) == 3

    def test_thread_id_is_the_carrying_message_thread(self, tmp_path):
        """Codex round 1: a reparse that moves a message to another thread
        updates ``messages.thread_id`` but not an existing
        ``attachments.thread_id``; the filter and the row follow the
        message."""
        conn, path = _open_built_db_conn(tmp_path, "moved.db")
        _insert_message(conn, message_id="mv@x.test", thread_id="t-new", sent_at=_TIE)
        _insert_attachment(
            conn,
            message_id="mv@x.test",
            thread_id="t-old",
            attachment_id="p",
            filename="f.pdf",
            occurrence_id="o",
        )
        conn.close()
        db = Database(str(path))
        [row] = db.query_attachments(thread_id="t-new").attachments
        assert row.thread_id == "t-new"
        assert db.query_attachments(thread_id="t-old").total_matches == 0

    def test_exact_matches(self, corpus):
        db, _ = corpus
        m5 = claimant_of("m05@example.com")
        assert db.query_attachments(claimant_id=m5).total_matches == 3
        assert db.query_attachments(claimant_id=m5[:-1]).total_matches == 0
        assert db.query_attachments(thread_id="t1").total_matches == 15
        assert db.query_attachments(thread_id="t").total_matches == 0
        assert db.query_attachments(content_type="application/pdf").total_matches == 60
        assert db.query_attachments(content_type="application/PDF").total_matches == 0


class TestMessageFilters:
    @pytest.mark.parametrize(
        "filters",
        [
            {"sender": "jane@example.com"},
            {"sender": "Jane"},
            {"recipient": "carol@example.net"},
            {"participant": "bob@example.org"},
            {"date_from": "2024-04-15", "date_to": "2024-04-17"},
            {"folder": "Trash"},
        ],
    )
    def test_message_filters_select_the_messages_query_messages_selects(self, corpus, filters):
        db, _ = corpus
        messages = db.query_messages(**filters, limit=100)
        page = db.query_attachments(**filters, limit=100)
        carriers = {m.claimant_id for m in messages.messages}
        assert {a.claimant_id for a in page.attachments} == carriers
        assert (
            page.total_matches
            == len(page.attachments)
            == 3 * len(carriers) - (len(carriers) if filters == {"folder": "Trash"} else 0)
        )

    def test_invalid_date_is_rejected(self, corpus):
        db, _ = corpus
        with pytest.raises(InvalidFilterError) as exc:
            db.query_attachments(date_from=f"{MARKER}-01")
        assert exc.value.field_name == "date_from"


class TestExtractionStatus:
    def test_counts_cover_the_whole_match_including_none(self, corpus):
        db, _ = corpus
        page = db.query_attachments(limit=1)
        # 20 shared successes; the 20 own pdf payloads cycle five statuses;
        # the 20 '' occurrences have no extraction row.
        assert page.status_counts == {
            "success": 24,
            "failed": 4,
            "empty": 4,
            "unsupported": 4,
            "too_large": 4,
            "none": 20,
        }
        assert sum(page.status_counts.values()) == page.total_matches
        assert page.indeterminate == 0

    def test_none_selects_occurrences_without_an_extraction_row(self, corpus):
        db, _ = corpus
        page = db.query_attachments(extraction_status="none", limit=100)
        assert page.total_matches == 20
        assert {a.extractor_module for a in page.attachments} == {""}
        assert page.indeterminate == 0
        assert page.status_counts == {
            "success": 0,
            "failed": 0,
            "empty": 0,
            "unsupported": 0,
            "too_large": 0,
            "none": 20,
        }

    def test_a_stored_status_is_unknown_without_an_extraction_row(self, corpus):
        db, _ = corpus
        page = db.query_attachments(extraction_status="failed", limit=100)
        assert page.total_matches == 4
        assert {a.extraction_status for a in page.attachments} == {"failed"}
        # The 20 occurrences with no row may still be failed once extracted.
        assert page.indeterminate == 20

    def test_a_false_leaf_decides_an_unknown_one(self, corpus):
        """SQL three-valued AND: an occurrence with no extraction row on
        a message outside the date range is rejected, not indeterminate."""
        db, _ = corpus
        page = db.query_attachments(
            extraction_status="success", date_from="2024-04-19", date_to="2024-04-19"
        )
        # m19: shared (success) + own pdf (too_large) + own '' (none).
        assert page.total_matches == 1
        assert page.indeterminate == 1

    def test_unknown_sender_and_unknown_status_conjoin(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "null.db")
        for n, ambiguous in enumerate((0, None)):
            mid = f"n{n}@x.test"
            _insert_message(
                conn,
                message_id=mid,
                thread_id="t",
                sent_at=_TIE,
                from_=["jane@example.com"],
                sender_ambiguous=ambiguous,
            )
            for k, module in enumerate(("pdf", "")):
                _insert_attachment(
                    conn,
                    message_id=mid,
                    thread_id="t",
                    attachment_id=f"p{n}",
                    filename=f"f{k}",
                    occurrence_id=f"o{n}{k}",
                    extractor_module=module,
                )
            _insert_extraction(conn, attachment_id=f"p{n}", status="failed")
        conn.close()
        db = Database(str(path))
        failed = db.query_attachments(sender="jane@example.com", extraction_status="failed")
        assert [a.attachment_occurrence_id for a in failed.attachments] == ["o00"]
        # o01 (status unknown), o10 (sender unknown), o11 (both unknown).
        assert failed.indeterminate == 3
        success = db.query_attachments(sender="jane@example.com", extraction_status="success")
        # o00 and o10 are failed: false whatever the sender.
        assert success.total_matches == 0
        assert success.indeterminate == 2

    def test_an_unknown_status_value_is_rejected(self, corpus):
        db, _ = corpus
        with pytest.raises(InvalidFilterError) as exc:
            db.query_attachments(extraction_status=MARKER)
        assert exc.value.field_name == "extraction_status"
        assert MARKER not in str(exc.value)


class TestClipping:
    def _db(self, tmp_path, filename: str, content_type: str = "application/pdf") -> Database:
        conn, path = _open_built_db_conn(tmp_path, "clip.db")
        _insert_message(conn, message_id="c@x.test", thread_id="t", sent_at=_TIE)
        _insert_attachment(
            conn,
            message_id="c@x.test",
            thread_id="t",
            attachment_id="p",
            filename=filename,
            content_type=content_type,
            occurrence_id="o",
        )
        conn.close()
        return Database(str(path))

    def test_cut_matches_the_header_limit(self):
        assert ATTACHMENT_META_CHARS == HEADER_CHAR_LIMIT

    @pytest.mark.parametrize(
        ("name", "shown", "clipped"),
        [
            ("a" * 500, "a" * 500, False),
            ("a" * 501, "a" * 500, True),
            ("é" * 600, "é" * 500, True),
            ("😀" * 499 + "é", "😀" * 499 + "é", False),
            ("😀" * 501, "😀" * 500, True),
            ("ab\x00cd.pdf", "ab\x00cd.pdf", False),
            ("\x00" * 600, "\x00" * 500, True),
        ],
    )
    def test_filename_is_cut_with_a_flag(self, tmp_path, name, shown, clipped):
        [row] = self._db(tmp_path, name).query_attachments().attachments
        assert row.filename == shown
        assert row.filename_clipped is clipped

    def test_content_type_is_cut_with_a_flag(self, tmp_path):
        [row] = self._db(tmp_path, "f.pdf", "x/" + "y" * 600).query_attachments().attachments
        assert row.content_type == ("x/" + "y" * 600)[:500]
        assert row.content_type_clipped is True


class TestWork:
    @staticmethod
    def _trace(db, monkeypatch) -> list[str]:
        statements: list[str] = []
        connect = db._connect

        def traced():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(db, "_connect", traced)
        return statements

    @staticmethod
    def _shape(statements: list[str]) -> list[str]:
        return [s.split()[0].upper() for s in statements]

    def test_one_transaction_with_a_fixed_number_of_statements(self, corpus, monkeypatch):
        db, _ = corpus
        statements = self._trace(db, monkeypatch)
        db.query_attachments(limit=7)
        # BEGIN, the grouped count, the page, ROLLBACK: whatever the
        # corpus size, and no indeterminate count without an unknown leaf.
        assert self._shape(statements) == ["BEGIN", "SELECT", "WITH", "ROLLBACK"]
        assert "LIMIT 8" in statements[2]
        statements.clear()
        db.query_attachments(limit=7, extraction_status="failed")
        assert self._shape(statements) == ["BEGIN", "SELECT", "SELECT", "WITH", "ROLLBACK"]
        assert "IS NULL" in statements[2]

    def test_filenames_are_read_for_the_page_rows_only(self, corpus, monkeypatch):
        """The page's row columns (the clipped filename among them) are
        selected after the keyset ``LIMIT``: the ordered phase selects
        keys only, so a sort never copies every matching filename."""
        db, _ = corpus
        statements = self._trace(db, monkeypatch)
        db.query_attachments(limit=3)
        page_sql = statements[2]
        keys, rows = page_sql.split("LIMIT 4", 1)
        assert "filename" not in keys
        assert "a.filename" in rows

    def test_a_huge_filename_returns_only_its_head(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "huge.db")
        _insert_message(conn, message_id="h@x.test", thread_id="t", sent_at=_TIE)
        _insert_attachment(
            conn,
            message_id="h@x.test",
            thread_id="t",
            attachment_id="p",
            filename="n" * 2_000_000,
            occurrence_id="o",
        )
        conn.close()
        db = Database(str(path))
        [row] = db.query_attachments().attachments
        assert len(row.filename) == ATTACHMENT_META_CHARS
        assert row.filename_clipped


class TestSnapshot:
    def test_counts_and_page_share_one_snapshot(self, tmp_path):
        """An occurrence the indexer commits after the counts are read and
        before the page is read appears in neither."""
        db, writer = _snapshot_db(tmp_path)
        _insert_attachment(
            writer,
            message_id="a",
            thread_id="t",
            attachment_id="p",
            filename="f",
            occurrence_id="o1",
        )
        connect = db._connect

        def traced():
            conn = connect()

            def trace(statement: str) -> None:
                if statement.lstrip().upper().startswith("WITH"):
                    _insert_attachment(
                        writer,
                        message_id="a",
                        thread_id="t",
                        attachment_id="p2",
                        filename="g",
                        occurrence_id="o2",
                    )

            conn.set_trace_callback(trace)
            return conn

        db._connect = traced  # type: ignore[method-assign]
        try:
            page = db.query_attachments()
            assert page.total_matches == 1
            assert [a.attachment_occurrence_id for a in page.attachments] == ["o1"]
            # The commit landed: a fresh call sees both.
            db._connect = connect  # type: ignore[method-assign]
            assert db.query_attachments().total_matches == 2
        finally:
            writer.close()


def _handlers(fake_server, db):
    register_retrieval_tools(fake_server, db)
    return fake_server.tools


class TestTool:
    def test_structured_output_and_prose(self, fake_server, corpus):
        db, expected = corpus
        tool = _handlers(fake_server, db)["query_attachments"]
        out = asyncio.run(tool(limit=2, extraction_status="failed"))
        data = out.structured_content
        assert data["total_matches"] == 4
        assert data["indeterminate"] == 20
        assert data["returned"] == 2 and data["has_more"] is True
        assert data["status_counts"]["failed"] == 4
        assert data["filters"] == [
            {"filter": "extraction_status", "value": "failed", "match": "equals"}
        ]
        row = data["attachments"][0]
        assert set(row) >= {
            "attachment_occurrence_id",
            "attachment_id",
            "extractor_module",
            "claimant_id",
            "message_id",
            "thread_id",
            "filename",
            "filename_clipped",
            "content_type",
            "content_type_clipped",
            "size_bytes",
            "folder",
            "sent_at",
            "occurred_at",
            "source_file",
            "extraction_status",
            "extractor",
            "extracted_at",
            "ocr_pages_skipped",
        }
        text = out.content[0].text
        assert "total_matches: 4" in text
        assert "indeterminate: 20" in text
        assert "no extraction recorded" in text
        assert f"next_cursor: {data['next_cursor']}" in text
        nxt = asyncio.run(tool(limit=50, extraction_status="failed", cursor=data["next_cursor"]))
        assert nxt.structured_content["offset"] == 2
        assert nxt.structured_content["returned"] == 2
        assert "No further" not in nxt.content[0].text

    def test_a_real_client_receives_output_matching_the_schema(self, corpus):
        """Through FastMCP, so the structured output is checked against
        the published ``outputSchema`` and the enum against the input
        schema."""
        db, _ = corpus
        server = FastMCP("query-attachments-test")
        register_retrieval_tools(server, db)

        async def run():
            async with Client(server) as client:
                tool = next(t for t in await client.list_tools() if t.name == "query_attachments")
                ok = await client.call_tool_mcp("query_attachments", {"limit": 3})
                bad = await client.call_tool_mcp(
                    "query_attachments", {"extraction_status": "pending"}
                )
                # Codex round 1: a client that sends unset strings as ""
                # gets the blank-filter rule, as for every other filter.
                blanks = [
                    await client.call_tool_mcp(
                        "query_attachments", {"extraction_status": value, "limit": 3}
                    )
                    for value in ("", "   ", "\t")
                ]
                # Codex round 2: every value the database accepts, padded
                # or not, passes the schema; anything else does not.
                accepted = [
                    await client.call_tool_mcp(
                        "query_attachments", {"extraction_status": value, "limit": 1}
                    )
                    for status in EXTRACTION_STATUS_FILTERS
                    for value in (status, f" {status} ")
                ]
                rejected = [
                    await client.call_tool_mcp(
                        "query_attachments", {"extraction_status": value, "limit": 1}
                    )
                    for value in ("Success", "success,failed", "nonex", "pending", " - ")
                ]
                return tool, ok, bad, blanks, accepted, rejected

        tool, ok, bad, blanks, accepted, rejected = asyncio.run(run())
        assert not ok.is_error
        assert ok.structured_content["returned"] == 3
        assert bad.is_error
        for blank in blanks:
            assert not blank.is_error
            assert blank.structured_content["filters"] == []
            assert blank.structured_content["total_matches"] == 60
        assert not any(r.is_error for r in accepted)
        assert all(r.is_error for r in rejected)
        assert "extraction_status" in tool.input_schema["properties"]
        # Codex round 1: the served description asks for a count and a
        # disclosure before paging metadata, and claims no reader.
        description = " ".join(tool.description.split())
        assert "limit=1" in description
        assert "how many rows you will page" in description
        assert "No tool reads a listed attachment's whole text yet" in description

    def test_prose_row_and_a_page_emptied_by_churn(self, fake_server, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "prose.db")
        _insert_message(
            conn,
            message_id="p@x.test",
            thread_id="t",
            sent_at="2024-01-01T09:00:00+00:00",
            occurred_at="2024-01-02T09:00:00+00:00",
        )
        for n in range(2):
            _insert_attachment(
                conn,
                message_id="p@x.test",
                thread_id="t",
                attachment_id="p",
                filename="f" * 600,
                content_type="y" * 600,
                occurrence_id=f"o{n}",
            )
        _insert_extraction(conn, attachment_id="p", status="success", extracted_text="t")
        conn.execute("UPDATE attachment_extractions SET ocr_pages_skipped = 3")
        conn.commit()
        tool = _handlers(fake_server, Database(str(path)))["query_attachments"]
        first = asyncio.run(tool(limit=1, date_from="2024-01-01"))
        text = first.content[0].text
        assert "Date bounds (UTC): from 2024-01-01T00:00:00+00:00" in text
        assert "delivered 2024-01-02T09:00:00+00:00" in text
        assert f"File: {'f' * 500} … [cut] ({'y' * 500} … [cut], 1.2 KB)" in text
        assert "Extraction: success (3 scanned pages not OCRed)" in text
        assert "Occurrence ID: o1 | Attachment ID: p" in text
        assert first.structured_content["attachments"][0]["ocr_pages_skipped"] == 3
        # The second occurrence goes before its page is read.
        conn.execute("DELETE FROM attachments WHERE attachment_occurrence_id = 'o0'")
        conn.commit()
        conn.close()
        second = asyncio.run(
            tool(limit=1, date_from="2024-01-01", cursor=first.structured_content["next_cursor"])
        )
        assert second.structured_content["returned"] == 0
        assert second.structured_content["offset"] == 1
        assert "No further attachments." in second.content[0].text

    def test_limit_is_clamped_to_fifty(self, fake_server, corpus):
        db, _ = corpus
        tool = _handlers(fake_server, db)["query_attachments"]
        assert asyncio.run(tool(limit=500)).structured_content["returned"] == 50
        assert asyncio.run(tool(limit=0)).structured_content["returned"] == 1

    def test_padded_none_names_no_extraction_cause(self, fake_server, tmp_path):
        """A padded ``none`` is ``none``: an occurrence the sender leaf
        cannot decide is indeterminate for the sender only."""
        conn, path = _open_built_db_conn(tmp_path, "padded.db")
        _insert_message(
            conn,
            message_id="u@x.test",
            thread_id="t",
            sent_at=_TIE,
            from_=["jane@example.com"],
            sender_ambiguous=None,
        )
        _insert_attachment(
            conn, message_id="u@x.test", thread_id="t", attachment_id="p", filename="f"
        )
        conn.close()
        tool = _handlers(fake_server, Database(str(path)))["query_attachments"]
        out = asyncio.run(tool(sender="jane@example.com", extraction_status=" none "))
        assert out.structured_content["indeterminate"] == 1
        text = out.content[0].text
        assert "sender ambiguous or not yet checked" in text
        assert "no extraction recorded yet" not in text

    def test_empty_results_say_whether_any_are_undecided(self, fake_server, corpus):
        db, _ = corpus
        tool = _handlers(fake_server, db)["query_attachments"]
        none = asyncio.run(tool(filename=MARKER))
        assert "No attachments match." in none.content[0].text
        unknown = asyncio.run(
            tool(extraction_status="failed", date_from="2024-04-14", date_to="2024-04-14")
        )
        # m14 own pdf is too_large; its '' occurrence has no row.
        assert unknown.structured_content["indeterminate"] == 1
        assert "No attachments are known to match." in unknown.content[0].text

    def test_mail_values_never_reach_the_log(self, fake_server, corpus, caplog):
        db, _ = corpus
        tool = _handlers(fake_server, db)["query_attachments"]
        caplog.set_level(logging.DEBUG)
        asyncio.run(
            tool(
                filename=MARKER,
                content_type=MARKER,
                claimant_id=MARKER,
                thread_id=MARKER,
                sender=MARKER,
                folder=MARKER,
                extraction_status="none",
            )
        )
        with pytest.raises(ToolError):
            asyncio.run(tool(cursor=MARKER))
        with pytest.raises(ToolError):
            asyncio.run(tool(filename=MARKER, date_from=f"{MARKER}-01"))
        assert MARKER not in caplog.text
        assert "'extraction_status': 'none'" in caplog.text
        assert "rejected invalid argument: query_attachments.cursor" in caplog.text
        assert "rejected invalid argument: query_attachments.date_from" in caplog.text

    def test_timing_line_carries_the_counts(self, fake_server, corpus, caplog):
        db, _ = corpus
        tool = _handlers(fake_server, db)["query_attachments"]
        caplog.set_level(logging.INFO)
        asyncio.run(tool(limit=3, extraction_status="failed"))
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "tool=query_attachments" in line
        assert "'total_matches': 4" in line
        assert "'indeterminate': 20" in line
        assert "'returned': 3" in line

    def test_database_error_is_reported_by_type(self, fake_server, corpus, monkeypatch, caplog):
        db, _ = corpus

        def boom(**_):
            raise ValueError(MARKER)

        monkeypatch.setattr(db, "query_attachments", boom)
        tool = _handlers(fake_server, db)["query_attachments"]
        with pytest.raises(ToolError, match="Error: ValueError"):
            asyncio.run(tool())
        assert MARKER not in caplog.text

    def test_log_allowlist_is_the_database_statuses(self):
        """The logging allowlist is written out; it must be the
        database's ``EXTRACTION_STATUS_FILTERS``."""
        check = _LOGGABLE_TOOL_PARAMS["extraction_status"]
        assert all(check(v) for v in EXTRACTION_STATUS_FILTERS)
        assert not check(MARKER)
        assert not check("None")


class TestAddressCompleteness:
    """#1086: an address filter that finds nothing on a carrying message
    whose stored addresses for that role are incomplete (0) or not yet
    checked (NULL) cannot rule it out: the occurrence is indeterminate,
    not a miss. A stored match still decides it."""

    @pytest.fixture
    def db(self, tmp_path):
        conn, path = _open_built_db_conn(tmp_path, "completeness.db")
        # (message, From, To, completeness overrides)
        for i, (mid, from_, to, flags) in enumerate(
            (
                ("hit", "jane@example.com", "carol@example.net", {"to_addresses_complete": None}),
                ("whole", "bob@example.org", "dan@example.net", {}),
                ("to-null", "bob@example.org", "dan@example.net", {"to_addresses_complete": None}),
                ("cc-zero", "bob@example.org", "dan@example.net", {"cc_addresses_complete": 0}),
                ("from-zero", "bob@example.org", "dan@example.net", {"from_addresses_complete": 0}),
            )
        ):
            _insert_message(
                conn,
                message_id=mid,
                thread_id=f"t-{mid}",
                sent_at=f"2024-04-0{i + 1}T09:00:00+00:00",
                from_=[from_],
                to=[to],
                has_attachments=True,
                completeness=flags,
            )
            _insert_attachment(
                conn, message_id=mid, thread_id=f"t-{mid}", attachment_id=f"p-{mid}", filename="f"
            )
        conn.commit()
        conn.close()
        return Database(str(path))

    @pytest.mark.parametrize(
        ("filters", "matches", "indeterminate"),
        [
            # To NULL on "hit" does not matter: the stored To decides.
            ({"recipient": "carol@example.net"}, 1, 2),
            ({"recipient": "carol"}, 1, 2),
            # The From flag alone, for the sender role.
            ({"sender": "jane@example.com"}, 1, 1),
            # Any role incomplete leaves participant unknown on a miss.
            ({"participant": "carol@example.net"}, 1, 3),
        ],
    )
    def test_counts(self, db, filters, matches, indeterminate):
        page = db.query_attachments(**filters, limit=1)
        assert (page.total_matches, page.indeterminate) == (matches, indeterminate)
        # status_counts covers the definite matches only.
        assert sum(page.status_counts.values()) == matches

    def test_prose_and_timing_line_name_the_cause(self, fake_server, db, caplog):
        tool = _handlers(fake_server, db)["query_attachments"]
        caplog.set_level(logging.INFO)
        out = asyncio.run(tool(recipient="carol@example.net"))
        assert out.structured_content["indeterminate"] == 2
        assert (
            "address list incomplete (an over-long or unparseable address), or not yet checked"
            in out.content[0].text
        )
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "'indeterminate_cause_address_list': 1" in line
        assert "indeterminate_cause_sender_ambiguous" not in line

"""
Tests for src/tools/retrieval.py.

Retrieval tools are how the LLM follows up on a search hit (or addresses
a thread by id directly). The handlers are thin formatters around the
read-only Database, so the tests focus on:

- the body_text vs snippet fallback used when the indexer has not yet
  populated body_text on legacy threads;
- the attachment-metadata gating;
- not-found and invalid-input paths raising ``ToolError``, which the
  client receives as an ``isError`` result;
- limit/offset clamping on list_threads (an LLM-supplied ``limit=99999``
  must not turn into an unbounded scan);
- formatting contract that downstream tools depend on (Thread ID line,
  Message-IDs list, folder count line in list_folders).
"""

import asyncio
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlite_vec
from fastmcp.exceptions import ToolError
from src.lib.sqlite import (
    MAX_LISTED_CLAIMANTS,
    REAPED_BY_CLAIMANT_SQL,
    REAPED_BY_MESSAGE_ID_SQL,
    REAPED_RECORD_RETENTION_DAYS,
    AmbiguousMessageId,
    Database,
)
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import (
    RECENT_REAP_AT,
    RECENT_REAP_DAY,
    _build_schema,
    _insert_message,
    _insert_thread,
    claimant_of,
    insert_reaped,
)


@contextmanager
def _open_fixture_db(tmp_path):
    """An empty schema to insert into through ``conn``; close ``conn``
    before querying through ``db``."""
    path = tmp_path / "fixture.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    db = Database(str(path))
    yield conn, db


def _handlers(fake_server, db):
    register_retrieval_tools(fake_server, db)
    return fake_server.tools


def _text(result) -> str:
    """Extract the prose from a tool's ``CallToolResult``."""
    assert len(result.content) == 1
    return result.content[0].text


def _error(coro) -> str:
    """Run a tool call that must fail; return the ``ToolError`` message
    the client receives as an ``isError`` result."""
    with pytest.raises(ToolError) as exc:
        asyncio.run(coro)
    return str(exc.value)


class TestGetThread:
    def test_known_thread_renders_metadata_and_body(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-alpha"))
        text = _text(out)
        assert "invoice for march" in text
        assert "INBOX" in text
        assert "alice@example.com" in text
        assert "please find the invoice attached" in text
        # Local-only banner must always appear so the LLM does not claim
        # live Bridge retrieval happened.
        assert "local SQLite index only" in text

    def test_unknown_thread_raises_not_found_error(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        assert "Thread not found" in _error(handler(thread_id="t-does-not-exist"))

    def test_attachment_note_present_when_thread_has_attachments(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-alpha", include_attachments_metadata=True))
        assert "Attachments are present" in _text(out)

    def test_attachment_note_omitted_when_flag_false(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-alpha", include_attachments_metadata=False))
        assert "Attachments are present" not in _text(out)

    def test_attachment_note_omitted_for_thread_without_attachments(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-beta", include_attachments_metadata=True))
        # t-beta has no attachments; the conditional must not fire.
        assert "Attachments are present" not in _text(out)

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated read failure")

        seeded_db.get_thread_page = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        assert "Error" in _error(handler(thread_id="anything"))

    def test_messages_render_oldest_first_with_own_headers(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_thread"]
        text = _text(asyncio.run(handler(thread_id="t1")))
        assert "Messages: 2 (showing 1-2, oldest first)" in text
        first, second = text.index("[1/2] Message-ID: m1"), text.index("[2/2] Message-ID: m2")
        assert first < second
        m1, m2 = text[first:second], text[second:]
        assert "From: Jane Doe <jane@example.com>" in m1
        assert "Sent: 2024-01-10T09:00:00+00:00" in m1
        assert "the budget is approved" in m1
        assert "Subject: Re: Budget review" in m2
        assert "Cc: carol@other.org" in m2
        assert "In-Reply-To: m1" in m2
        assert "Attachments: yes" in m2
        assert "thanks, budget noted" in m2

    def test_message_bodies_exclude_attachment_text(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_thread"]
        assert "spreadsheet totals" not in _text(asyncio.run(handler(thread_id="t1")))

    def test_bodies_replace_accumulated_thread_text(self, fake_server, chunked_db):
        # With per-message bodies indexed, the accumulated thread text (a
        # retrieval artifact carrying quoted replies) is not the reading
        # representation.
        handler = _handlers(fake_server, chunked_db)["get_thread"]
        text = _text(asyncio.run(handler(thread_id="t-alpha")))
        assert "invoice number 12345 due march 31" in text
        assert "Indexed thread text" not in text

    def test_message_without_body_chunks_says_so(self, fake_server, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn, message_id="a", thread_id="t", sent_at="2024-01-01T00:00:00+00:00", body="hi"
            )
            _insert_message(
                conn, message_id="b", thread_id="t", sent_at="2024-01-02T00:00:00+00:00"
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        assert text.count("(No body text is indexed for this message.)") == 1

    def test_falls_back_to_thread_text_when_no_bodies_are_indexed(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        text = _text(asyncio.run(handler(thread_id="t-alpha")))
        assert "[1/1] Message-ID: t-alpha" in text
        assert "Indexed thread text" in text
        assert "please find the invoice attached for march" in text

    def test_pages_through_messages(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_thread"]
        first = _text(asyncio.run(handler(thread_id="t1", limit=1)))
        assert "Messages: 2 (showing 1-1, oldest first)" in first
        assert "Message-ID: m1" in first and "Message-ID: m2" not in first
        assert "call get_thread with offset=1" in first
        second = _text(asyncio.run(handler(thread_id="t1", offset=1, limit=1)))
        assert "Messages: 2 (showing 2-2, oldest first)" in second
        assert "[2/2] Message-ID: m2" in second
        assert "call get_thread with offset" not in second

    def test_offset_past_the_end_is_explicit(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_thread"]
        text = _text(asyncio.run(handler(thread_id="t1", offset=5)))
        assert "No messages at offset 5; the thread has 2." in text

    def test_limit_is_clamped(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_thread"]
        calls = []
        real = messages_db.get_thread_page

        def spy(thread_id, **kwargs):
            calls.append(kwargs)
            return real(thread_id, **kwargs)

        messages_db.get_thread_page = spy  # type: ignore[assignment]
        asyncio.run(handler(thread_id="t1", limit=100000, offset=-3))
        assert calls[0]["limit"] == 50
        assert calls[0]["offset"] == 0

    def test_long_bodies_are_cut_with_an_explicit_marker(self, fake_server, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                body="x" * 4000 + "TAIL" + "y" * 996,
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        assert "TAIL" not in text
        assert (
            f'[1,000 more characters not shown; get_message("{claimant_of("a")}") '
            "returns the full body]"
        ) in text

    def test_overlapping_chunks_render_once(self, fake_server, overlap_db):
        from tests.conftest import OVERLAP_BODY

        handler = _handlers(fake_server, overlap_db)["get_thread"]
        text = _text(asyncio.run(handler(thread_id="t-ov")))
        assert OVERLAP_BODY in text
        assert text.count("Paragraph P3 ") == 1
        assert text.count("Same line again.") == 2

    def test_long_references_are_bounded(self, fake_server, tmp_path):
        # A sender controls References; 12,000 of them must not bypass
        # get_thread's paging and body limits.
        refs = [f"ref{i:05d}@example.com" for i in range(12000)]
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                references=refs,
                body="hello",
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        assert len(text) < 3000
        assert "ref00009@example.com (+11990 more)" in text
        assert "ref00010@example.com" not in text
        assert "get_message returns full headers" in text

    def test_long_header_values_are_cut_with_a_marker(self, fake_server, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                subject="S" * 100_000,
                in_reply_to="i" * 100_000,
                body="hello",
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        # Thread subject, message subject, and In-Reply-To.
        assert text.count("… [99,500 more characters]") == 3
        assert len(text) < 5000

    def test_long_thread_participant_lists_are_summarized(self, fake_server, tmp_path):
        people = [f"p{i:04d}@example.com" for i in range(5000)]
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_thread(conn, thread_id="t", subject="s", participants=people)
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        assert "Participants: p0000@example.com" in text
        assert "p4999@example.com" not in text
        assert "more)" in text
        assert len(text) < 5000

    def test_long_recipient_lists_are_summarized(self, fake_server, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                to=[f"r{i:02d}@example.com" for i in range(12)],
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
        assert "(+2 more)" in text
        assert "r11@example.com" not in text


class TestGetMessage:
    def test_known_message_returns_thread_context(self, fake_server, seeded_db):
        # seeded_db inserts one message per thread with message_id=thread_id.
        handler = _handlers(fake_server, seeded_db)["get_message"]
        out = asyncio.run(handler(message_id="t-alpha"))
        text = _text(out)
        assert "invoice for march" in text
        assert "Message-ID: t-alpha" in text
        assert "Thread ID: t-alpha" in text
        assert "local SQLite index only" in text

    def test_renders_the_messages_own_headers(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_message"]
        text = _text(asyncio.run(handler(message_id="m2")))
        assert "Subject: Re: Budget review" in text
        assert "From: bob@example.com" in text
        assert "To: Jane Doe <jane@example.com>" in text
        assert "Cc: carol@other.org" in text
        assert "Sent: 2024-01-11T10:00:00+00:00" in text
        assert "Folder: INBOX" in text
        assert "In-Reply-To: m1" in text
        assert "References: m1" in text
        assert "Attachments: yes" in text
        # The thread is named by its root subject, not the message's.
        assert "Thread: Budget review" in text

    def test_absent_headers_are_omitted(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["get_message"]
        text = _text(asyncio.run(handler(message_id="m1")))
        assert "Cc:" not in text
        assert "In-Reply-To:" not in text
        assert "References:" not in text
        assert "Attachments: no" in text

    def test_overlapping_chunks_render_once(self, fake_server, overlap_db):
        from tests.conftest import OVERLAP_BODY

        handler = _handlers(fake_server, overlap_db)["get_message"]
        text = _text(asyncio.run(handler(message_id="ov1")))
        assert OVERLAP_BODY in text
        assert text.count("Paragraph P5 ") == 1
        assert text.count("Same line again.") == 2

    def test_returns_every_reference(self, fake_server, tmp_path):
        # get_message is the full-header view get_thread points to.
        refs = [f"ref{i:03d}@example.com" for i in range(200)]
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                references=refs,
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_message"](message_id="a")))
        assert f"References: {', '.join(refs)}" in text

    def test_lists_every_recipient(self, fake_server, tmp_path):
        # get_message is the authoritative single-message view: no
        # "+N more" summarizing.
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                to=[f"r{i:02d}@example.com" for i in range(12)],
            )
            conn.close()
            text = _text(asyncio.run(_handlers(fake_server, db)["get_message"](message_id="a")))
        assert "r11@example.com" in text
        assert "more)" not in text

    def test_unknown_message_raises_not_found_error(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_message"]
        assert "Message not found" in _error(handler(message_id="never-existed"))

    def test_reconstructs_body_from_indexed_chunks(self, fake_server, chunked_db):
        # chunked_db carries body chunk alpha-c1 for message t-alpha;
        # "12345" is in the chunk text only (not the subject/snippet), so
        # its presence proves the body was reconstructed from the chunks.
        handler = _handlers(fake_server, chunked_db)["get_message"]
        out = asyncio.run(handler(message_id="t-alpha"))
        text = _text(out)
        assert "12345" in text
        assert "Message body (the indexed body after quoted-reply stripping" in text

    def test_falls_back_to_thread_context_without_chunks(self, fake_server, seeded_db):
        # seeded_db has no message chunks — the handler must fall back to
        # parent-thread context rather than returning an empty body.
        handler = _handlers(fake_server, seeded_db)["get_message"]
        out = asyncio.run(handler(message_id="t-alpha"))
        text = _text(out)
        assert "No per-message body chunks" in text
        assert "invoice for march" in text

    def test_fallback_uses_snippet_when_body_text_empty(self, fake_server, tmp_path):
        # No chunks and no accumulated body_text — the fallback must still
        # surface the thread's snippet rather than going blank.
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_thread

        path = tmp_path / "snippet-only.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id="t1",
            subject="s",
            participants=["a@example.com"],
            senders=["a@example.com"],
            snippet="snippet-only-content",
            body_text="",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        db = Database(str(path))
        handler = _handlers(fake_server, db)["get_message"]
        out = asyncio.run(handler(message_id="t1"))
        assert "snippet-only-content" in _text(out)

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom(_message_id):
            raise RuntimeError("simulated read failure")

        seeded_db.get_message_view = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["get_message"]
        assert "Error" in _error(handler(message_id="anything"))


class TestReapedSources:
    """PLAN Phase 4 item 4: a caller holding a claimant ID or thread ID
    from an earlier answer learns that its source was reaped
    (mirror retention), not that the ID never existed."""

    _REAPED_AT = RECENT_REAP_AT

    def _db(self, tmp_path, *, survivor: bool = False):
        with _open_fixture_db(tmp_path) as (conn, db):
            claimant = insert_reaped(
                conn, message_id="gone@example.com", thread_id="t-gone", reaped_at=self._REAPED_AT
            )
            if survivor:
                _insert_message(
                    conn,
                    message_id="kept@example.com",
                    thread_id="t-gone",
                    sent_at="2024-01-11T09:00:00+00:00",
                    body="the reply that survives",
                )
            conn.close()
        return db, claimant

    def test_get_message_by_claimant_id_reports_reaped(self, fake_server, tmp_path):
        db, claimant = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_message"](message_id=claimant))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message
        assert "not found" not in message

    def test_get_message_by_bare_message_id_reports_reaped(self, fake_server, tmp_path):
        db, _ = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_message"](message_id="gone@example.com"))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message

    def test_get_message_unknown_id_still_not_found(self, fake_server, tmp_path):
        db, _ = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_message"](message_id="never@example.com"))
        assert "Message not found" in message

    def test_reaped_claimant_id_is_not_taken_over_by_a_bare_message_id(self, fake_server, tmp_path):
        """Review round 2: a live message whose sender-chosen Message-ID
        equals a reaped claimant ID must not answer a lookup of that
        claimant ID; before the reap the collision read as ambiguous."""
        with _open_fixture_db(tmp_path) as (conn, db):
            reaped = insert_reaped(
                conn,
                message_id="gone@example.com",
                thread_id="t-gone",
                reaped_at=self._REAPED_AT,
            )
            _insert_message(
                conn,
                message_id=reaped,
                thread_id="t-crafted",
                sent_at="2024-01-11T09:00:00+00:00",
                body="zqxcrafted body",
            )
            conn.close()
        handlers = _handlers(fake_server, db)
        message = _error(handlers["get_message"](message_id=reaped))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message
        assert "zqxcrafted" not in message
        # The crafted message stays reachable by its own claimant ID.
        out = asyncio.run(handlers["get_message"](message_id=claimant_of(reaped)))
        assert "zqxcrafted body" in _text(out)

    def test_message_states_only_what_the_record_shows(self, fake_server, tmp_path):
        """Review round 1: the stored time is the local reap, not the
        upstream deletion, and a file missing from the Maildir is reaped
        too, so the text must not claim an upstream deletion date."""
        db, claimant = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_message"](message_id=claimant))
        assert "removed upstream" not in message
        assert "deleted in ProtonMail" not in message
        assert "deleted upstream or its file went missing" in message

    @staticmethod
    def _count_connections(db, monkeypatch) -> list[int]:
        opened: list[int] = []
        connect = db._connect

        def counted():
            opened.append(1)
            return connect()

        monkeypatch.setattr(db, "_connect", counted)
        return opened

    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("get_message", {"message_id": "gone@example.com"}),
            ("get_thread", {"thread_id": "t-gone"}),
        ],
    )
    def test_live_and_reaped_state_read_in_one_snapshot(
        self, fake_server, tmp_path, monkeypatch, tool, kwargs
    ):
        """Review round 1: a restore landing between a live miss and a
        separate reap lookup would report a live message as reaped."""
        db, _ = self._db(tmp_path)
        opened = self._count_connections(db, monkeypatch)
        message = _error(_handlers(fake_server, db)[tool](**kwargs))
        assert "reaped from the index" in message
        assert len(opened) == 1

    def test_bare_message_id_reap_lookup_reads_one_row(self, tmp_path, monkeypatch):
        """Review round 1: many reaped files claiming one sender-chosen
        Message-ID must not make each lookup visit all of them."""
        _RECENT = datetime.fromisoformat(RECENT_REAP_AT)
        with _open_fixture_db(tmp_path) as (conn, db):
            for i in range(MAX_LISTED_CLAIMANTS * 5):
                insert_reaped(
                    conn,
                    message_id="dup@example.com",
                    variant=f"v{i}",
                    thread_id="t-gone",
                    reaped_at=(_RECENT - timedelta(days=i % 28)).isoformat(),
                )
            plans = [
                " ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))
                for sql, params in (
                    (REAPED_BY_MESSAGE_ID_SQL, ("dup@example.com", "")),
                    (REAPED_BY_CLAIMANT_SQL, ("dup@example.com", "")),
                )
            ]
            conn.close()
        assert all("TEMP B-TREE" not in p and "USING" in p for p in plans), plans
        assert REAPED_BY_MESSAGE_ID_SQL.rstrip().endswith("LIMIT 1")
        statements = TestMessageIdClaimants._trace(db, monkeypatch)
        view = db.get_message_view("dup@example.com")
        assert view is not None and view.reaped_at == _RECENT.isoformat()
        assert not [s for s in statements if "reaped_messages" in s and "MAX(" in s]

    def test_get_thread_of_fully_reaped_thread_reports_reaped(self, fake_server, tmp_path):
        db, _ = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_thread"](thread_id="t-gone"))
        assert f"reaped from the index on {RECENT_REAP_DAY} (mirror retention)" in message
        assert "not found" not in message

    def test_get_thread_unknown_id_still_not_found(self, fake_server, tmp_path):
        db, _ = self._db(tmp_path)
        message = _error(_handlers(fake_server, db)["get_thread"](thread_id="t-never"))
        assert "Thread not found" in message

    def test_get_thread_lists_messages_removed_from_a_surviving_thread(self, fake_server, tmp_path):
        db, claimant = self._db(tmp_path, survivor=True)
        out = asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t-gone"))
        assert "the reply that survives" in _text(out)
        assert f"{claimant} (reaped {RECENT_REAP_DAY})" in _text(out)
        assert out.structured_content["reaped_messages"] == [
            {"claimant_id": claimant, "reaped_at": self._REAPED_AT}
        ]
        assert out.structured_content["reaped_messages_truncated"] is False

    def test_get_thread_without_reaps_lists_none(self, fake_server, messages_db):
        out = asyncio.run(_handlers(fake_server, messages_db)["get_thread"](thread_id="t1"))
        assert "reaped" not in _text(out).lower()
        assert out.structured_content["reaped_messages"] == []

    def test_a_restored_message_is_not_listed_as_removed(self, fake_server, tmp_path):
        """Restored upstream after the reap, the message is indexed again
        under the same claimant ID; its stale record must not shadow it."""
        with _open_fixture_db(tmp_path) as (conn, db):
            insert_reaped(conn, message_id="back@example.com", thread_id="t1", reaped_at="x")
            _insert_message(
                conn,
                message_id="back@example.com",
                thread_id="t1",
                sent_at="2024-01-11T09:00:00+00:00",
                body="restored body",
            )
            conn.close()
        handlers = _handlers(fake_server, db)
        out = asyncio.run(handlers["get_thread"](thread_id="t1"))
        assert out.structured_content["reaped_messages"] == []
        out = asyncio.run(handlers["get_message"](message_id="back@example.com"))
        assert "restored body" in _text(out)

    def test_reaped_list_is_capped(self, fake_server, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            for i in range(MAX_LISTED_CLAIMANTS + 5):
                insert_reaped(
                    conn,
                    message_id=f"gone{i:02d}@example.com",
                    thread_id="t1",
                    reaped_at=self._REAPED_AT,
                )
            _insert_message(conn, message_id="kept", thread_id="t1", sent_at="2024-01-11")
            conn.close()
        out = asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t1"))
        assert len(out.structured_content["reaped_messages"]) == MAX_LISTED_CLAIMANTS
        assert out.structured_content["reaped_messages_truncated"] is True

    def _expired_db(self, tmp_path, *, survivor: bool = False):
        """One record just inside the retention window and one just past
        it, as an indexer whose prune has not run yet leaves them."""
        now = datetime.fromisoformat(RECENT_REAP_AT)
        expired = (now - timedelta(days=REAPED_RECORD_RETENTION_DAYS + 1)).isoformat()
        with _open_fixture_db(tmp_path) as (conn, db):
            old = insert_reaped(
                conn, message_id="old@example.com", thread_id="t-old", reaped_at=expired
            )
            fresh = insert_reaped(
                conn, message_id="new@example.com", thread_id="t-new", reaped_at=RECENT_REAP_AT
            )
            if survivor:
                insert_reaped(
                    conn, message_id="old2@example.com", thread_id="t-new", reaped_at=expired
                )
                _insert_message(
                    conn,
                    message_id="kept@example.com",
                    thread_id="t-new",
                    sent_at="2024-01-11T09:00:00+00:00",
                    body="the reply that survives",
                )
            conn.close()
        return db, old, fresh

    def test_expired_record_reads_as_not_found(self, fake_server, tmp_path):
        """#576: a record past the retention window is never served, even
        while the indexer has not pruned it yet."""
        db, old, fresh = self._expired_db(tmp_path)
        handlers = _handlers(fake_server, db)
        for identifier in (old, "old@example.com"):
            message = _error(handlers["get_message"](message_id=identifier))
            assert "Message not found" in message
            assert "reaped" not in message
        message = _error(handlers["get_thread"](thread_id="t-old"))
        assert "Thread not found" in message
        assert "reaped" not in message
        # A fresh record is still served.
        for identifier in (fresh, "new@example.com"):
            assert "reaped from the index" in _error(handlers["get_message"](message_id=identifier))
        assert "reaped from the index" in _error(handlers["get_thread"](thread_id="t-new"))

    def test_expired_record_is_not_listed_on_a_surviving_thread(self, fake_server, tmp_path):
        db, _old, fresh = self._expired_db(tmp_path, survivor=True)
        out = asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t-new"))
        # The fresh record on the thread is listed; the expired one is not.
        assert out.structured_content["reaped_messages"] == [
            {"claimant_id": fresh, "reaped_at": RECENT_REAP_AT}
        ]

    def test_retention_and_schema_mirror_the_indexer(self, tmp_path):
        """The documented retention and the fixture table are only true
        while they match what the indexer writes."""
        source = INDEXER_DATABASE.read_text()
        assert f"REAPED_RECORD_RETENTION_DAYS = {REAPED_RECORD_RETENTION_DAYS}\n" in source
        with _open_fixture_db(tmp_path) as (conn, _db):
            mirror = [
                " ".join(r[0].split())
                for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name = 'reaped_messages' "
                    "AND sql IS NOT NULL ORDER BY name"
                )
            ]
            conn.close()
        flat = " ".join(source.split())
        assert mirror and all(statement in flat for statement in mirror)


class TestMessageIdClaimants:
    """#217: two files can claim one Message-ID. The index keeps both,
    each under a claimant ID (``<Message-ID>#<hash prefix>``), and the
    tools carry that ID so a caller can address either one. The bare
    Message-ID still works while it names one message; when it names
    several, get_message lists them instead of picking one."""

    def _two_claimants(self, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            for variant, sent_at, body in (
                ("", "2024-01-10T09:00:00+00:00", "original wording alphaword"),
                ("b", "2024-01-11T09:00:00+00:00", "replacement wording betaword"),
            ):
                _insert_message(
                    conn,
                    message_id="dup@example.com",
                    variant=variant,
                    thread_id="t1",
                    sent_at=sent_at,
                    subject="Shared subject",
                    from_=["jane@example.com"],
                    body=body,
                )
            conn.close()
        return db, claimant_of("dup@example.com"), claimant_of("dup@example.com", "b")

    def test_bare_message_id_with_several_claimants_lists_them(self, fake_server, tmp_path):
        db, first, second = self._two_claimants(tmp_path)
        text = _error(_handlers(fake_server, db)["get_message"](message_id="dup@example.com"))
        assert "2 messages" in text
        assert first in text and second in text
        assert "alphaword" not in text and "betaword" not in text

    def test_claimant_id_selects_one_message(self, fake_server, tmp_path):
        db, first, second = self._two_claimants(tmp_path)
        out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id=second))
        text = _text(out)
        assert "betaword" in text and "alphaword" not in text
        assert f"Claimant ID: {second}" in text
        assert first in text  # named as the other claimant
        message = out.structured_content["message"]
        assert message["message_id"] == "dup@example.com"
        assert message["claimant_id"] == second
        assert out.structured_content["other_claimants"] == [first]
        assert out.structured_content["other_claimants_truncated"] is False

    def test_message_id_crafted_to_equal_a_claimant_id_is_ambiguous(self, fake_server, tmp_path):
        """A sender can set a Message-ID equal to another message's
        claimant ID; that ID then names both, so neither is returned."""
        victim = claimant_of("victim@example.com")
        with _open_fixture_db(tmp_path) as (conn, db):
            for mid in ("victim@example.com", victim):
                _insert_message(conn, message_id=mid, thread_id="t1", sent_at="2024-01-10")
            conn.close()
        text = _error(_handlers(fake_server, db)["get_message"](message_id=victim))
        assert "2 messages" in text
        assert claimant_of(victim) in text

    def test_single_claimant_bare_message_id_unchanged(self, fake_server, messages_db):
        out = asyncio.run(_handlers(fake_server, messages_db)["get_message"](message_id="m1"))
        assert "the budget is approved" in _text(out)
        assert out.structured_content["message"]["claimant_id"] == claimant_of("m1")
        assert out.structured_content["other_claimants"] == []

    def test_get_thread_lists_each_claimant_with_its_own_body(self, fake_server, tmp_path):
        db, first, second = self._two_claimants(tmp_path)
        out = asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t1"))
        messages = out.structured_content["messages"]
        assert [(m["claimant_id"], m["message_id"]) for m in messages] == [
            (first, "dup@example.com"),
            (second, "dup@example.com"),
        ]
        assert "alphaword" in messages[0]["body"] and "betaword" in messages[1]["body"]
        text = _text(out)
        assert f"Claimant ID: {first}" in text and f"Claimant ID: {second}" in text

    def _many_claimants(self, tmp_path, count):
        """``count`` files claiming ``dup@example.com``, inserted newest
        first so arrival order differs from the listing order. Returns
        the db and the claimant IDs, oldest first."""
        with _open_fixture_db(tmp_path) as (conn, db):
            for i in reversed(range(count)):
                _insert_message(
                    conn,
                    message_id="dup@example.com",
                    variant=f"v{i}",
                    thread_id="t1",
                    sent_at=f"2024-01-{i + 1:02d}T09:00:00+00:00",
                    from_=["jane@example.com"],
                    to=["bob@example.com"],
                )
            conn.close()
        return db, [claimant_of("dup@example.com", f"v{i}") for i in range(count)]

    @staticmethod
    def _trace(db, monkeypatch):
        """Record every statement ``db`` runs, with its bound values."""
        statements: list[str] = []
        connect = db._connect

        def traced():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(db, "_connect", traced)
        return statements

    def test_bare_message_id_lists_at_most_the_cap(self, fake_server, tmp_path, monkeypatch):
        """#456: a bare Message-ID claimed by many files lists the oldest
        ``MAX_LISTED_CLAIMANTS`` and says more exist, reading at most one
        row past the cap and no participants."""
        db, claimants = self._many_claimants(tmp_path, MAX_LISTED_CLAIMANTS + 5)
        statements = self._trace(db, monkeypatch)
        text = _error(_handlers(fake_server, db)["get_message"](message_id="dup@example.com"))
        assert f"more than {MAX_LISTED_CLAIMANTS} messages" in text
        listed = claimants[:MAX_LISTED_CLAIMANTS]
        assert [c for c in claimants if c in text] == listed
        assert text.index(listed[0]) < text.index(listed[-1])
        records = [s for s in statements if "FROM messages m WHERE" in s]
        # One claimant-ID lookup and one Message-ID lookup (#538).
        assert len(records) == 2
        assert all(f"LIMIT {MAX_LISTED_CLAIMANTS + 1} " in r for r in records)
        assert not [s for s in statements if "FROM message_participants" in s]

    def test_bare_message_id_at_the_cap_lists_all(self, fake_server, tmp_path):
        db, claimants = self._many_claimants(tmp_path, MAX_LISTED_CLAIMANTS)
        text = _error(_handlers(fake_server, db)["get_message"](message_id="dup@example.com"))
        assert f"names {MAX_LISTED_CLAIMANTS} messages" in text
        assert "more than" not in text
        assert all(c in text for c in claimants)

    def test_other_claimants_capped_and_flagged(self, fake_server, tmp_path, monkeypatch):
        """#456: a claimant ID whose Message-ID many other files claim
        lists at most ``MAX_LISTED_CLAIMANTS`` of them, in claimant-ID
        order, with ``other_claimants_truncated`` set."""
        db, claimants = self._many_claimants(tmp_path, MAX_LISTED_CLAIMANTS + 5)
        statements = self._trace(db, monkeypatch)
        out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id=claimants[0]))
        others = sorted(claimants[1:])[:MAX_LISTED_CLAIMANTS]
        assert out.structured_content["other_claimants"] == others
        assert out.structured_content["other_claimants_truncated"] is True
        text = _text(out)
        assert f"first {MAX_LISTED_CLAIMANTS} of more than" in text
        query = [s for s in statements if "claimant_id != " in s]
        assert len(query) == 1 and f"LIMIT {MAX_LISTED_CLAIMANTS + 1}" in query[0]

    def test_other_claimants_at_the_cap_not_truncated(self, fake_server, tmp_path):
        db, claimants = self._many_claimants(tmp_path, MAX_LISTED_CLAIMANTS + 1)
        out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id=claimants[0]))
        assert out.structured_content["other_claimants"] == sorted(claimants[1:])
        assert out.structured_content["other_claimants_truncated"] is False
        assert "first " not in _text(out)

    def test_query_messages_returns_both_claimants(self, fake_server, tmp_path):
        db, first, second = self._two_claimants(tmp_path)
        out = asyncio.run(_handlers(fake_server, db)["query_messages"](sender="jane@example.com"))
        assert out.structured_content["total_matches"] == 2
        assert [m["claimant_id"] for m in out.structured_content["messages"]] == [second, first]


INDEXER_DATABASE = Path(__file__).resolve().parents[2] / "indexer" / "src" / "database.py"


def _messages_indexes(sql_text: str) -> dict[str, str]:
    """``CREATE INDEX`` statements on ``messages``: name to column list."""
    return {
        name: " ".join(cols.split())
        for name, cols in re.findall(r"CREATE INDEX (\w+)\s+ON messages\(([^)]*)\)", sql_text)
    }


class TestGetMessageIndexWalk:
    """#538: the claimant listings in ``get_message`` walk an index in
    the order they return, so ``LIMIT`` stops the walk instead of every
    file claiming the Message-ID being read and sorted."""

    MESSAGE_ID = "dup@example.com"

    def _flood(self, tmp_path, count):
        """One full message claiming ``MESSAGE_ID`` plus ``count`` bare
        ``messages`` rows claiming it too. Returns the db and the full
        message's claimant ID."""
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn, message_id=self.MESSAGE_ID, thread_id="t1", sent_at="2024-01-01T09:00:00"
            )
            conn.executemany(
                "INSERT INTO messages (claimant_id, message_id, thread_id, filepath, folder, "
                "subject, sent_at, references_json, has_attachments, indexed_at) "
                "VALUES (?, ?, 't1', ?, 'INBOX', 's', ?, '[]', 0, '2024-01-01')",
                (
                    (f"{self.MESSAGE_ID}#{i:08x}", self.MESSAGE_ID, f"/f{i}", f"2023-{i % 9 + 1}")
                    for i in range(count)
                ),
            )
            conn.commit()
            conn.close()
        return db, claimant_of(self.MESSAGE_ID)

    @staticmethod
    def _instrument(db, monkeypatch):
        """Record each statement ``db`` runs (values inlined) and count
        the SQLite VM steps it takes."""
        statements: list[str] = []
        steps = [0]
        connect = db._connect

        def tick():
            steps[0] += 1
            return 0

        def instrumented():
            conn = connect()
            conn.set_trace_callback(statements.append)
            conn.set_progress_handler(tick, 1)
            return conn

        monkeypatch.setattr(db, "_connect", instrumented)
        return statements, steps

    def test_test_schema_mirrors_the_indexer_messages_indexes(self, tmp_path):
        """The plans below are only evidence if the fixture schema has
        the indexer's ``messages`` indexes."""
        with _open_fixture_db(tmp_path) as (conn, _db):
            mirror = "\n".join(
                r[0]
                for r in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'messages' "
                    "AND sql IS NOT NULL"
                )
            )
            conn.close()
        indexer = _messages_indexes(INDEXER_DATABASE.read_text())
        assert indexer and _messages_indexes(mirror) == indexer

    @pytest.mark.parametrize("by_claimant", [False, True])
    def test_listing_queries_walk_an_index_in_order(self, tmp_path, monkeypatch, by_claimant):
        db, claimant = self._flood(tmp_path, 200)
        statements, _ = self._instrument(db, monkeypatch)
        db.get_message_view(claimant if by_claimant else self.MESSAGE_ID)
        listings = [s for s in statements if "FROM messages" in s]
        assert listings
        with closing(sqlite3.connect(db.path)) as conn:
            for sql in listings:
                plan = " | ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))
                assert "TEMP B-TREE" not in plan, (sql, plan)
                assert "MULTI-INDEX OR" not in plan, (sql, plan)
                assert "SCAN" not in plan, (sql, plan)

    @pytest.mark.parametrize("by_claimant", [False, True])
    def test_work_does_not_grow_with_the_claimant_count(self, tmp_path, monkeypatch, by_claimant):
        """10,000 files claiming one Message-ID cost no more VM steps
        than 50: the walk stops at the listing cap."""
        counts = {}
        for count in (50, 10_000):
            (tmp_path / str(count)).mkdir()
            db, claimant = self._flood(tmp_path / str(count), count)
            _, steps = self._instrument(db, monkeypatch)
            db.get_message_view(claimant if by_claimant else self.MESSAGE_ID)
            counts[count] = steps[0]
        assert counts[10_000] <= counts[50] * 1.1, counts

    def test_claimant_match_merges_into_the_message_id_listing(self, fake_server, tmp_path):
        """An identifier that is one message's claimant ID and, crafted,
        the Message-ID of more than the cap of others lists the oldest
        ``MAX_LISTED_CLAIMANTS`` of all of them, the claimant included."""
        victim = claimant_of("victim@example.com")
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="victim@example.com",
                thread_id="t1",
                sent_at="2024-01-10T09:00:00+00:00",
            )
            for i in range(MAX_LISTED_CLAIMANTS + 5):
                _insert_message(
                    conn,
                    message_id=victim,
                    variant=f"v{i}",
                    thread_id="t2",
                    sent_at=f"2024-01-{i + 1:02d}T10:00:00+00:00",
                )
            conn.close()
        crafted = [claimant_of(victim, f"v{i}") for i in range(MAX_LISTED_CLAIMANTS + 5)]
        expected = (crafted[:9] + [victim] + crafted[9:])[:MAX_LISTED_CLAIMANTS]
        view = db.get_message_view(victim)
        assert isinstance(view, AmbiguousMessageId)
        assert [r.claimant_id for r in view.claimants] == expected
        assert view.truncated is True


class TestListThreads:
    def test_returns_threads_in_folder(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        out = asyncio.run(handler(folder="INBOX"))
        text = _text(out)
        # INBOX has t-alpha and t-beta; both subjects should appear.
        assert "invoice for march" in text
        assert "lunch plans" in text
        # Archive's t-gamma should not.
        assert "meeting notes archive" not in text

    def test_empty_folder_returns_no_threads_message(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        out = asyncio.run(handler(folder="Trash"))
        assert "No threads found" in _text(out)

    def test_above_ceiling_limit_is_clamped(self, fake_server, seeded_db):
        # Spy on db.list_threads to confirm clamping happened before the
        # query. 100 is the documented ceiling at clamp_int(maximum=100).
        seen: dict = {}
        original = seeded_db.list_threads

        def spy(**kwargs):
            seen["limit"] = kwargs.get("limit")
            seen["offset"] = kwargs.get("offset")
            return original(**kwargs)

        seeded_db.list_threads = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        asyncio.run(handler(folder="INBOX", limit=10_000, offset=-50))
        assert seen["limit"] == 100
        # Negative offset must clamp to 0 to avoid SQL OFFSET errors.
        assert seen["offset"] == 0

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated read failure")

        seeded_db.list_threads = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        assert "Error" in _error(handler(folder="INBOX"))

    def test_unsupported_filter_type_returns_error_text(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        text = _error(handler(folder="INBOX", filter_type="unread"))
        assert "Error" in text
        assert "filter_type" in text

    def test_value_error_after_validation_is_type_only(
        self, fake_server, seeded_db, monkeypatch, caplog
    ):
        """Only the filter_type check returns its message; any other
        ValueError (row conversion, output validation) can quote stored
        mail, so it reaches the caller and the log as its type (#257)."""

        def boom(*_args, **_kwargs):
            raise ValueError(f"bad date {_ERROR_MARKER}")

        monkeypatch.setattr(seeded_db, "list_threads", boom)
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        with caplog.at_level("DEBUG"):
            text = _error(handler(folder="INBOX"))
        assert _ERROR_MARKER not in text
        assert _ERROR_MARKER not in caplog.text
        assert "ValueError" in text


class TestListFolders:
    def test_lists_each_folder_with_count(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["list_folders"]
        out = asyncio.run(handler())
        text = _text(out)
        # seeded_db has INBOX (2 threads) and Archive (1 thread).
        assert "INBOX" in text
        assert "Archive" in text
        assert "2 threads" in text
        assert "1 threads" in text

    def test_empty_index_returns_no_folders_message(self, fake_server, empty_db):
        handler = _handlers(fake_server, empty_db)["list_folders"]
        out = asyncio.run(handler())
        assert "No folders found" in _text(out)

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom():
            raise RuntimeError("simulated read failure")

        seeded_db.list_folders = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["list_folders"]
        assert "Error" in _error(handler())


class TestFindContact:
    """The MCP tool wrapping ``Database.find_contact``. Tests focus on
    the rendered text — what the LLM actually receives — rather than
    re-asserting the aggregation, which is covered in ``test_sqlite.py``.
    """

    def test_renders_contacts_with_email_and_count(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        # ``alice`` matches alice@example.com, who appears in t-alpha
        # (sender) and t-beta (participant) per the seeded fixture.
        out = asyncio.run(handler(query="alice"))
        text = _text(out)
        assert "alice@example.com" in text
        # Count formatting must surface the number so the LLM can pick
        # the most-active sender when several match.
        assert "Threads: 2" in text

    def test_renders_the_contact_organization(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        out = asyncio.run(handler(query="alice"))
        assert "Organization: example.com" in _text(out)
        assert out.structuredContent["contacts"][0]["organization"] == "example.com"

    def test_no_match_returns_empty_sentinel(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        out = asyncio.run(handler(query="zzznosuchname"))
        assert "No contacts found" in _text(out)

    def test_empty_query_raises_guidance_error(self, fake_server, seeded_db):
        # An empty string would otherwise hit the DB as a no-op aggregation;
        # the tool must short-circuit with a guidance error so the LLM
        # gets a clear signal rather than an empty list.
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        assert "Provide a name" in _error(handler(query=""))

    def test_above_ceiling_limit_is_clamped(self, fake_server, seeded_db):
        # An LLM-supplied ``limit=99999`` should clamp to the documented
        # ceiling (50) before reaching the DB. Spy on the call to confirm.
        seen: dict = {}
        original = seeded_db.find_contact

        def spy(query, limit):
            seen["limit"] = limit
            return original(query, limit)

        seeded_db.find_contact = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        asyncio.run(handler(query="alice", limit=99999))
        assert seen["limit"] == 50

    def test_many_long_aliases_are_bounded(self, fake_server, seeded_db):
        # A contact's whole history can carry any number of
        # sender-controlled display names: list at most MAX_LISTED, cut
        # each one, and report the full count.
        from src.tools.outputs import HEADER_CHAR_LIMIT, MAX_LISTED

        names = [f"Alias{n:02d} " + "x" * 2000 for n in range(30)]

        def many(_query, _limit):
            return [
                {
                    "email": "p@example.test",
                    "names": names,
                    "thread_count": 30,
                    "organization": "example.test",
                    "authority_class": "unclassified",
                    "authority_rule": None,
                }
            ]

        seeded_db.find_contact = many  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        out = asyncio.run(handler(query="alias"))
        contact = out.structuredContent["contacts"][0]
        assert contact["name_count"] == 30
        assert contact["thread_count"] == 30
        assert len(contact["names"]) == MAX_LISTED
        assert all(len(n) < HEADER_CHAR_LIMIT + 40 for n in contact["names"])
        text = _text(out)
        assert "Alias09" in text and "Alias10" not in text
        assert "20 more" in text
        assert "x" * (HEADER_CHAR_LIMIT + 1) not in text

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom(_query, _limit):
            raise RuntimeError("simulated read failure")

        seeded_db.find_contact = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        assert "Error" in _error(handler(query="alice"))


class TestQueryMessages:
    """The MCP tool wrapping ``Database.query_messages``: the enumeration
    contract (total, returned, has_more, cursor) must be stated in the
    text the LLM receives, never left for it to infer."""

    def test_states_counts_and_cursor(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler(sender="jane@example.com", limit=2)))
        assert "total_matches: 3" in text
        assert "returned: 2 (matches 1-2)" in text
        assert "has_more: true" in text
        assert "next_cursor: " in text
        assert "Message-ID: m5" in text
        assert "Thread ID: t3" in text

    def test_inverted_date_range_is_an_error(self, fake_server, messages_db):
        # #312: an empty interval is rejected rather than answered.
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _error(handler(date_from="2024-02-01", date_to="2024-01-01"))
        assert "date_from must not be after date_to" in text

    def test_following_the_cursor_returns_the_rest(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        first = _text(asyncio.run(handler(sender="jane@example.com", limit=2)))
        cursor = next(
            line.split(": ", 1)[1] for line in first.splitlines() if line.startswith("next_cursor:")
        )
        text = _text(asyncio.run(handler(sender="jane@example.com", limit=2, cursor=cursor)))
        assert "returned: 1 (matches 3-3)" in text
        assert "has_more: false" in text
        assert "next_cursor" not in text
        assert "Message-ID: m1" in text

    def test_describes_how_each_address_predicate_matched(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler(sender="Jane@Example.com", recipient="@other.org")))
        assert "sender=jane@example.com (exact address)" in text
        assert "recipient='@other.org' (substring of address or name)" in text

    def test_renders_participants_by_role(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler(subject="Re: Budget")))
        assert "From: bob@example.com" in text
        assert "To: Jane Doe <jane@example.com>" in text
        assert "Cc: carol@other.org" in text
        assert "2024-01-11T10:00:00+00:00 | INBOX | attachments" in text

    def test_no_filters_is_labelled(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler()))
        assert "no filters" in text
        assert "total_matches: 5" in text

    def test_zero_matches_is_explicit(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler(sender="nobody@example.com")))
        assert "total_matches: 0" in text
        # An empty page still carries the full paging contract.
        assert "returned: 0" in text
        assert "has_more: false" in text
        assert "next_cursor" not in text
        assert "No messages match" in text

    def test_invalid_input_value_is_not_logged(self, fake_server, messages_db, caplog):
        # log_tool_call withholds a non-ISO date_from; the validation
        # error quoting it must not put it back in the log.
        handler = _handlers(fake_server, messages_db)["query_messages"]
        with caplog.at_level("DEBUG"):
            text = _error(handler(date_from="private-sentinel-value"))
        assert "private-sentinel-value" in text  # the caller still learns why
        assert "private-sentinel-value" not in caplog.text
        assert "date_from" in caplog.text

    def test_long_recipient_lists_state_what_was_left_out(self, fake_server, tmp_path):
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_message

        path = tmp_path / "many.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_message(
            conn,
            message_id="m1",
            thread_id="t1",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["a@example.com"],
            to=[f"r{i:02d}@example.com" for i in range(13)],
        )
        conn.close()
        handler = _handlers(fake_server, Database(str(path)))["query_messages"]
        text = _text(asyncio.run(handler()))
        assert "r09@example.com (+3 more)" in text
        assert "r10@example.com" not in text

    def test_above_ceiling_limit_is_clamped(self, fake_server, messages_db):
        seen: dict = {}
        original = messages_db.query_messages

        def spy(**kwargs):
            seen.update(kwargs)
            return original(**kwargs)

        messages_db.query_messages = spy  # type: ignore[assignment]
        handler = _handlers(fake_server, messages_db)["query_messages"]
        asyncio.run(handler(limit=99999))
        assert seen["limit"] == 100

    def test_invalid_input_returns_the_reason(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        assert "date_from" in _error(handler(date_from="last tuesday"))
        assert "cursor" in _error(handler(cursor="garbage"))

    def test_db_exception_returns_error_text(self, fake_server, messages_db):
        def boom(**_kwargs):
            raise RuntimeError("simulated read failure")

        messages_db.query_messages = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, messages_db)["query_messages"]
        assert "Error" in _error(handler())


_ERROR_MARKER = "privatemarkerq7z"


class TestQueryMessagesValueErrorWithheld:
    def test_conversion_value_error_is_type_only(
        self, fake_server, messages_db, monkeypatch, caplog
    ):
        """Only argument validation (InvalidFilterError) returns its text;
        a ValueError from converting stored rows can quote mail (#257)."""

        def boom(*_args, **_kwargs):
            raise ValueError(f"bad stored value {_ERROR_MARKER}")

        monkeypatch.setattr(messages_db, "query_messages", boom)
        handler = _handlers(fake_server, messages_db)["query_messages"]
        with caplog.at_level("DEBUG"):
            text = _error(handler(text="budget"))
        assert _ERROR_MARKER not in text
        assert _ERROR_MARKER not in caplog.text
        assert "ValueError" in text

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"text": "!!!"}, "at least one word"),
            ({"cursor": "not-a-cursor"}, "cursor"),
            ({"date_from": "yesterday-ish"}, "date_from"),
        ],
    )
    def test_validation_messages_still_reach_the_caller(
        self, fake_server, messages_db, caplog, kwargs, expected
    ):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        with caplog.at_level("DEBUG"):
            text = _error(handler(**kwargs))
        assert expected in text
        for value in kwargs.values():
            assert value not in caplog.text


class TestHandlerErrorTextWithheld:
    """A query-path failure reaches the log and the caller as its type
    only: an SQLite error can quote withheld arguments (an FTS5 error
    quotes the term) or stored mail (#257)."""

    @pytest.mark.parametrize(
        ("fixture", "tool", "method", "kwargs"),
        [
            ("seeded_db", "get_thread", "get_thread_page", {"thread_id": "t-alpha"}),
            ("seeded_db", "get_message", "get_message_view", {"message_id": "t-alpha"}),
            ("seeded_db", "list_threads", "list_threads", {"folder": "INBOX"}),
            ("messages_db", "query_messages", "query_messages", {"text": "budget"}),
            ("seeded_db", "find_contact", "find_contact", {"query": "alice"}),
            ("seeded_db", "list_folders", "list_folders", {}),
        ],
    )
    def test_sqlite_error_text_is_withheld(
        self, request, fake_server, monkeypatch, caplog, fixture, tool, method, kwargs
    ):
        db = request.getfixturevalue(fixture)

        def boom(*_args, **_kwargs):
            raise sqlite3.OperationalError(f"no such column: {_ERROR_MARKER}")

        monkeypatch.setattr(db, method, boom)
        handler = _handlers(fake_server, db)[tool]
        with caplog.at_level("DEBUG"):
            text = _error(handler(**kwargs))
        assert _ERROR_MARKER not in text
        assert _ERROR_MARKER not in caplog.text
        assert "OperationalError" in text
        assert "OperationalError" in caplog.text

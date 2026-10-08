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
            "pages through the full body: follow next_offset]"
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
        assert "long headers are shortened" in text

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

    def test_long_references_are_bounded(self, fake_server, tmp_path):
        # #489: References are sender-controlled; get_message lists at
        # most 10 like every other tool, with a count of the rest.
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
            out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id="a"))
        text = _text(out)
        assert len(text) < 3000
        assert "ref00009@example.com (+11990 more)" in text
        assert "ref00010@example.com" not in text
        message = out.structured_content["message"]
        assert message["references"] == refs[:10]
        assert message["references_count"] == 12000

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
            out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id="a"))
        assert "r09@example.com (+2 more)" in _text(out)
        assert "r11@example.com" not in _text(out)
        assert len(out.structured_content["message"]["to"]) == 10
        assert out.structured_content["message"]["to_count"] == 12

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
            out = asyncio.run(_handlers(fake_server, db)["get_message"](message_id="a"))
        text = _text(out)
        # Message subject, In-Reply-To, and the thread subject.
        assert text.count("… [99,500 more characters]") == 3
        assert len(text) < 5000
        assert out.structured_content["thread_subject"].endswith("[99,500 more characters]")

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


class TestGetMessagePaging:
    """#489: get_message returns the body in pages of
    ``_MESSAGE_BODY_PAGE_CHARS`` characters with a ``next_offset``;
    paging from offset 0 to the end reconstructs the body exactly."""

    def _db(self, tmp_path, body):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="a",
                thread_id="t",
                sent_at="2024-01-01T00:00:00+00:00",
                body=body,
            )
            conn.close()
        return db

    def _pages(self, handler):
        pages, offset, calls = [], 0, 0
        while offset is not None:
            out = asyncio.run(handler(message_id="a", offset=offset))
            calls += 1
            assert calls < 100
            pages.append(out)
            offset = out.structured_content["next_offset"]
        return pages

    def test_huge_body_paged_to_completion_reconstructs_exactly(self, fake_server, tmp_path):
        from src.tools.retrieval import _MESSAGE_BODY_PAGE_CHARS

        body = "".join(f"line {i:06d} of the synthetic body\n" for i in range(5000))
        handler = _handlers(fake_server, self._db(tmp_path, body))["get_message"]
        pages = self._pages(handler)
        assert "".join(p.structured_content["body"] for p in pages) == body
        assert len(pages) == -(-len(body) // _MESSAGE_BODY_PAGE_CHARS)
        offset = 0
        for p in pages:
            page = p.structured_content
            assert page["body_offset"] == offset
            assert page["body_total_chars"] == len(body)
            assert len(page["body"]) <= _MESSAGE_BODY_PAGE_CHARS
            # The prose carries the same page.
            assert page["body"] in _text(p)
            offset += len(page["body"])

    def test_default_page_is_bounded_with_next_offset(self, fake_server, tmp_path):
        body = "x" * 20_000 + "TAIL" + "y" * 29_996
        out = asyncio.run(
            _handlers(fake_server, self._db(tmp_path, body))["get_message"](message_id="a")
        )
        text = _text(out)
        assert "TAIL" not in text
        assert len(text) < 21_000
        assert out.structured_content["next_offset"] == 20_000
        assert out.structured_content["body_total_chars"] == 50_000
        assert "characters 1-20,000 of 50,000" in text
        assert "30,000 more characters: call get_message with offset=20000" in text

    def test_short_body_has_no_next_offset(self, fake_server, messages_db):
        out = asyncio.run(_handlers(fake_server, messages_db)["get_message"](message_id="m2"))
        assert out.structured_content["body"] == "thanks, budget noted"
        assert out.structured_content["next_offset"] is None
        assert "call get_message with offset" not in _text(out)

    def test_offset_at_end_returns_an_empty_page(self, fake_server, tmp_path):
        handler = _handlers(fake_server, self._db(tmp_path, "abc"))["get_message"]
        out = asyncio.run(handler(message_id="a", offset=3))
        assert out.structured_content["body"] == ""
        assert out.structured_content["next_offset"] is None
        assert "No body text past offset 3; the body has 3 characters." in _text(out)

    @pytest.mark.parametrize("offset", [-1, 4, 10**9])
    def test_invalid_offset_is_rejected_and_only_the_field_logged(
        self, fake_server, tmp_path, caplog, offset
    ):
        handler = _handlers(fake_server, self._db(tmp_path, "abc"))["get_message"]
        with caplog.at_level("DEBUG"):
            message = _error(handler(message_id="a", offset=offset))
        assert message.startswith("Error: offset")
        rejected = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
        assert rejected == ["rejected invalid argument: get_message.offset"]

    def test_offset_past_a_missing_body_is_rejected(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_message"]
        assert "past the end of the body (0 characters)" in _error(
            handler(message_id="t-alpha", offset=1)
        )

    @pytest.mark.parametrize(
        ("body", "keep"),
        [
            # A combining acute accent on the cut.
            ("a" * 19_999 + "e\u0301" + "b" * 100, 19_999),
            # A zero-width joiner on the cut.
            ("a" * 19_999 + "\U0001f469\u200d\U0001f4bb" + "b" * 100, 19_999),
            # A zero-width joiner just before the cut.
            ("a" * 19_998 + "\U0001f469\u200d\U0001f4bb" + "b" * 100, 19_998),
        ],
    )
    def test_page_boundary_does_not_split_a_combining_sequence(
        self, fake_server, tmp_path, body, keep
    ):
        handler = _handlers(fake_server, self._db(tmp_path, body))["get_message"]
        pages = self._pages(handler)
        assert pages[0].structured_content["next_offset"] == keep
        assert "".join(p.structured_content["body"] for p in pages) == body

    def test_a_long_run_of_marks_still_makes_progress(self, fake_server, tmp_path):
        body = "a" + "\u0301" * 50_000
        handler = _handlers(fake_server, self._db(tmp_path, body))["get_message"]
        pages = self._pages(handler)
        assert pages[0].structured_content["next_offset"] == 20_000
        assert "".join(p.structured_content["body"] for p in pages) == body


def _long_list(prefix: str) -> list[str]:
    # Twelve entries whose first ten join to well past HEADER_CHAR_LIMIT.
    return [f"{prefix}{i:02d}-{'x' * 60}@example.com" for i in range(12)]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_message", {"message_id": "a"}),
        ("get_thread", {"thread_id": "t"}),
        ("query_messages", {}),
    ],
)
def test_long_header_lists_keep_their_omitted_count(fake_server, tmp_path, tool, args):
    """Review round 2 (#489): the "+N more" note was appended before the
    500-character cut of the joined list, so a list of long entries lost
    its count. Every prose list (From / To / Cc, References) keeps it."""
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_message(
            conn,
            message_id="a",
            thread_id="t",
            sent_at="2024-01-01T00:00:00+00:00",
            from_=_long_list("f"),
            to=_long_list("t"),
            cc=_long_list("c"),
            references=_long_list("r"),
            body="hello",
        )
        conn.close()
        text = _text(asyncio.run(_handlers(fake_server, db)[tool](**args)))
    expected = 4 if tool != "query_messages" else 3  # query_messages lists no References
    assert text.count("(+2 more)") == expected
    assert len(text) < 10_000


def test_long_thread_participants_keep_their_omitted_count(fake_server, tmp_path):
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_thread(conn, thread_id="t", subject="s", participants=_long_list("p"))
        conn.close()
        text = _text(asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t")))
    assert "(+2 more)" in text


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
        text = _error(handler(folder="INBOX", filter_type="replied"))
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
        assert out.structured_content["contacts"][0]["organization"] == "example.com"

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
        contact = out.structured_content["contacts"][0]
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

    def test_states_how_many_addresses_a_name_matched(self, fake_server, namesakes_db, caplog):
        # #801: two people share the display name; the text says so and
        # nothing about the addresses reaches the log.
        handler = _handlers(fake_server, namesakes_db)["query_messages"]
        with caplog.at_level("DEBUG"):
            text = _text(asyncio.run(handler(sender="Avery Cole")))
        assert "total_matches: 3" in text
        assert "sender matched 2 distinct addresses" in text
        assert "possibly different people" in text
        for marker in ("Avery Cole", "one.example", "two.example"):
            assert marker not in caplog.text

    def test_single_matched_address_is_stated_without_a_warning(self, fake_server, namesakes_db):
        handler = _handlers(fake_server, namesakes_db)["query_messages"]
        text = _text(asyncio.run(handler(sender="avery@one.example")))
        assert "sender matched 1 distinct address" in text
        assert "possibly different people" not in text

    def test_renders_participants_by_role(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(asyncio.run(handler(subject="Re: Budget")))
        assert "From: bob@example.com" in text
        assert "To: Jane Doe <jane@example.com>" in text
        assert "Cc: carol@other.org" in text
        assert "2024-01-11T10:00:00+00:00 | INBOX | unread | attachments" in text

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

    def test_internal_date_basis_is_unavailable(self, fake_server, messages_db, caplog):
        # #1085: a legal value the index cannot serve yet; fixed text
        # naming #1092, raised before any retrieval work.
        handler = _handlers(fake_server, messages_db)["query_messages"]
        with caplog.at_level("DEBUG"):
            text = _error(handler(date_basis="internal"))
        assert "date_basis 'internal' is unavailable until #1092" in text
        assert "rejected invalid argument: query_messages.date_basis" in caplog.text

    def test_date_bounds_echo_the_basis(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        out = asyncio.run(handler(date_from="2024-01-01", date_basis="sent"))
        assert out.structured_content["date_bounds"] == {
            "date_from": "2024-01-01T00:00:00+00:00",
            "date_to": None,
            "basis": "sent",
        }
        text = _text(out)
        assert "Date bounds (UTC): from 2024-01-01T00:00:00+00:00" in text
        assert "date_basis: sent (bounds, order and cursor use sent_at)" in text
        # Without bounds the basis is still echoed, since it orders the page.
        out = asyncio.run(handler(date_basis="occurred"))
        assert out.structured_content["date_bounds"] == {
            "date_from": None,
            "date_to": None,
            "basis": "occurred",
        }
        assert (
            "date_basis: occurred (bounds, order and cursor use occurred_at; "
            "messages without a delivery time are left out)"
        ) in _text(out)

    def test_default_basis_leaves_the_response_unchanged(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        out = asyncio.run(handler())
        assert out.structured_content["date_bounds"] is None
        assert "date_basis" not in _text(out)
        out = asyncio.run(handler(date_from="2024-01-01"))
        assert out.structured_content["date_bounds"]["basis"] == "effective"
        assert "date_basis" not in _text(out)

    def test_replied_and_size_filters_are_described(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        out = asyncio.run(handler(replied=False, size_min=50, size_max=100))
        text = _text(out)
        assert "Query: replied=False, size_min=50, size_max=100" in text
        assert "total_matches: 5" in text  # every fixture row is 100 bytes and unreplied
        assert out.structured_content["filters"] == [
            {"filter": "replied", "value": False, "match": "equals"},
            {"filter": "size_min", "value": 50, "match": "inclusive_bound"},
            {"filter": "size_max", "value": 100, "match": "inclusive_bound"},
        ]
        assert "total_matches: 0" in _text(asyncio.run(handler(size_max=99)))
        assert "size_min must not be greater than size_max" in _error(
            handler(size_min=2, size_max=1)
        )

    def test_indeterminate_is_stated_whenever_non_zero(self, fake_server, tmp_path, caplog):
        import sqlite3

        import sqlite_vec
        from src.lib.sqlite import Database

        from tests.conftest import _build_schema, _insert_message

        path = tmp_path / "unknown.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        # u1 has a size and a delivery time; u2 has neither.
        _insert_message(
            conn,
            message_id="u1",
            thread_id="t-u",
            sent_at="2024-01-01T00:00:00+00:00",
            occurred_at="2024-01-01T01:00:00+00:00",
            from_=["a@example.com"],
            size_bytes=200,
        )
        _insert_message(
            conn,
            message_id="u2",
            thread_id="t-u",
            sent_at="2024-01-02T00:00:00+00:00",
            from_=["a@example.com"],
            size_bytes=None,
        )
        conn.close()
        handler = _handlers(fake_server, Database(str(path)))["query_messages"]
        with caplog.at_level("INFO", logger="mcp.timings"):
            out = asyncio.run(handler(size_min=100))
        assert out.structured_content["total_matches"] == 1
        assert out.structured_content["indeterminate"] == 1
        text = _text(out)
        assert "total_matches: 1\nindeterminate: 1 (messages the filters could neither" in text
        assert "in neither total_matches nor the pages)" in text
        # The timing line says so too (Codex round 3), so the log never
        # shows a call that could not decide every message as complete.
        timing = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert len(timing) == 1 and "outcome=ok" in timing[0]
        assert "'total_matches': 1, 'indeterminate': 1, 'returned': 1" in timing[0]
        # Zero is in the structured output but not stated in the prose.
        out = asyncio.run(handler(replied=False))
        assert out.structured_content["indeterminate"] == 0
        assert "indeterminate" not in _text(out)
        # An empty page is not stated as a definite "no match" while some
        # messages are undecided (Codex round 5): u2 might still match.
        out = asyncio.run(handler(size_min=10_000))
        assert (
            out.structured_content["total_matches"],
            out.structured_content["indeterminate"],
        ) == (0, 1)
        assert _text(out).endswith("No messages are known to match.")
        assert "No messages match." not in _text(out)
        # With nothing undecided, the empty page is a definite answer.
        out = asyncio.run(handler(size_min=10_000, replied=True))
        assert (
            out.structured_content["total_matches"],
            out.structured_content["indeterminate"],
        ) == (0, 0)
        assert _text(out).endswith("No messages match.")
        # Under the occurred basis, u2's missing delivery time is unknown too.
        out = asyncio.run(handler(date_basis="occurred"))
        assert (
            out.structured_content["total_matches"],
            out.structured_content["indeterminate"],
        ) == (
            1,
            1,
        )
        # A later page that comes back empty (its rows went between
        # calls) says so, whatever the indeterminate count.
        first = asyncio.run(handler(replied=False, limit=1))
        assert first.structured_content["has_more"] is True
        conn = sqlite3.connect(str(path))
        conn.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
        later = asyncio.run(
            handler(replied=False, limit=1, cursor=first.structured_content["next_cursor"])
        )
        assert later.structured_content["returned"] == 0
        assert _text(later).endswith("No further messages.")


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

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
import sqlite3
from contextlib import contextmanager

import pytest
import sqlite_vec
from mcp.server.fastmcp.exceptions import ToolError
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _build_schema, _insert_message, _insert_thread


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
        assert '[1,000 more characters not shown; get_message("a") returns the full body]' in text

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
            return [{"email": "p@example.test", "names": names, "thread_count": 30}]

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

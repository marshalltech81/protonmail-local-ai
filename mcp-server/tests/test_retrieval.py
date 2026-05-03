"""
Tests for src/tools/retrieval.py.

Retrieval tools are how the LLM follows up on a search hit (or addresses
a thread by id directly). The handlers are thin formatters around the
read-only Database, so the tests focus on:

- the body_text vs snippet fallback used when the indexer has not yet
  populated body_text on legacy threads;
- the attachment-metadata gating;
- not-found paths returning the documented sentinel message rather than
  raising;
- limit/offset clamping on list_threads (an LLM-supplied ``limit=99999``
  must not turn into an unbounded scan);
- formatting contract that downstream tools depend on (Thread ID line,
  Message-IDs list, folder count line in list_folders).
"""

import asyncio

from src.tools.retrieval import register_retrieval_tools


def _handlers(fake_server, db):
    register_retrieval_tools(fake_server, db)
    return fake_server.tools


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


class TestGetThread:
    def test_known_thread_renders_metadata_and_body(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-alpha"))
        text = _text(out)
        assert "invoice for march" in text
        assert "INBOX" in text
        assert "alice@example.com" in text
        assert "please find the invoice attached" in text
        # Local-only mode banner must always appear so the LLM does not
        # claim live Bridge retrieval happened.
        assert "local SQLite index only" in text

    def test_unknown_thread_returns_not_found_sentinel(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="t-does-not-exist"))
        assert "Thread not found" in _text(out)

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
        def boom(_thread_id):
            raise RuntimeError("simulated read failure")

        seeded_db.get_thread = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["get_thread"]
        out = asyncio.run(handler(thread_id="anything"))
        assert "Error" in _text(out)


class TestGetMessage:
    def test_known_message_returns_thread_context(self, fake_server, seeded_db):
        # seeded_db inserts message_thread_map(message_id=thread_id) so
        # asking for "t-alpha" round-trips through find_thread_by_message_id.
        handler = _handlers(fake_server, seeded_db)["get_message"]
        out = asyncio.run(handler(message_id="t-alpha"))
        text = _text(out)
        assert "invoice for march" in text
        assert "Message-ID: t-alpha" in text
        assert "local SQLite index only" in text

    def test_unknown_message_returns_not_found_sentinel(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_message"]
        out = asyncio.run(handler(message_id="never-existed"))
        assert "Message not found" in _text(out)


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
        out = asyncio.run(handler(folder="INBOX"))
        assert "Error" in _text(out)

    def test_unsupported_filter_type_returns_error_text(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["list_threads"]
        out = asyncio.run(handler(folder="INBOX", filter_type="unread"))
        text = _text(out)
        assert "Error" in text
        assert "filter_type" in text


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
        out = asyncio.run(handler())
        assert "Error" in _text(out)


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

    def test_empty_query_returns_guidance_message(self, fake_server, seeded_db):
        # An empty string would otherwise hit the DB as a no-op aggregation;
        # the tool must short-circuit with a guidance message so the LLM
        # gets a clear signal rather than an empty list.
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        out = asyncio.run(handler(query=""))
        assert "Provide a name" in _text(out)

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

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom(_query, _limit):
            raise RuntimeError("simulated read failure")

        seeded_db.find_contact = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["find_contact"]
        out = asyncio.run(handler(query="alice"))
        assert "Error" in _text(out)

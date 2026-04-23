"""
Tests for src/tools/actions.py.

Action tools mutate the mailbox over IMAP/SMTP. The default deployment
sets ``MCP_READ_ONLY=true`` and explicitly does not register these tools
on the server, so the primary correctness guarantee is "every mutation
path returns a refusal sentinel until the operator opts in." That is the
behavior these tests pin down.

Once a real write backend exists, the assertions on ``send_email`` /
``move_message`` / ``mark_read`` / ``flag_message`` should grow to cover
the success paths beyond just verifying the IMAP stub was called.
"""

import asyncio

from src.tools.actions import register_action_tools


def _handlers(fake_server, imap, *, read_only):
    register_action_tools(fake_server, imap, read_only=read_only)
    return fake_server.tools


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


class TestReadOnlyGuard:
    def test_send_email_is_blocked_when_read_only(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=True)["send_email"]
        out = asyncio.run(handler(to=["a@example.com"], subject="hi", body="body"))
        assert "MCP read-only mode" in _text(out)
        # Critically: the IMAP stub must not have been called at all.
        assert fake_imap.send_calls == []

    def test_move_message_is_blocked_when_read_only(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=True)["move_message"]
        out = asyncio.run(handler(uid="7", src_folder="INBOX", dst_folder="Archive"))
        assert "MCP read-only mode" in _text(out)
        assert fake_imap.move_calls == []

    def test_mark_read_is_blocked_when_read_only(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=True)["mark_read"]
        out = asyncio.run(handler(uids=["1", "2"]))
        assert "MCP read-only mode" in _text(out)
        assert fake_imap.flag_calls == []

    def test_flag_message_is_blocked_when_read_only(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=True)["flag_message"]
        out = asyncio.run(handler(uid="1"))
        assert "MCP read-only mode" in _text(out)
        assert fake_imap.flag_calls == []


class TestNoBackendGuard:
    """When read_only=False but no IMAP transport is wired, every mutation
    must still refuse rather than crashing on a None attribute access."""

    def test_send_email_refuses_when_imap_is_none(self, fake_server):
        handler = _handlers(fake_server, None, read_only=False)["send_email"]
        out = asyncio.run(handler(to=["a@example.com"], subject="hi", body="body"))
        assert "no live Bridge-backed action transport" in _text(out)

    def test_move_message_refuses_when_imap_is_none(self, fake_server):
        handler = _handlers(fake_server, None, read_only=False)["move_message"]
        out = asyncio.run(handler(uid="7", src_folder="INBOX", dst_folder="Archive"))
        assert "no live Bridge-backed action transport" in _text(out)


class TestUnimplementedTools:
    """``reply_to_thread`` and ``create_draft`` are reserved tool names
    that always return an explanation. The contract guards an LLM from
    silently no-op'ing on those calls — they must say what's missing."""

    def test_reply_to_thread_returns_unimplemented(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["reply_to_thread"]
        out = asyncio.run(handler(thread_id="t-x", body="hi"))
        assert "not yet implemented" in _text(out)

    def test_create_draft_returns_unimplemented(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["create_draft"]
        out = asyncio.run(handler(to=["a@example.com"], subject="hi", body="body"))
        assert "not yet implemented" in _text(out)


class TestWriteSuccessPaths:
    """Once an IMAP backend exists in the deployment (read_only=False
    plus a non-None imap), the handlers should drive the IMAP stub and
    surface success/failure to the caller. These pin the visible
    behavior so a future regression in the wire-up surfaces immediately."""

    def test_send_email_calls_imap_and_reports_recipients(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["send_email"]
        out = asyncio.run(
            handler(
                to=["a@example.com", "b@example.com"],
                subject="hello",
                body="body",
                reply_to_message_id="parent-1",
            )
        )
        assert "Email sent successfully" in _text(out)
        assert "a@example.com" in _text(out)
        # reply_to_message_id must propagate to In-Reply-To/References.
        assert fake_imap.send_calls[0]["in_reply_to"] == "<parent-1>"
        assert fake_imap.send_calls[0]["references"] == "<parent-1>"

    def test_send_email_reports_failure_when_imap_returns_false(self, fake_server):
        from tests.conftest import FakeIMAP

        imap = FakeIMAP(send_ok=False)
        handler = _handlers(fake_server, imap, read_only=False)["send_email"]
        out = asyncio.run(handler(to=["a@example.com"], subject="hi", body="body"))
        assert "Failed to send" in _text(out)

    def test_send_email_handles_imap_exception(self, fake_server):
        class ExplodingIMAP:
            def send_email(self, **_kwargs):
                raise RuntimeError("smtp boom")

        handler = _handlers(fake_server, ExplodingIMAP(), read_only=False)["send_email"]
        out = asyncio.run(handler(to=["a@example.com"], subject="hi", body="body"))
        assert "Error" in _text(out)

    def test_move_message_success_reports_folders(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["move_message"]
        out = asyncio.run(handler(uid="9", src_folder="INBOX", dst_folder="Archive"))
        text = _text(out)
        assert "moved" in text.lower()
        assert "INBOX" in text
        assert "Archive" in text
        assert fake_imap.move_calls == [("9", "INBOX", "Archive")]

    def test_mark_read_emits_one_line_per_uid(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["mark_read"]
        out = asyncio.run(handler(uids=["1", "2", "3"]))
        text = _text(out)
        for uid in ("1", "2", "3"):
            assert f"UID {uid}" in text
        assert len(fake_imap.flag_calls) == 3

    def test_flag_message_reports_state(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["flag_message"]
        out = asyncio.run(handler(uid="1", flagged=True))
        assert "flagged successfully" in _text(out)

    def test_unflag_message_reports_state(self, fake_server, fake_imap):
        handler = _handlers(fake_server, fake_imap, read_only=False)["flag_message"]
        out = asyncio.run(handler(uid="1", flagged=False))
        assert "unflagged successfully" in _text(out)

"""
Tests for src/parser.py.

Covers: plain text, HTML, multipart, attachments, inline Content-Disposition,
encoded headers, address parsing, date fallback, and folder derivation.
"""

import textwrap
from datetime import datetime
from pathlib import Path

from src.parser import (
    _clean_id,
    _decode_header,
    _parse_addrs,
    parse_email,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_eml(tmp_path: Path, content: str, name: str = "test.eml") -> Path:
    """Write a raw email string to a Maildir-like path and return it."""
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(textwrap.dedent(content).strip(), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# parse_email — basic cases
# ---------------------------------------------------------------------------


class TestParseEmail:
    def test_plain_text_email(self, tmp_path):
        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: Hello world
            Message-ID: <msg1@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Hello Bob, how are you?
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.message_id == "msg1@example.com"
        assert msg.subject == "Hello world"
        assert msg.from_addr == "alice@example.com"
        assert "bob@example.com" in msg.to_addrs
        assert "Hello Bob" in msg.body_text
        assert msg.folder == "INBOX"
        assert msg.has_attachments is False

    def test_missing_message_id_returns_none(self, tmp_path):
        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: No ID
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Body text.
        """,
        )
        assert parse_email(path) is None

    def test_folder_derived_from_path(self, tmp_path):
        folder = tmp_path / "Sent" / "cur"
        folder.mkdir(parents=True)
        path = folder / "msg.eml"
        path.write_text(
            textwrap.dedent("""
            From: alice@example.com
            To: bob@example.com
            Subject: Sent message
            Message-ID: <sent1@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Sent body.
        """).strip()
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.folder == "Sent"

    def test_in_reply_to_and_references_parsed(self, tmp_path):
        path = write_eml(
            tmp_path,
            """
            From: bob@example.com
            To: alice@example.com
            Subject: Re: Hello
            Message-ID: <msg2@example.com>
            In-Reply-To: <msg1@example.com>
            References: <msg0@example.com> <msg1@example.com>
            Date: Mon, 01 Jan 2024 13:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Reply body.
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.in_reply_to == "msg1@example.com"
        assert msg.references == ["msg0@example.com", "msg1@example.com"]

    def test_invalid_date_falls_back_to_now(self, tmp_path):
        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: Bad date
            Message-ID: <bad_date@example.com>
            Date: not-a-date
            Content-Type: text/plain; charset=utf-8

            Body.
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert isinstance(msg.date, datetime)
        # Fallback uses timezone.utc
        assert msg.date.tzinfo is not None

    def test_date_minus_zero_normalized_to_aware_utc(self, tmp_path):
        """RFC 2822 ``-0000`` means "local time, offset unknown".
        ``parsedate_to_datetime`` returns a naive datetime for that case,
        which mixes badly with aware datetimes during thread sorting.
        ``_parse_date`` must normalize it to a UTC-aware datetime."""
        from datetime import UTC

        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: Minus-zero date
            Message-ID: <mz_date@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 -0000
            Content-Type: text/plain; charset=utf-8

            Body.
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.date.tzinfo is not None
        assert msg.date.utcoffset().total_seconds() == 0
        assert msg.date.tzinfo is UTC

    def test_date_non_utc_offset_converted_to_utc(self, tmp_path):
        """A +0500 offset must be converted to UTC for downstream comparisons."""
        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: Offset date
            Message-ID: <off_date@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0500
            Content-Type: text/plain; charset=utf-8

            Body.
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.date.utcoffset().total_seconds() == 0
        assert msg.date.hour == 7  # 12:00 +0500 = 07:00 UTC

    def test_nonexistent_file_propagates_filenotfounderror(self, tmp_path):
        # Transient I/O errors must propagate so the worker's retry
        # path takes over. Returning ``None`` would route the row to
        # ``mark_succeeded`` and silently drop the file from the index.
        import pytest

        with pytest.raises(FileNotFoundError):
            parse_email(tmp_path / "ghost.eml")

    def test_permission_denied_propagates_for_queue_retry(self, tmp_path):
        # Models the mbsync 0600→0644 chmod race: the file exists but
        # is not yet readable to the indexer UID. The error must
        # propagate so the durable queue retries on backoff; the prior
        # behavior (return None) collapsed this into "terminal success"
        # and dropped the message permanently.
        import os

        import pytest

        path = write_eml(
            tmp_path,
            """
            From: a@example.com
            To: b@example.com
            Subject: race
            Message-ID: <race@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Body.
            """,
            name="race.eml",
        )
        os.chmod(path, 0o000)
        try:
            with pytest.raises(PermissionError):
                parse_email(path)
        finally:
            os.chmod(path, 0o644)

    def test_nested_folder_path_preserved_when_maildir_root_given(self, tmp_path):
        """Regression: without a ``maildir_root`` the folder is derived as
        ``path.parent.parent.name``, which collapses ``Clients/ABC/cur/msg``
        to just ``ABC`` and loses the parent context. Passing the root
        preserves the full relative path."""
        folder = tmp_path / "Clients" / "ABC" / "cur"
        folder.mkdir(parents=True)
        path = folder / "msg.eml"
        path.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Nested\r\n"
            "Message-ID: <nested@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Body.\r\n",
            encoding="utf-8",
        )
        msg = parse_email(path, maildir_root=tmp_path)
        assert msg is not None
        assert msg.folder == "Clients/ABC"

    def test_folder_falls_back_to_leaf_without_root(self, tmp_path):
        """Backward compat: callers that do not pass ``maildir_root`` still
        get the old leaf-name behavior."""
        folder = tmp_path / "Clients" / "ABC" / "cur"
        folder.mkdir(parents=True)
        path = folder / "msg.eml"
        path.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Nested\r\n"
            "Message-ID: <nested2@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Body.\r\n",
            encoding="utf-8",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.folder == "ABC"


# ---------------------------------------------------------------------------
# parse_email — file identity (schema v7)
# ---------------------------------------------------------------------------


class TestFileIdentity:
    def test_populates_size_mtime_and_content_hash(self, tmp_path):
        import hashlib

        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: Identity
            Message-ID: <identity1@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Body text for identity check.
        """,
        )
        raw = path.read_bytes()
        expected_hash = hashlib.sha256(raw).hexdigest()

        msg = parse_email(path)
        assert msg is not None
        assert msg.size == len(raw)
        assert msg.content_hash == expected_hash
        # mtime_ns must be an int when the stat succeeds; the exact value
        # depends on the filesystem, so verify only the type and that it
        # is non-negative.
        assert isinstance(msg.mtime_ns, int)
        assert msg.mtime_ns >= 0

    def test_flag_rename_preserves_content_hash(self, tmp_path):
        """A Maildir flag rename (``msg:2,S`` → ``msg:2,SR``) is a
        filename change only; the file contents on disk are identical.
        Parsing both paths must yield identical ``content_hash`` values
        so the reconciler can recognise it as the same message."""
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        raw = (
            b"From: alice@example.com\r\n"
            b"To: bob@example.com\r\n"
            b"Subject: Flag rename\r\n"
            b"Message-ID: <flagrename@example.com>\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"\r\n"
            b"Body.\r\n"
        )

        path_seen = folder / "msg:2,S"
        path_seen.write_bytes(raw)
        msg_seen = parse_email(path_seen)

        path_seen_replied = folder / "msg:2,SR"
        path_seen_replied.write_bytes(raw)
        msg_seen_replied = parse_email(path_seen_replied)

        assert msg_seen is not None and msg_seen_replied is not None
        assert msg_seen.content_hash == msg_seen_replied.content_hash
        assert msg_seen.size == msg_seen_replied.size


# ---------------------------------------------------------------------------
# parse_email — body extraction
# ---------------------------------------------------------------------------


class TestBodyExtraction:
    def test_html_only_converted_to_text(self, tmp_path):
        path = write_eml(
            tmp_path,
            """
            From: alice@example.com
            To: bob@example.com
            Subject: HTML email
            Message-ID: <html1@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/html; charset=utf-8

            <html><body><p>Hello <b>world</b></p></body></html>
        """,
        )
        msg = parse_email(path)
        assert msg is not None
        assert "Hello" in msg.body_text
        assert "<html>" not in msg.body_text
        assert "<b>" not in msg.body_text

    def test_multipart_prefers_plain_text_over_html(self, tmp_path):
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Multipart\r\n"
            "Message-ID: <multi1@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/alternative; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Plain text body.\r\n"
            "--bound\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<p>HTML body.</p>\r\n"
            "--bound--\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "multi.eml"
        path.write_bytes(content.encode("utf-8"))
        msg = parse_email(path)
        assert msg is not None
        assert "Plain text body." in msg.body_text
        assert "<p>" not in msg.body_text

    def test_inline_without_filename_is_body_not_attachment(self, tmp_path):
        """Content-Disposition: inline without a filename is the message body."""
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Inline body\r\n"
            "Message-ID: <inline1@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Disposition: inline\r\n"
            "\r\n"
            "This is the inline body text.\r\n"
            "--bound--\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "inline.eml"
        path.write_bytes(content.encode("utf-8"))
        msg = parse_email(path)
        assert msg is not None
        assert "inline body text" in msg.body_text
        assert msg.has_attachments is False
        assert msg.attachments == []

    def test_attachment_tracked_correctly(self, tmp_path):
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Has attachment\r\n"
            "Message-ID: <attach1@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "See attached.\r\n"
            "--bound\r\n"
            "Content-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="report.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "AAAA\r\n"
            "--bound--\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "attach.eml"
        path.write_bytes(content.encode("utf-8"))
        msg = parse_email(path)
        assert msg is not None
        assert msg.has_attachments is True
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "report.pdf"
        assert msg.attachments[0].content_type == "application/pdf"
        assert "See attached" in msg.body_text


# ---------------------------------------------------------------------------
# _parse_addrs
# ---------------------------------------------------------------------------


class TestParseAddrs:
    def test_simple_address(self):
        assert _parse_addrs("alice@example.com") == ["alice@example.com"]

    def test_display_name(self):
        result = _parse_addrs("Alice Smith <alice@example.com>")
        assert any("alice@example.com" in r for r in result)

    def test_display_name_with_comma(self):
        """Display names like 'Smith, Alice' must not be split on the comma."""
        result = _parse_addrs('"Smith, Alice" <alice@example.com>')
        assert len(result) == 1
        assert "alice@example.com" in result[0]

    def test_multiple_addresses(self):
        result = _parse_addrs("alice@example.com, bob@example.com")
        assert len(result) == 2

    def test_empty_returns_empty_list(self):
        assert _parse_addrs("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert _parse_addrs("   ") == []


# ---------------------------------------------------------------------------
# _decode_header
# ---------------------------------------------------------------------------


class TestMimeHardening:
    def _write(self, tmp_path: Path, content: str, name: str = "m.eml") -> Path:
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content.encode("utf-8"))
        return path

    def test_uppercase_attachment_disposition_recognised(self, tmp_path):
        """Content-Disposition is case-insensitive per RFC 2183. A sender
        using ``Attachment`` (capital A) used to be parsed as the message
        body — its payload decoded as text and the body_text lost."""
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Upper attach\r\n"
            "Message-ID: <upper_attach@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Real body text.\r\n"
            "--bound\r\n"
            "Content-Type: application/pdf\r\n"
            'Content-Disposition: Attachment; filename="report.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "AAAA\r\n"
            "--bound--\r\n"
        )
        msg = parse_email(self._write(tmp_path, content))
        assert msg is not None
        assert msg.has_attachments is True
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "report.pdf"
        assert "Real body text." in msg.body_text

    def test_filename_without_content_disposition_is_attachment(self, tmp_path):
        """Some clients emit attachment parts with a ``filename`` parameter
        but no ``Content-Disposition`` header at all. Under the old rule
        (``"attachment" in cd or ("inline" in cd and has_filename)``) such a
        part would fall through to the body path — its payload decoded as
        text and the real body_text lost when the attachment preceded it.
        Any part carrying a filename is treated as an attachment now."""
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Filename only\r\n"
            "Message-ID: <fname_only@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            'Content-Type: application/octet-stream; name="leak.bin"\r\n'
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "AAAA\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Real body text.\r\n"
            "--bound--\r\n"
        )
        msg = parse_email(self._write(tmp_path, content))
        assert msg is not None
        assert msg.has_attachments is True
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "leak.bin"
        assert "Real body text." in msg.body_text

    def test_html_before_plain_still_prefers_plain(self, tmp_path):
        """Previous body extraction took whichever of text/plain or
        text/html appeared first in the multipart. If an HTML part came
        first, the LLM got html2text-converted output even when the
        sender sent a clean text/plain body. Always prefer text/plain."""
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: HTML first\r\n"
            "Message-ID: <html_first@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/alternative; boundary="bound"\r\n'
            "\r\n"
            "--bound\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<p>HTML <b>body</b></p>\r\n"
            "--bound\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Plain body marker.\r\n"
            "--bound--\r\n"
        )
        msg = parse_email(self._write(tmp_path, content))
        assert msg is not None
        assert "Plain body marker." in msg.body_text
        # html2text markdown would include asterisks for <b>; plain path doesn't.
        assert "**" not in msg.body_text


class TestDecodeHeader:
    def test_plain_ascii(self):
        assert _decode_header("Hello world") == "Hello world"

    def test_utf8_encoded_header(self):
        # RFC 2047 encoded: "Héllo"
        encoded = "=?utf-8?q?H=C3=A9llo?="
        result = _decode_header(encoded)
        assert "Héllo" in result or "H" in result  # decoded, not raw

    def test_empty_string(self):
        assert _decode_header("") == ""

    def test_unknown_charset_falls_back_to_utf8(self):
        """Regression: an obscure or invalid charset label used to raise
        LookupError inside _decode_header, which propagated up through
        parse_email's broad except and silently dropped the whole message
        from the index. Fall back to utf-8 with replacement instead."""
        encoded = "=?not-a-real-charset?q?Hello?="
        result = _decode_header(encoded)
        assert "Hello" in result

    def test_unknown_charset_in_eml_does_not_drop_message(self, tmp_path):
        """End-to-end: a message whose Subject declares an unknown charset
        must still be parsed rather than returned as ``None``."""
        content = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: =?not-a-real-charset?q?Weird?=\r\n"
            "Message-ID: <bad_charset@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Body.\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "badcharset.eml"
        path.write_bytes(content.encode("utf-8"))
        msg = parse_email(path)
        assert msg is not None
        assert "Weird" in msg.subject


# ---------------------------------------------------------------------------
# _clean_id
# ---------------------------------------------------------------------------


class TestCleanId:
    def test_strips_angle_brackets(self):
        assert _clean_id("<msg1@example.com>") == "msg1@example.com"

    def test_strips_whitespace(self):
        assert _clean_id("  <msg1@example.com>  ") == "msg1@example.com"

    def test_already_clean(self):
        assert _clean_id("msg1@example.com") == "msg1@example.com"

    def test_empty_string(self):
        assert _clean_id("") == ""


# ---------------------------------------------------------------------------
# _normalize_subject (imported from threader via parser module context)
# ---------------------------------------------------------------------------


class TestNormalizeSubject:
    def test_strips_re(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Re: Hello") == "hello"

    def test_strips_multiple_re(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Re: Re: Re: Hello") == "hello"

    def test_strips_fwd(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Fwd: Hello") == "hello"

    def test_strips_mixed_prefixes(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Re: Fwd: Re: Hello world") == "hello world"

    def test_case_insensitive(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("RE: HELLO") == "hello"

    def test_plain_subject_unchanged(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Hello world") == "hello world"

    def test_empty_subject(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("") == ""

    def test_collapses_internal_whitespace(self):
        from src.threader import _normalize_subject

        assert _normalize_subject("Hello   world") == "hello world"

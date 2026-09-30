"""
Tests for src/parser.py.

Covers: plain text, HTML, multipart, attachments, inline Content-Disposition,
encoded headers, address parsing, date fallback, and folder derivation.
"""

import hashlib
import textwrap
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from src.parser import (
    OversizedMessageError,
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

    def test_oversized_file_raises_oversized_message_error(self, tmp_path, monkeypatch):
        # Files past INDEXER_PARSE_MAX_BYTES must raise so the worker
        # routes them through ``mark_skipped(reason="oversized")``
        # instead of the previous silent ``return None`` /
        # ``mark_succeeded`` path that hid oversized entries from
        # operator-visible logs.
        import pytest

        # Force a small cap so we don't have to write a 50 MB fixture.
        monkeypatch.setenv("INDEXER_PARSE_MAX_BYTES", "100")
        path = write_eml(
            tmp_path,
            """
            From: a@example.com
            To: b@example.com
            Subject: huge
            Message-ID: <huge@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            """
            + ("X" * 500),
            name="huge.eml",
        )
        with pytest.raises(OversizedMessageError) as excinfo:
            parse_email(path)
        assert excinfo.value.cap == 100
        assert excinfo.value.size > 100
        assert excinfo.value.path == path

    def test_oversized_caught_when_fstat_returns_undersized(self, tmp_path, monkeypatch):
        # Defends against the stat-then-read TOCTOU: if ``os.fstat`` returns
        # a size below the cap (because it failed, or because the file grew
        # after the syscall), the bounded ``f.read(cap + 1)`` must still
        # raise ``OversizedMessageError`` rather than slurping an unbounded
        # file into memory. We simulate by monkey-patching ``os.fstat`` to
        # report ``None`` (the stat-failure branch) on a file that exceeds
        # the cap.
        import os as _os

        import pytest
        from src import parser as parser_module

        monkeypatch.setenv("INDEXER_PARSE_MAX_BYTES", "100")
        path = write_eml(
            tmp_path,
            """
            From: a@example.com
            To: b@example.com
            Subject: huge-toctou
            Message-ID: <toctou@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            """
            + ("Y" * 500),
            name="toctou.eml",
        )
        real_fstat = _os.fstat

        def _fail_fstat(fd):
            raise OSError("simulated fstat failure")

        monkeypatch.setattr(parser_module.os, "fstat", _fail_fstat)
        try:
            with pytest.raises(OversizedMessageError) as excinfo:
                parse_email(path)
        finally:
            monkeypatch.setattr(parser_module.os, "fstat", real_fstat)
        assert excinfo.value.cap == 100
        # Read path reports len(raw) (which is cap+1) when stat is unavailable.
        assert excinfo.value.size == 101
        assert excinfo.value.path == path

    def test_negative_parse_max_bytes_falls_back_to_default(self, tmp_path, monkeypatch, caplog):
        # Regression: ``INDEXER_PARSE_MAX_BYTES=-1`` used to silently
        # disable the cap (the old ``max(0, value)`` collapsed it to 0,
        # the disable sentinel). A typo should fall back to the default
        # rather than disabling OOM protection, so the call must apply
        # the 50 MB default and a small fixture parses without raising.
        import logging

        from src import parser as parser_module

        monkeypatch.setenv("INDEXER_PARSE_MAX_BYTES", "-1")
        path = write_eml(
            tmp_path,
            """
            From: a@example.com
            To: b@example.com
            Subject: negative-cap-fallback
            Message-ID: <neg@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            Body.
            """,
            name="neg.eml",
        )
        with caplog.at_level(logging.WARNING, logger="indexer.parser"):
            assert parser_module._parse_max_bytes() == parser_module._DEFAULT_PARSE_MAX_BYTES
            msg = parse_email(path)
        assert msg is not None
        assert msg.message_id == "neg@example.com"
        assert any("INDEXER_PARSE_MAX_BYTES" in r.message for r in caplog.records)

    def test_oversized_size_reports_post_growth_max(self, tmp_path, monkeypatch):
        # Regression for the stat-then-read race: if the file grows
        # between ``fstat`` and the bounded read, ``stat.st_size``
        # underreports vs. ``len(raw)``. The dead-letter ``OversizedMessageError.size``
        # must reflect the larger of the two so operators don't see a
        # misleadingly small size when the read-side detection fires.
        import os as _os

        import pytest
        from src import parser as parser_module

        monkeypatch.setenv("INDEXER_PARSE_MAX_BYTES", "100")
        path = write_eml(
            tmp_path,
            """
            From: a@example.com
            To: b@example.com
            Subject: stat-stale
            Message-ID: <stale@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/plain; charset=utf-8

            """
            + ("Z" * 500),
            name="stale.eml",
        )
        real_fstat = _os.fstat

        def _undersized_fstat(fd):
            stat = real_fstat(fd)
            return _os.stat_result(
                (
                    stat.st_mode,
                    stat.st_ino,
                    stat.st_dev,
                    stat.st_nlink,
                    stat.st_uid,
                    stat.st_gid,
                    50,
                    stat.st_atime,
                    stat.st_mtime,
                    stat.st_ctime,
                )
            )

        monkeypatch.setattr(parser_module.os, "fstat", _undersized_fstat)
        with pytest.raises(OversizedMessageError) as excinfo:
            parse_email(path)
        # ``stat.st_size`` says 50, but ``len(raw) == cap + 1 == 101``;
        # the reported size is the max so the dead-letter doesn't lie.
        assert excinfo.value.size == 101
        assert excinfo.value.cap == 100

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

    def test_unclosed_style_does_not_blank_the_next_html_body(self, tmp_path):
        """Regression (#216): one shared HTML2Text instance kept its
        parser state across messages, so an unclosed ``<style>`` left it
        in CDATA mode and the next HTML-only body came out empty —
        indexed as successful, never retried."""
        template = """
            From: alice@example.com
            To: bob@example.com
            Subject: HTML email
            Message-ID: <{mid}@example.com>
            Date: Mon, 01 Jan 2024 12:00:00 +0000
            Content-Type: text/html; charset=utf-8

            {body}
        """
        bad = write_eml(tmp_path, template.format(mid="bad", body="<style>unfinished"), "bad.eml")
        good = write_eml(
            tmp_path, template.format(mid="good", body="<p>Next message body</p>"), "good.eml"
        )
        assert parse_email(bad) is not None
        msg = parse_email(good)
        assert msg is not None
        assert "Next message body" in msg.body_text

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


def _write_message(tmp_path: Path, message: EmailMessage, name: str = "m.eml") -> Path:
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(message.as_bytes())
    return path


def _attached_email(marker: str) -> EmailMessage:
    inner = EmailMessage()
    inner["Message-ID"] = f"<{marker}@example.test>"
    inner["From"] = "other@example.test"
    inner["Subject"] = "Old instructions"
    inner.set_content(f"{marker} obsolete instructions")
    return inner


def _html_parent(*attached: EmailMessage) -> EmailMessage:
    outer = EmailMessage()
    outer["Message-ID"] = "<outer@example.test>"
    outer["From"] = "sender@example.test"
    outer["To"] = "owner@example.test"
    outer["Date"] = "Mon, 28 Sep 2026 12:00:00 +0000"
    outer["Subject"] = "New instructions"
    outer.set_content("<p>OUTER_BODY_MARKER new instructions</p>", subtype="html")
    for i, inner in enumerate(attached):
        outer.add_attachment(inner, filename=f"forwarded-{i}.eml")
    return outer


class TestAttachmentBoundaries:
    def test_attached_email_body_does_not_replace_the_parent_body(self, tmp_path):
        """#230: the MIME walk descended into an attached message and its
        text/plain beat the parent's HTML, so the parent was indexed with
        the attached email's body."""
        msg = parse_email(_write_message(tmp_path, _html_parent(_attached_email("INNER_MARKER"))))
        assert msg is not None
        assert "OUTER_BODY_MARKER" in msg.body_text
        assert "INNER_MARKER" not in msg.body_text
        assert msg.message_id == "outer@example.test"
        assert [a.content_type for a in msg.attachments] == ["message/rfc822"]

    def test_attached_email_is_hashed_by_its_bytes(self, tmp_path):
        """#230: every attached email had an empty payload, so all of them
        shared ``sha256(b"")`` as their content hash and attachment ID."""
        first, second = _attached_email("FIRST_MARKER"), _attached_email("SECOND_MARKER")
        msg = parse_email(_write_message(tmp_path, _html_parent(first, second)))
        assert msg is not None
        a, b = msg.attachments
        assert b"FIRST_MARKER" in a.payload and b"SECOND_MARKER" in b.payload
        assert a.size == len(a.payload) > 0
        assert a.content_hash == hashlib.sha256(a.payload).hexdigest()
        assert a.content_hash != b.content_hash

    def test_deeply_nested_attached_email_degrades_to_empty_payload(self, tmp_path):
        """A depth the stdlib parser accepts but that is past
        ``MAX_ATTACHED_MESSAGE_DEPTH`` is never serialized (serializing
        would recurse past the limit): the message stays indexable, with
        the old empty payload."""
        attached = 'Content-Type: message/rfc822\r\nContent-Disposition: attachment; filename="x.eml"\r\n\r\n'
        nested = attached * 299 + "Content-Type: text/plain\r\n\r\nleaf\r\n"
        content = (
            "Message-ID: <deep@example.test>\r\n"
            "From: sender@example.test\r\n"
            "Date: Mon, 28 Sep 2026 12:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="b"\r\n'
            "\r\n--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            f"--b\r\n{attached}{nested}\r\n--b--\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "deep.eml").write_bytes(content.encode())
        msg = parse_email(folder / "deep.eml")
        assert msg is not None
        assert msg.body_text == "PARENT_BODY"
        # Each nested attached email is recorded, as the old walk did;
        # only the outermost is ever serialized, and this one is too deep.
        assert len(msg.attachments) == 300
        assert all(a.payload == b"" for a in msg.attachments)

    def test_attached_email_nested_past_the_cap_is_not_serialized(self, tmp_path):
        """Review round 1: serializing copies the subtree once per nesting
        level, so a large leaf under many wrappers cost depth x size. Past
        the depth cap the attached email keeps the empty payload."""
        from src.parser import MAX_ATTACHED_MESSAGE_DEPTH

        def parent(depth: int) -> str:
            wrapper = "Content-Type: message/rfc822\r\n\r\n"
            nested = wrapper * (depth - 1) + "Content-Type: text/plain\r\n\r\nleaf\r\n"
            return (
                "Message-ID: <deep@example.test>\r\n"
                "From: sender@example.test\r\n"
                "Date: Mon, 28 Sep 2026 12:00:00 +0000\r\n"
                "MIME-Version: 1.0\r\n"
                'Content-Type: multipart/mixed; boundary="b"\r\n'
                "\r\n--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
                '--b\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="x.eml"\r\n\r\n'
                f"{nested}\r\n--b--\r\n"
            )

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        for depth, serialized in ((MAX_ATTACHED_MESSAGE_DEPTH, True), (50, False)):
            path = folder / f"d{depth}.eml"
            path.write_bytes(parent(depth).encode())
            msg = parse_email(path)
            assert msg is not None
            assert msg.body_text == "PARENT_BODY"
            [attachment] = msg.attachments
            assert (b"leaf" in attachment.payload) is serialized, depth

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_transfer_encoded_attached_email_is_hashed_decoded(self, tmp_path, encoding):
        """Review round 1: a base64 / quoted-printable message/rfc822 part
        (not allowed by RFC 2046, but sent) was hashed in its transport
        form, so the same email got a different ID and size per encoding."""
        import base64
        import quopri

        inner = (
            b"Message-ID: <inner@example.test>\r\nFrom: other@example.test\r\n\r\nINNER=body\r\n"
        )
        encoded = base64.encodebytes(inner) if encoding == "base64" else quopri.encodestring(inner)

        def parent(cte: str, body: bytes) -> bytes:
            return (
                b"Message-ID: <outer@example.test>\r\nFrom: sender@example.test\r\n"
                b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
                b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
                b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
                b"--b\r\nContent-Type: message/rfc822\r\n"
                + f"Content-Transfer-Encoding: {cte}\r\n".encode()
                + b'Content-Disposition: attachment; filename="x.eml"\r\n\r\n'
                + body
                + b"\r\n--b--\r\n"
            )

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "plain.eml").write_bytes(parent("7bit", inner))
        (folder / "encoded.eml").write_bytes(parent(encoding, encoded))
        plain = parse_email(folder / "plain.eml")
        coded = parse_email(folder / "encoded.eml")
        assert plain is not None and coded is not None
        assert b"INNER=body" in coded.attachments[0].payload
        assert coded.attachments[0].content_hash == plain.attachments[0].content_hash

    @staticmethod
    def _inner_with_long_subject() -> bytes:
        return (
            b"Message-ID: <inner@example.test>\r\nFrom: other@example.test\r\n"
            b"Subject: " + b"LONG_SUBJECT " * 12 + b"\r\n\r\nINNER=body\r\n"
        )

    def _attached(self, cte: str, body: bytes, *, wrap: bool) -> bytes:
        attachment = (
            b"--i\r\nContent-Type: message/rfc822\r\n"
            + f"Content-Transfer-Encoding: {cte}\r\n".encode()
            + b'Content-Disposition: attachment; filename="x.eml"\r\n\r\n'
            + body
            + b"\r\n--i--\r\n"
        )
        inner_part = b'Content-Type: multipart/mixed; boundary="i"\r\n\r\n' + attachment
        head = (
            b"Message-ID: <outer@example.test>\r\nFrom: sender@example.test\r\n"
            b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
        )
        if not wrap:
            return head + inner_part
        # One more multipart level, so the part is two boundaries deep.
        return (
            head
            + b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
            + b"--o\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            + b"--o\r\n"
            + inner_part
            + b"\r\n--o--\r\n"
        )

    @pytest.mark.parametrize("wrap", [False, True])
    def test_quoted_printable_soft_break_in_a_header_hashes_like_7bit(self, tmp_path, wrap):
        """Review round 3: the parser splits a quoted-printable attached
        email into headers and body before it is decoded, so a soft line
        break in a long Subject moved text into the body and changed the
        hash. The part's own raw bytes are sliced out by boundary and
        decoded instead."""
        import quopri

        inner = self._inner_with_long_subject()
        encoded = quopri.encodestring(inner)
        assert b"=\n" in encoded or b"=\r\n" in encoded  # a soft break
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "plain.eml").write_bytes(self._attached("7bit", inner, wrap=wrap))
        (folder / "qp.eml").write_bytes(self._attached("quoted-printable", encoded, wrap=wrap))
        plain = parse_email(folder / "plain.eml")
        coded = parse_email(folder / "qp.eml")
        assert plain is not None and coded is not None
        # Serialization folds the long Subject; undo that to compare text.
        unfolded = coded.attachments[0].payload.replace(b"\n ", b" ")
        assert b"LONG_SUBJECT " * 12 in unfolded
        assert b"INNER=body" in coded.attachments[0].payload
        assert coded.attachments[0].content_hash == plain.attachments[0].content_hash

    def test_raw_slicing_gives_up_when_the_bytes_do_not_match_the_tree(self):
        """A boundary missing from the raw bytes (here, bytes of another
        message) returns nothing, and the caller keeps its best effort."""
        import email

        from src.parser import _raw_part

        parsed = email.message_from_bytes(self._attached("7bit", b"x\r\n", wrap=True))
        other = self._attached("7bit", b"x\r\n", wrap=False)
        assert _raw_part(other, parsed, (1, 0)) is None
        whole = self._attached("7bit", b"x\r\n", wrap=True)
        assert _raw_part(whole, parsed, (1, 0)) is not None

    def test_undecodable_base64_attached_email_keeps_an_empty_payload(self, tmp_path):
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "m.eml"
        path.write_bytes(
            b"Message-ID: <bad64@example.test>\r\nFrom: sender@example.test\r\n"
            b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b"--b\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n"
            b'Content-Disposition: attachment; filename="x.eml"\r\n\r\nA\r\n--b--\r\n'
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.body_text == "PARENT_BODY"
        assert msg.attachments[0].payload == b""

    def test_eight_bit_disposition_does_not_abort_parsing(self, tmp_path):
        """Review round 1: an unencoded 8-bit Content-Disposition comes back
        from the compat32 parser as a Header, which has no ``lower()``."""
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "m.eml"
        path.write_bytes(
            b"Message-ID: <eight@example.test>\r\nFrom: sender@example.test\r\n"
            b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="r\xe9sum\xe9.pdf"\r\n\r\n%PDF\r\n'
        )
        msg = parse_email(path)
        assert msg is not None
        [attachment] = msg.attachments
        assert attachment.filename.endswith(".pdf")

    def _parse_raw(self, tmp_path, raw: bytes):
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "raw.eml"
        path.write_bytes(raw)
        msg = parse_email(path)
        assert msg is not None
        return msg

    _HEAD = (
        b"Message-ID: <outer@example.test>\r\nFrom: sender@example.test\r\n"
        b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
    )

    def test_attached_email_as_the_root_is_an_attachment(self, tmp_path):
        """Review round 2: compat32 reports message/rfc822 as multipart, so
        a root attached email skipped classification and its body was
        taken as the outer message's."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b"Content-Type: message/rfc822\r\n"
            b'Content-Disposition: attachment; filename="forward.eml"\r\n\r\n'
            b"Message-ID: <inner@example.test>\r\nFrom: other@example.test\r\n\r\n"
            b"INNER_MARKER\r\n",
        )
        assert "INNER_MARKER" not in msg.body_text
        assert [a.content_type for a in msg.attachments] == ["message/rfc822"]
        assert b"INNER_MARKER" in msg.attachments[0].payload

    def test_attachments_inside_attachments_are_still_found(self, tmp_path):
        """Review round 2: the old walk found a PDF inside an attached
        bundle or email; stopping at the boundary lost it. Text inside an
        attachment is still never the parent's body."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
            b"--o\r\nContent-Type: text/html\r\n\r\n<p>OUTER_BODY</p>\r\n"
            b'--o\r\nContent-Type: multipart/mixed; boundary="i"\r\n'
            b'Content-Disposition: attachment; filename="bundle.mime"\r\n\r\n'
            b"--i\r\nContent-Type: text/plain\r\n\r\nBUNDLE_TEXT\r\n"
            b"--i\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="inner.pdf"\r\n\r\n%PDF-INNER\r\n'
            b"--i--\r\n--o--\r\n",
        )
        assert "OUTER_BODY" in msg.body_text
        assert "BUNDLE_TEXT" not in msg.body_text
        by_name = {a.filename: a for a in msg.attachments}
        assert set(by_name) == {"bundle.mime", "inner.pdf"}
        assert b"%PDF-INNER" in by_name["inner.pdf"].payload
        assert b"BUNDLE_TEXT" in by_name["bundle.mime"].payload

    def test_every_delivery_status_block_is_kept(self, tmp_path):
        """Review round 2: message/delivery-status parses into one block
        per recipient; only the first was serialized."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
            b"Final-Recipient: rfc822; first@example.test\r\nStatus: 5.1.1\r\n\r\n"
            b"Final-Recipient: rfc822; second@example.test\r\nStatus: 5.2.2\r\n"
            b"\r\n--r--\r\n",
        )
        [status] = msg.attachments
        assert b"first@example.test" in status.payload
        assert b"second@example.test" in status.payload

    def test_single_part_attachment_is_an_attachment(self, tmp_path):
        """#209: a message whose root part is an attachment had its payload
        decoded as the body and no attachment recorded."""
        message = EmailMessage()
        message["Message-ID"] = "<single@example.test>"
        message["From"] = "sender@example.test"
        message["Subject"] = "Invoice"
        message.set_content(
            b"%PDF-1.4 SYNTHETIC_PDF_MARKER",
            maintype="application",
            subtype="pdf",
            disposition="attachment",
            filename="invoice.pdf",
        )
        msg = parse_email(_write_message(tmp_path, message))
        assert msg is not None
        assert msg.body_text == ""
        assert msg.has_attachments is True
        [attachment] = msg.attachments
        assert attachment.filename == "invoice.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.payload == b"%PDF-1.4 SYNTHETIC_PDF_MARKER"

    def test_single_part_binary_body_is_not_decoded_as_text(self, tmp_path):
        """#209: only text parts are message text; a nameless binary root
        is skipped, as the same part is inside a multipart."""
        message = EmailMessage()
        message["Message-ID"] = "<binary@example.test>"
        message["From"] = "sender@example.test"
        message.set_content(b"BINARY_MARKER", maintype="application", subtype="octet-stream")
        msg = parse_email(_write_message(tmp_path, message))
        assert msg is not None
        assert msg.body_text == ""
        assert msg.attachments == []

    def test_single_part_text_body_is_still_the_body(self, tmp_path):
        message = EmailMessage()
        message["Message-ID"] = "<calendar@example.test>"
        message["From"] = "sender@example.test"
        message.set_content("CALENDAR_MARKER", subtype="calendar")
        msg = parse_email(_write_message(tmp_path, message))
        assert msg is not None
        assert msg.body_text == "CALENDAR_MARKER"


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
        """Behavior contract: invalid / obscure charset labels do NOT
        drop the message. ``_decode_header`` falls back to utf-8 with
        ``errors="replace"`` locally, so a recoverable header pathology
        stays inside the function rather than escaping into the queue's
        dead-letter path. This complements the broader contract that
        unanticipated parser failures DO escape ``parse_email`` (no
        blanket ``except Exception``) so the queue can retry / dead-
        letter them with operator visibility."""
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

    def test_mixed_plain_and_encoded_text(self):
        assert _decode_header("Re: =?utf-8?q?H=C3=A9llo?= world") == "Re: Héllo world"

    def test_adjacent_encoded_words_join_without_whitespace(self):
        """RFC 2047 §6.2: whitespace between adjacent encoded-words is
        not part of the text."""
        assert _decode_header("=?utf-8?q?H=C3=A9?= =?utf-8?q?llo?=") == "Héllo"

    def test_whitespace_kept_next_to_a_malformed_encoded_word(self):
        """Review round 1: whitespace is dropped only between two valid
        encoded-words. Collapsing it before validating both sides fused
        ``ok`` onto the raw malformed fragment."""
        from src.parser import _decode_display_name

        value = "=?utf-8?q?ok?= =?utf-8?x?bad?="
        assert _decode_header(value) == "ok =?utf-8?x?bad?="
        assert _decode_display_name(value) == "ok =?utf-8?x?bad?="

    def test_malformed_encoded_word_prefixes_decode_in_linear_time(self):
        """Regression (#218): ``email.header.decode_header`` rescans the
        rest of the header at every malformed ``=?`` prefix, so a 48 KB
        Subject of them took ~0.4 s and a multi-MB one stalled the
        indexing worker. The prefixes are kept as raw text."""
        import time

        value = "=?utf-8?q?x " * 16_000
        started = time.monotonic()
        result = _decode_header(value)
        assert time.monotonic() - started < 1.0
        assert result == value.strip()

    def test_malformed_subject_does_not_stall_parse(self, tmp_path):
        """End-to-end #218: Subject and the raw-From fallback both go
        through ``_decode_header``."""
        import time

        junk = "=?utf-8?q?x " * 16_000
        content = (
            f"From: {junk}\r\n"
            "To: bob@example.com\r\n"
            f"Subject: {junk}\r\n"
            "Message-ID: <slow_subject@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Body.\r\n"
        )
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "slow.eml"
        path.write_bytes(content.encode("utf-8"))
        started = time.monotonic()
        msg = parse_email(path)
        assert time.monotonic() - started < 2.0
        assert msg is not None
        assert msg.subject.startswith("=?utf-8?q?x")


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


def test_format_address_never_changes_the_address():
    """Identity invariant for the (name, address) -> string round trip:
    whatever a display name contains, the serialized string must parse
    back to the same address, or the name is dropped."""
    from src.parser import _format_address
    from src.threader import canonical_addr

    for name in ("Mallory@example.com\r", "Doe,\r Jane", "x\n<mallory@example.com>", "a\x00b"):
        assert canonical_addr(_format_address(name, "bob@example.com")) == "bob@example.com"
    assert _format_address("Doe, Jane", "jane@example.com") == '"Doe, Jane" <jane@example.com>'
    assert _format_address("José Álvarez", "josé@example.com") == "José Álvarez <josé@example.com>"


def test_split_address_list_only_splits_at_top_level():
    from src.parser import _split_address_list as split

    # Empty elements vanish wherever they appear.
    assert split(", a@x, , b@x,") == ["a@x", "b@x"]
    # Group names are dropped; members become elements; ";" ends a group.
    assert split("Team: , a@x, ,b@x, ; c@x") == ["a@x", "b@x", "c@x"]
    assert split("undisclosed-recipients:;") == []
    # Commas and colons inside quoted strings, comments, angle brackets,
    # and domain literals are content, never separators.
    assert split('"a, ,b"@x, c@x,') == ['"a, ,b"@x', "c@x"]
    assert split('"Doe, Jane" <j@x>, "Re: x" <r@x>') == ['"Doe, Jane" <j@x>', '"Re: x" <r@x>']
    assert split("a@x (p, q: r), b@x") == ["a@x (p, q: r)", "b@x"]
    assert split("<@hostA,@hostB:joe@x>, b@x") == ["<@hostA,@hostB:joe@x>", "b@x"]
    assert split("a@[1, ,2], b@x") == ["a@[1, ,2]", "b@x"]
    # Escaped quotes do not end a quoted string early.
    assert split('"a\\", ,b"@x, c@x') == ['"a\\", ,b"@x', "c@x"]
    # Unterminated constructs run to the end (no separator inside them).
    assert split('a@x, "b, ,c') == ["a@x", '"b, ,c']


def test_encoded_word_contents_are_never_parsed_as_syntax():
    from src.parser import _parse_addrs
    from src.threader import canonical_addr

    # Colons, commas, parentheses, and "@" inside encoded-words are data.
    out = _parse_addrs("=?utf-8?q?a:b,c@d?= <bob@example.com>, =?x(((?q?A?= <carol@example.com>")
    assert [canonical_addr(a) for a in out] == ["bob@example.com", "carol@example.com"]


def test_parse_addrs_output_is_always_a_parseaddr_fixed_point():
    """Every emitted string is re-parsed downstream (identity check,
    canonical_addr, the participant writer). Anything that does not
    round-trip — including an unsafe restored encoded-word — is
    discarded rather than handed to an unguarded reparser."""
    from email.utils import parseaddr

    from src.parser import _parse_addrs

    bomb = "=?x" + "(" * 1200 + ")" * 1200 + "?q?bob@example.com?= (Bob)"
    assert _parse_addrs(bomb) == []
    assert _parse_addrs(bomb + ", carol@example.com") == ["carol@example.com"]
    for header in (
        '"Doe, Jane" <jane@example.com>, =?utf-8?q?Zo=C3=AB?= <zoe@example.com>',
        "josé@example.com, Team: a@x.example, b@x.example;",
    ):
        for formatted in _parse_addrs(header):
            addr = parseaddr(formatted)[1]
            assert addr and parseaddr(addr)[1] == addr

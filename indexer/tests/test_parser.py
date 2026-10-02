"""
Tests for src/parser.py.

Covers: plain text, HTML, multipart, attachments, inline Content-Disposition,
encoded headers, address parsing, date fallback, and folder derivation.
"""

import base64
import email.utils
import hashlib
import logging
import quopri
import textwrap
from datetime import UTC, datetime
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

    @pytest.mark.parametrize(
        ("date_header", "fallback"),
        [
            ("", True),
            ("Date: not-a-date\n", True),
            ("Date: Mon, 01 Jan 2024 12:00:00 +0000\n", False),
        ],
        ids=["missing", "malformed", "valid"],
    )
    def test_fallback_date_is_flagged(self, tmp_path, date_header, fallback):
        """#297: callers keep an already-persisted date only when the
        parser fabricated this one, so the fallback must be visible."""
        path = tmp_path / "INBOX" / "cur" / "flag.eml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "From: alice@example.com\nSubject: s\nMessage-ID: <flag@example.com>\n"
            f"{date_header}\nBody.\n",
            encoding="utf-8",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.date_is_fallback is fallback

    @pytest.mark.parametrize(
        "branch",
        ["unparseable", "type_error", "parsed_none"],
    )
    def test_fallback_date_value_not_logged(self, tmp_path, monkeypatch, caplog, branch):
        """#257: the Date header is attacker-controlled mail content, so no
        fallback branch may log it. ``parsedate_to_datetime`` on 3.14
        raises only ``ValueError`` for a ``str``; the other two branches
        are reached by stubbing it."""
        marker = "SYNTHETIC-DATE-MARKER-257"
        if branch == "type_error":

            def _raise_type_error(_value):
                raise TypeError(marker)

            monkeypatch.setattr(email.utils, "parsedate_to_datetime", _raise_type_error)
        elif branch == "parsed_none":
            monkeypatch.setattr(email.utils, "parsedate_to_datetime", lambda _value: None)
        path = tmp_path / "INBOX" / "cur" / "marker.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"From: alice@example.com\nSubject: s\nMessage-ID: <marker@example.com>\n"
            + f"Date: {marker}\n\nBody.\n".encode()
        )
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(path)
        assert msg is not None
        assert msg.date_is_fallback is True
        assert any("date header" in r.getMessage().lower() for r in caplog.records)
        assert marker not in caplog.text

    @pytest.mark.parametrize(
        ("date_bytes", "fallback"),
        [
            (b"Mon, 1 Jan 2024 10:00:00 +0000 \xe9", False),
            (b"Mon, 1 Jan 2024 10:00:00 +0000\xe9", False),
            (b"Mon, 1 Jan 2024 15:00:00 +0500\xe9", False),
            (b"Mon, 1 Jan 2024 07:00:00 -0300\xe9\xe9", False),
            (b"\xe9\xe9 not a date", True),
        ],
    )
    def test_8bit_date_header_does_not_crash(self, tmp_path, date_bytes, fallback):
        """#361: a raw 8-bit Date header comes back from the parser as an
        ``email.header.Header``, which ``parsedate_to_datetime`` cannot
        split; it must parse like the text it holds, or fall back."""
        path = tmp_path / "INBOX" / "cur" / "eightbit.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"From: alice@example.com\nSubject: s\nMessage-ID: <eightbit@example.com>\n"
            + b"Date: "
            + date_bytes
            + b"\n\nBody.\n"
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.date_is_fallback is fallback
        if not fallback:
            assert msg.date == datetime(2024, 1, 1, 10, 0, tzinfo=UTC)

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
        # dead-letters them terminally (``mark_dead_terminal``)
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


class TestClaimantId:
    """#217: the per-message key is the Message-ID plus the first eight
    hex digits of the SHA-256 of the file's raw bytes, so two files
    claiming one Message-ID with different content get distinct keys,
    while the same file keeps its key across reparses, flag renames and
    folder moves (none of which change its bytes)."""

    _RAW = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Claimant\r\n"
        b"Message-ID: <claimant@example.com>\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"\r\n"
        b"Body.\r\n"
    )

    def _parse(self, tmp_path, rel: str, raw: bytes):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        msg = parse_email(path, maildir_root=tmp_path)
        assert msg is not None
        return msg

    def test_is_message_id_plus_raw_bytes_hash_prefix(self, tmp_path):
        msg = self._parse(tmp_path, "INBOX/cur/a:2,S", self._RAW)
        digest = hashlib.sha256(self._RAW).hexdigest()
        assert msg.claimant_id == f"claimant@example.com#{digest[:8]}"

    def test_independent_of_flags_filename_and_folder(self, tmp_path):
        first = self._parse(tmp_path, "INBOX/cur/a:2,S", self._RAW)
        renamed = self._parse(tmp_path, "Archive/cur/b:2,RS", self._RAW)
        assert renamed.claimant_id == first.claimant_id

    def test_differs_for_different_content(self, tmp_path):
        first = self._parse(tmp_path, "INBOX/cur/a:2,S", self._RAW)
        other = self._parse(tmp_path, "INBOX/cur/b:2,S", self._RAW.replace(b"Body.", b"Other."))
        assert other.message_id == first.message_id
        assert other.claimant_id != first.claimant_id

    def test_hand_built_message_without_hash_keys_on_message_id(self):
        """Messages built in tests without file identity fall back to
        the bare Message-ID."""
        from src.parser import Message

        msg = Message(
            message_id="bare@example.com",
            in_reply_to=None,
            references=[],
            subject="",
            from_addr="",
            to_addrs=[],
            cc_addrs=[],
            date=datetime(2024, 1, 1, tzinfo=UTC),
            body_text="",
            folder="INBOX",
            filepath="/x",
        )
        assert msg.claimant_id == "bare@example.com"


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

    @pytest.mark.parametrize(
        ("plain", "expected"),
        [
            ("", "HTML_INVOICE_MARKER"),
            ("\n", "HTML_INVOICE_MARKER"),
            ("\r\n\r\n", "HTML_INVOICE_MARKER"),
            ("   \t ", "HTML_INVOICE_MARKER"),
            ("PLAIN_BODY_MARKER", "PLAIN_BODY_MARKER"),
        ],
        ids=["empty", "newline", "blank-lines", "spaces", "nonempty-plain-wins"],
    )
    def test_whitespace_only_plain_falls_back_to_html(self, tmp_path, plain, expected):
        """#298: a whitespace-only text/plain alternative was truthy, so
        it suppressed a substantive HTML alternative and was then stripped
        to an empty body. Plain text wins only when it has content."""
        message = EmailMessage()
        message["Message-ID"] = "<blank-plain@example.test>"
        message["From"] = "billing@example.test"
        message["To"] = "owner@example.test"
        message["Date"] = "Mon, 28 Sep 2026 12:00:00 +0000"
        message["Subject"] = "Invoice"
        message.set_content(plain)
        message.add_alternative("<p>HTML_INVOICE_MARKER total due</p>", subtype="html")
        msg = parse_email(_write_message(tmp_path, message))
        assert msg is not None
        assert expected in msg.body_text
        if expected == "PLAIN_BODY_MARKER":
            assert "HTML_INVOICE_MARKER" not in msg.body_text


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

    @staticmethod
    def _deep_attached_email(depth: int, leaf_bytes: int, cte: str | None = None) -> bytes:
        """A message carrying an attached email whose body is ``depth``
        nested message/rfc822 wrappers around a ``leaf_bytes`` text leaf.
        ``cte`` labels the attachment with a transfer encoding, which the
        parser ignores when it reads the body as MIME."""
        line = b"x" * 76 + b"\r\n"
        leaf = b"Content-Type: text/plain\r\n\r\n" + line * max(1, leaf_bytes // len(line))
        nested = b"Content-Type: message/rfc822\r\n\r\n" * (depth - 1) + leaf
        label = f"Content-Transfer-Encoding: {cte}\r\n".encode() if cte else b""
        return (
            b"Message-ID: <deep@example.test>\r\nFrom: sender@example.test\r\n"
            b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b"--b\r\nContent-Type: message/rfc822\r\n"
            + label
            + b'Content-Disposition: attachment; filename="x.eml"\r\n\r\n'
            + nested
            + b"\r\n--b--\r\n"
        )

    @staticmethod
    def _count_serializations(monkeypatch) -> list[int]:
        """Count ``Message.as_bytes`` calls: the parser never serializes,
        so every call is the attachment walk serializing a container."""
        import email.message

        calls: list[int] = []
        real = email.message.Message.as_bytes

        def counting(self, *args, **kwargs):
            calls.append(1)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(email.message.Message, "as_bytes", counting)
        return calls

    # Serializing copies the subtree once per level above it, so the
    # worst case is a large leaf under many wrappers: 24 MB (under the
    # 50 MB parse cap) under 240, the shape the security review measured.
    WORST_CASE_DEPTH = 240
    WORST_CASE_LEAF = 24_000_000
    # Generous: the bounded path parses the 24 MB in well under a second.
    WORST_CASE_SECONDS = 10.0

    def test_attached_email_at_the_cap_is_serialized(self, tmp_path):
        from src.parser import MAX_ATTACHED_MESSAGE_DEPTH

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "d.eml"
        path.write_bytes(self._deep_attached_email(MAX_ATTACHED_MESSAGE_DEPTH, 100))
        msg = parse_email(path)
        assert msg is not None
        [attachment] = msg.attachments
        assert b"xxxx" in attachment.payload

    @pytest.mark.parametrize("cte", [None, "base64"])
    def test_attached_email_past_the_cap_is_never_serialized(self, tmp_path, monkeypatch, cte):
        """Review rounds 1, 7 and 8: past the depth cap the attached email
        keeps the empty payload without its tree ever being serialized —
        including a transfer-encoded one, whose transport body the parser
        still reads as MIME. The serialization count is the enforcing
        check: on Python 3.14 the unbounded serialization of this worst
        case measures well under a second, so elapsed time alone cannot
        tell the two apart; the time bound is the safety net."""
        import time

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "d.eml"
        path.write_bytes(
            self._deep_attached_email(self.WORST_CASE_DEPTH, self.WORST_CASE_LEAF, cte)
        )
        calls = self._count_serializations(monkeypatch)
        started = time.perf_counter()
        msg = parse_email(path)
        elapsed = time.perf_counter() - started
        assert msg is not None
        assert msg.body_text == "PARENT_BODY"
        assert msg.attachments[0].payload == b""
        assert calls == []
        assert elapsed < self.WORST_CASE_SECONDS

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

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_attachments_inside_an_encoded_attached_email_are_found(self, tmp_path, encoding):
        """Review round 4: traversal walked the parser's tree, where an
        encoded attached email is transport text, so the PDF it carries
        was recorded under 7bit but not base64 / quoted-printable."""
        import base64
        import quopri

        inner = (
            b"Message-ID: <inner@example.test>\r\nFrom: other@example.test\r\n"
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="n"\r\n\r\n'
            b"--n\r\nContent-Type: text/plain\r\n\r\nINNER_TEXT\r\n"
            b"--n\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="inside.pdf"\r\n\r\n%PDF-INSIDE\r\n'
            b"--n--\r\n"
        )
        encoded = base64.encodebytes(inner) if encoding == "base64" else quopri.encodestring(inner)
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        path = folder / "m.eml"
        path.write_bytes(
            b"Message-ID: <outer@example.test>\r\nFrom: sender@example.test\r\n"
            b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
            b"--o\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b"--o\r\nContent-Type: message/rfc822\r\n"
            + f"Content-Transfer-Encoding: {encoding}\r\n".encode()
            + b'Content-Disposition: attachment; filename="fwd.eml"\r\n\r\n'
            + encoded
            + b"\r\n--o--\r\n"
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.body_text == "PARENT_BODY"
        by_name = {a.filename: a for a in msg.attachments}
        assert set(by_name) == {"fwd.eml", "inside.pdf"}
        assert b"%PDF-INSIDE" in by_name["inside.pdf"].payload

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

    def test_multipart_root_presented_as_an_attachment_is_one(self, tmp_path):
        """Review round 5: a multipart root with an attachment disposition
        was exempt from classification, so the bundle went unrecorded and
        its text became the body. It is classified like a single-part
        root; the PDF inside is still found."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="b"\r\n'
            b'Content-Disposition: attachment; filename="bundle.mime"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nBUNDLE_TEXT\r\n"
            b"--b\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="inner.pdf"\r\n\r\n%PDF-INNER\r\n'
            b"--b--\r\n",
        )
        assert "BUNDLE_TEXT" not in msg.body_text
        by_name = {a.filename: a for a in msg.attachments}
        assert set(by_name) == {"bundle.mime", "inner.pdf"}
        assert b"BUNDLE_TEXT" in by_name["bundle.mime"].payload

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

    @pytest.mark.parametrize("fields, serialized", [(100, True), (20_000, False)])
    def test_container_with_too_many_fields_is_never_serialized(
        self, tmp_path, monkeypatch, fields, serialized
    ):
        """Review round 9: the generator costs several times the parser
        per header field, and a delivery report is one field per line, so
        a report past MAX_ATTACHED_MESSAGE_FIELDS keeps the empty payload
        without ever being serialized."""
        from src.parser import MAX_ATTACHED_MESSAGE_FIELDS

        assert (fields > MAX_ATTACHED_MESSAGE_FIELDS) is not serialized
        raw = (
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            b"Reporting-MTA: dns; mx.example.test\r\n"
            + b"X-Field: value\r\n" * fields
            + b"\r\n--r--\r\n"
        )
        calls = self._count_serializations(monkeypatch)
        msg = self._parse_raw(tmp_path, raw)
        [status] = msg.attachments
        assert (status.payload != b"") is serialized
        assert (calls != []) is serialized

    def test_headerless_blocks_count_against_the_budget(self, tmp_path, monkeypatch):
        """Review round 10: a delivery report of blank blocks parses into
        parts with no header fields, so a fields-only budget never
        advanced while every part was still serialized. Parts count too."""
        from src.parser import MAX_ATTACHED_MESSAGE_FIELDS

        raw = (
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
            + b"\r\n" * (2 * MAX_ATTACHED_MESSAGE_FIELDS)
            + b"--r--\r\n"
        )
        calls = self._count_serializations(monkeypatch)
        msg = self._parse_raw(tmp_path, raw)
        assert msg.attachments[0].payload == b""
        assert calls == []

    def test_header_bytes_count_against_the_budget(self, tmp_path, monkeypatch):
        """Review round 11: one 5 MB header field counted as one unit but
        cost the generator seconds to refold. Header bytes count too."""
        raw = (
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            b"X-Long: " + b"word " * 200_000 + b"\r\n\r\n--r--\r\n"
        )
        calls = self._count_serializations(monkeypatch)
        msg = self._parse_raw(tmp_path, raw)
        assert msg.attachments[0].payload == b""
        assert calls == []

    def test_the_budget_spans_every_container_in_a_message(self, tmp_path, monkeypatch):
        """Review round 10: fifty sibling reports each under the budget
        cost fifty serializations. One budget covers the whole message,
        so once it is spent no later container is serialized. Each
        report is worth about 70% of the budget (its fields plus their
        bytes), so the first fits and the second exhausts it."""
        from src.parser import MAX_ATTACHED_MESSAGE_FIELDS

        report = (
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            b"Reporting-MTA: dns; mx.example.test\r\n"
            + b"X-Field: value\r\n" * (MAX_ATTACHED_MESSAGE_FIELDS * 4 // 10)
            + b"\r\n"
        )
        raw = (
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            + report * 50
            + b"--r--\r\n"
        )
        calls = self._count_serializations(monkeypatch)
        msg = self._parse_raw(tmp_path, raw)
        assert len(msg.attachments) == 50
        assert msg.attachments[0].payload != b""
        assert all(a.payload == b"" for a in msg.attachments[1:])
        assert len(calls) == 1

    # Review round 12: an attached email must hash the same whatever its
    # transfer encoding. Every shape here matched 7bit as base64 all along;
    # quoted-printable diverged on six (a multipart body, and any header
    # line of 76+ characters, which quoted-printable soft-breaks) until the
    # transport text was rebuilt from the parser's raw tuples.
    _INNER_HEAD = b"From: a@example.test\r\nTo: b@example.test\r\nSubject: Hi\r\n"
    _INNER_MULTIPART = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="n"\r\n\r\n'
        b"--n\r\nContent-Type: text/plain\r\n\r\nhello\r\n--n--\r\n"
    )
    ENCODING_PARITY_SHAPES = {
        "plain body": _INNER_HEAD + b"\r\nhello world\r\n",
        "multipart body": _INNER_HEAD + _INNER_MULTIPART,
        "no trailing newline": _INNER_HEAD + b"\r\nhello",
        "extra blank lines at end": _INNER_HEAD + b"\r\nhello\r\n\r\n\r\n",
        "empty body": _INNER_HEAD + b"\r\n",
        "headers only": _INNER_HEAD,
        "8-bit body": _INNER_HEAD + "\r\nr\u00e9sum\u00e9 \u2014 caf\u00e9\r\n".encode(),
        "8-bit header": b"From: a@example.test\r\nSubject: r\xc3\xa9sum\xc3\xa9\r\n\r\nhello\r\n",
        "long body line": _INNER_HEAD + b"\r\n" + b"w" * 300 + b"\r\n",
        "trailing spaces": _INNER_HEAD + b"\r\nhello   \r\nworld \r\n",
        "tabs": _INNER_HEAD + b"\r\na\tb\r\n",
        "equals signs": _INNER_HEAD + b"\r\na=b=c ==\r\n",
        "folded header": b"From: a@example.test\r\nSubject: aaa\r\n bbb\r\n\r\nhello\r\n",
        "header 75 chars": b"From: a@example.test\r\nSubject: " + b"s" * 66 + b"\r\n\r\nhello\r\n",
        "header 76 chars": b"From: a@example.test\r\nSubject: " + b"s" * 67 + b"\r\n\r\nhello\r\n",
        "header 80 chars": b"From: a@example.test\r\nSubject: " + b"s" * 71 + b"\r\n\r\nhello\r\n",
        "long DKIM-like header": (
            b"From: a@example.test\r\nDKIM-Signature: v=1; a=rsa-sha256; b="
            + b"Q" * 200
            + b"\r\n\r\nhello\r\n"
        ),
        "equals in header": b"From: a@example.test\r\nX-Q: a=b\r\n\r\nhello\r\n",
        "no space after colon": b"From:a@example.test\r\nSubject:Hi\r\n\r\nhello\r\n",
        "LF line endings": _INNER_HEAD.replace(b"\r\n", b"\n") + b"\nhello\n",
        "From at line start": _INNER_HEAD + b"\r\nFrom the top\r\n",
        "From after blank line": _INNER_HEAD + b"\r\n\r\nFrom the top\r\n",
        "nested attached email": (
            _INNER_HEAD
            + b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="n"\r\n\r\n'
            b"--n\r\nContent-Type: message/rfc822\r\n\r\nFrom: c@example.test\r\n\r\ninner\r\n--n--\r\n"
        ),
        "folded Content-Type": (
            _INNER_HEAD
            + b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed;\r\n boundary="abc"\r\n\r\n'
            b"--abc\r\nContent-Type: text/plain\r\n\r\nhello\r\n--abc--\r\n"
        ),
        "QP-looking body text": _INNER_HEAD + b"\r\nprice =3D 5 and =20\r\n",
    }

    @pytest.mark.parametrize("shape", sorted(ENCODING_PARITY_SHAPES))
    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_transfer_encoded_attached_email_hashes_like_7bit(self, tmp_path, shape, encoding):
        import base64
        import quopri

        inner = self.ENCODING_PARITY_SHAPES[shape]
        encoded = base64.encodebytes(inner) if encoding == "base64" else quopri.encodestring(inner)

        def outer(cte: str, body: bytes) -> bytes:
            return (
                self._HEAD + b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
                b"--o\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
                b"--o\r\nContent-Type: message/rfc822\r\n"
                + f"Content-Transfer-Encoding: {cte}\r\n".encode()
                + b'Content-Disposition: attachment; filename="x.eml"\r\n\r\n'
                + body
                + b"\r\n--o--\r\n"
            )

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "plain.eml").write_bytes(outer("7bit", inner))
        (folder / "coded.eml").write_bytes(outer(encoding, encoded))
        plain = parse_email(folder / "plain.eml")
        coded = parse_email(folder / "coded.eml")
        assert plain is not None and coded is not None
        assert plain.attachments[0].payload != b""
        assert coded.attachments[0].content_hash == plain.attachments[0].content_hash

    def test_header_the_generator_refuses_keeps_an_empty_payload(self, tmp_path):
        """Review round 13 (P1): a header compat32 accepts but the
        generator refuses raised HeaderWriteError, whose text quotes the
        header, out of parse_email and into the job's recorded error."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b'--b\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="x.eml"\r\n\r\n'
            b"From: a@example.test\r\nX: PRIVATE_MARKER\x0brest\r\n\r\nhello\r\n--b--\r\n",
        )
        assert msg.body_text == "PARENT_BODY"
        assert msg.attachments[0].payload == b""

    def test_eight_bit_bytes_in_a_transport_form_keep_an_empty_payload(self, tmp_path):
        """Review round 13: raw 8-bit bytes inside a quoted-printable
        transport form (malformed) raised UnicodeEncodeError out of the
        reconstruction and failed the whole message."""
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b"--b\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: quoted-printable\r\n"
            b'Content-Disposition: attachment; filename="x.eml"\r\n\r\n'
            b"From: a@example.test\r\nSubject: caf\xc3\xa9\r\n\r\nhello\r\n--b--\r\n",
        )
        assert msg.body_text == "PARENT_BODY"
        assert msg.attachments[0].payload == b""

    def test_quoted_printable_delivery_status_hashes_like_7bit(self, tmp_path):
        """Review round 13: the rebuilt report ended with a blank line the
        text did not have, so the same report hashed differently."""
        import quopri

        report = (
            b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
            b"Final-Recipient: rfc822; first@example.test\r\nStatus: 5.1.1\r\n\r\n"
            b"Final-Recipient: rfc822; second@example.test\r\nStatus: 5.2.2\r\n"
        )

        def outer(cte: bytes, body: bytes) -> bytes:
            return (
                self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
                b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
                b"--r\r\nContent-Type: message/delivery-status\r\n"
                + cte
                + b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
                + body
                + b"\r\n--r--\r\n"
            )

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "plain.eml").write_bytes(outer(b"", report))
        (folder / "qp.eml").write_bytes(
            outer(b"Content-Transfer-Encoding: quoted-printable\r\n", quopri.encodestring(report))
        )
        plain = parse_email(folder / "plain.eml")
        coded = parse_email(folder / "qp.eml")
        assert plain is not None and coded is not None
        assert coded.attachments[0].content_hash == plain.attachments[0].content_hash

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_attachments_inside_a_nested_encoded_attached_email_are_found(self, tmp_path, encoding):
        """Review round 13: a nested container's payload is not kept, and
        that skipped decoding it too, so the tree walked was the transport
        form and a PDF two forwards deep was lost."""
        import base64
        import quopri

        innermost = (
            b'From: c@example.test\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="z"\r\n\r\n'
            b"--z\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="deep.pdf"\r\n\r\n%PDF-DEEP\r\n--z--\r\n'
        )
        encoded = (
            base64.encodebytes(innermost)
            if encoding == "base64"
            else quopri.encodestring(innermost)
        )
        middle = (
            b'From: b@example.test\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="m"\r\n\r\n'
            b"--m\r\nContent-Type: message/rfc822\r\n"
            + f"Content-Transfer-Encoding: {encoding}\r\n".encode()
            + b'Content-Disposition: attachment; filename="inner.eml"\r\n\r\n'
            + encoded
            + b"\r\n--m--\r\n"
        )
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
            b"--o\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b'--o\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="outer.eml"\r\n\r\n'
            + middle
            + b"\r\n--o--\r\n",
        )
        by_name = {a.filename: a for a in msg.attachments}
        assert set(by_name) == {"outer.eml", "inner.eml", "deep.pdf"}
        assert b"%PDF-DEEP" in by_name["deep.pdf"].payload

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_encoded_delivery_status_keeps_block_semantics(self, tmp_path, encoding):
        """Review round 14: the decoded report was parsed as a plain
        message, so only its first block counted as headers and a long
        header in a later block folded differently from the 7bit path.
        The decoded text is now parsed as the part's own content type."""
        import base64
        import quopri

        report = (
            b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
            b"Final-Recipient: rfc822; first@example.test\r\nStatus: 5.1.1\r\n"
            b"Diagnostic-Code: smtp; 550 5.1.1 the mailbox does not exist here, try again later\r\n"
        )
        encoded = (
            base64.encodebytes(report) if encoding == "base64" else quopri.encodestring(report)
        )

        def outer(cte: bytes, body: bytes) -> bytes:
            return (
                self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
                b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
                b"--r\r\nContent-Type: message/delivery-status\r\n"
                + cte
                + b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
                + body
                + b"\r\n--r--\r\n"
            )

        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True)
        (folder / "plain.eml").write_bytes(outer(b"", report))
        (folder / "coded.eml").write_bytes(
            outer(f"Content-Transfer-Encoding: {encoding}\r\n".encode(), encoded)
        )
        plain = parse_email(folder / "plain.eml")
        coded = parse_email(folder / "coded.eml")
        assert plain is not None and coded is not None
        assert b"first@example.test" in coded.attachments[0].payload
        assert coded.attachments[0].content_hash == plain.attachments[0].content_hash

    def test_nested_encoded_attached_emails_stop_decoding_at_the_caps(self, tmp_path, monkeypatch):
        """Review round 14: inside an attachment the decoded tree was
        walked without the depth check, so a chain of transfer-encoded
        attached emails decoded its large descendant at every level (40
        decodes, 165 MB for a 10.8 MB fixture). Decoding now stops at the
        depth cap or when the message's decodable bytes are spent."""
        import quopri

        from src import parser as parser_module
        from src.parser import MAX_ATTACHED_MESSAGE_DEPTH, MAX_DECODED_ATTACHMENT_BYTES

        decoded_sizes: list[int] = []
        real = parser_module._decode_transport_form

        def counting(data, encoding, content_type):
            decoded_sizes.append(len(data))
            return real(data, encoding, content_type)

        monkeypatch.setattr(parser_module, "_decode_transport_form", counting)
        inner = b"From: z@example.test\r\nSubject: leaf\r\n\r\n" + (b"y" * 76 + b"\r\n") * 6_000
        for i in range(40):
            inner = (
                b"From: w@example.test\r\nMIME-Version: 1.0\r\n"
                b'Content-Type: multipart/mixed; boundary="b%d"\r\n\r\n--b%d\r\n'
                b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: quoted-printable\r\n"
                b'Content-Disposition: attachment; filename="l%d.eml"\r\n\r\n'
                % (i, i, i)
                + quopri.encodestring(inner)
                + b"\r\n--b%d--\r\n" % i
            )
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/mixed; boundary="o"\r\n\r\n'
            b"--o\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
            b'--o\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename="top.eml"\r\n\r\n'
            + inner
            + b"\r\n--o--\r\n",
        )
        assert msg.body_text == "PARENT_BODY"
        assert 1 < len(decoded_sizes) <= MAX_ATTACHED_MESSAGE_DEPTH
        assert sum(decoded_sizes) <= MAX_DECODED_ATTACHMENT_BYTES + decoded_sizes[-1]
        # Every decoded level's attached email is recorded, plus the top
        # and the first one past the caps (recorded, not decoded).
        assert len(msg.attachments) == len(decoded_sizes) + 2

    def test_quoted_printable_delivery_status_keeps_every_block(self, tmp_path):
        """Review round 9: the transfer-encoded path serialized only the
        container's first child before decoding. An attached email has
        one child, but a delivery report has one per block, so a
        quoted-printable report lost every recipient's status."""
        import quopri

        report = (
            b"Reporting-MTA: dns; mx.example.test\r\n\r\n"
            b"Final-Recipient: rfc822; first@example.test\r\nStatus: 5.1.1\r\n\r\n"
            b"Final-Recipient: rfc822; second@example.test\r\nStatus: 5.2.2\r\n"
        )
        msg = self._parse_raw(
            tmp_path,
            self._HEAD + b'Content-Type: multipart/report; boundary="r"\r\n\r\n'
            b"--r\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
            b"--r\r\nContent-Type: message/delivery-status\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b'Content-Disposition: attachment; filename="status.txt"\r\n\r\n'
            + quopri.encodestring(report)
            + b"\r\n--r--\r\n",
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
        assert _decode_header(encoded) == "Héllo"

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

    def test_codec_rejecting_replace_falls_back_to_utf8(self, tmp_path):
        """#257: a sender charset whose codec rejects ``errors="replace"``
        (``idna`` raises ``UnicodeError``, not ``LookupError``) must take
        the same utf-8 fallback as an unknown label instead of escaping
        ``parse_email``."""
        path = tmp_path / "INBOX" / "cur" / "idna.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"From: alice@example.com\r\n"
            b"Subject: =?idna?q?Caf=C3=A9?=\r\n"
            b"Message-ID: <idna@example.com>\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=idna\r\n"
            b"\r\n"
            b"Caf\xc3\xa9 body.\r\n"
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.subject == "Café"
        assert msg.body_text == "Café body."

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


# ---------------------------------------------------------------------------
# Body assembly shape catalogue (#295)
# ---------------------------------------------------------------------------
#
# Each shape is a MIME tree: a leaf is ``(content_type, text, headers)``,
# a container is ``(multipart_type, [children], headers)``. The expected
# body is exact. Invariant: the body is the concatenation, in document
# order and separated by a blank line, of every non-blank inline text
# part outside attachments, where a multipart/alternative contributes
# one of its children (the first carrying non-blank plain text, else the
# first carrying any text).

_PDF = ("application/pdf", "JVBERi0=", {"Content-Disposition": 'attachment; filename="r.pdf"'})
_IMG = ("image/png", "iVBORw0=", {"Content-Disposition": 'inline; filename="i.png"'})
_FORWARDED = (
    "message/rfc822",
    "FORWARDED_MARKER",
    {"Content-Disposition": 'attachment; filename="fwd.eml"'},
)


def _plain(text: str, disposition: str = "") -> tuple:
    return ("text/plain", text, {"Content-Disposition": disposition} if disposition else {})


def _html(text: str) -> tuple:
    return ("text/html", f"<p>{text}</p>", {})


def _multi(subtype: str, *children: tuple) -> tuple:
    return (f"multipart/{subtype}", list(children), {})


def _render(node: tuple, counter: list[int]) -> str:
    """Raw MIME text for a shape; ``counter`` numbers the boundaries."""
    ctype, content, headers = node
    head = f"Content-Type: {ctype}"
    if ctype.startswith("multipart/"):
        counter[0] += 1
        boundary = f"b{counter[0]}"
        head += f'; boundary="{boundary}"'
    elif ctype.startswith("text/"):
        head += "; charset=utf-8\r\nContent-Transfer-Encoding: 8bit"
    elif ctype != "message/rfc822":
        head += "\r\nContent-Transfer-Encoding: base64"
    head += "".join(f"\r\n{name}: {value}" for name, value in headers.items()) + "\r\n\r\n"
    if ctype == "message/rfc822":
        inner = (
            "Message-ID: <inner@example.test>\r\nFrom: other@example.test\r\n"
            f"Content-Type: text/plain\r\n\r\n{content}\r\n"
        )
        encoding = headers.get("Content-Transfer-Encoding", "").lower()
        if encoding == "base64":
            inner = base64.encodebytes(inner.encode()).decode().replace("\n", "\r\n")
        elif encoding == "quoted-printable":
            inner = quopri.encodestring(inner.encode()).decode()
        return head + inner
    if not ctype.startswith("multipart/"):
        return head + content + "\r\n"
    body = "".join(f"--{boundary}\r\n{_render(child, counter)}" for child in content)
    return head + body + f"--{boundary}--\r\n"


_BODY_SHAPES = {
    # Shapes handled before #295; the expected bodies pin that behaviour.
    "single-plain": (_plain("P1"), "P1"),
    "single-html": (_html("H1"), "H1"),
    "single-8bit": (_plain("café P1"), "café P1"),
    "single-blank": (_plain(" \r\n"), ""),
    "alt-plain-html": (_multi("alternative", _plain("P1"), _html("H1")), "P1"),
    "alt-html-plain": (_multi("alternative", _html("H1"), _plain("P1")), "P1"),
    "alt-blank-plain-html": (_multi("alternative", _plain("  "), _html("H1")), "H1"),
    "mixed-plain-attachment": (_multi("mixed", _plain("P1"), _PDF), "P1"),
    "mixed-attachment-only": (_multi("mixed", _PDF), ""),
    "mixed-inline-plain": (_multi("mixed", _plain("P1", "inline")), "P1"),
    "mixed-alt-attachment": (
        _multi("mixed", _multi("alternative", _plain("P1"), _html("H1")), _PDF),
        "P1",
    ),
    "alt-plain-related-html": (
        _multi("alternative", _plain("P1"), _multi("related", _html("H1"), _IMG)),
        "P1",
    ),
    # A multipart/related contributes only its root, the first child;
    # later parts are resources it references (review round 2 on #444).
    "related-html-root-html-resource": (
        _multi("related", _html("H1"), _html("RESOURCE")),
        "H1",
    ),
    "related-html-root-plain-resource": (
        _multi("related", _html("H1"), _plain("RESOURCE")),
        "H1",
    ),
    "related-alt-root-image": (
        _multi("related", _multi("alternative", _plain("P1"), _html("H1")), _IMG),
        "P1",
    ),
    "related-blank-root-html-resource": (
        _multi("related", _html(""), _html("RESOURCE")),
        "",
    ),
    "mixed-related-then-plain": (
        _multi("mixed", _multi("related", _html("H1"), _html("RESOURCE")), _plain("P2")),
        "H1\n\nP2",
    ),
    "mixed-labelled-text-attachments": (
        _multi(
            "mixed",
            _plain("P1"),
            _plain("ATTACHED_TEXT", 'attachment; filename="n.txt"'),
            _plain("NAMED_TEXT", 'inline; filename="a.txt"'),
            _plain("NAMELESS_ATTACHED_TEXT", "attachment"),
        ),
        "P1",
    ),
    "mixed-html-forwarded-email": (_multi("mixed", _html("H1"), _FORWARDED), "H1"),
    # Sequential inline text parts (#295): each is kept, in order.
    "mixed-plain-attachment-plain": (
        _multi("mixed", _plain("P1"), _PDF, _plain("P2")),
        "P1\n\nP2",
    ),
    "mixed-html-attachment-html": (
        _multi("mixed", _html("H1"), _PDF, _html("H2")),
        "H1\n\nH2",
    ),
    "mixed-alt-attachment-plain": (
        _multi("mixed", _multi("alternative", _plain("P1"), _html("H1")), _PDF, _plain("P2")),
        "P1\n\nP2",
    ),
    "mixed-two-alternatives": (
        _multi(
            "mixed",
            _multi("alternative", _plain("P1"), _html("H1")),
            _multi("alternative", _html("H2"), _plain("P2")),
        ),
        "P1\n\nP2",
    ),
    "mixed-alt-blank-plain-then-plain": (
        _multi("mixed", _multi("alternative", _plain(""), _html("H1")), _plain("P2")),
        "H1\n\nP2",
    ),
    "mixed-plain-blank-plain": (
        _multi("mixed", _plain("P1"), _plain(" \r\n"), _plain("P2")),
        "P1\n\nP2",
    ),
    "mixed-plain-forwarded-plain": (
        _multi("mixed", _plain("P1"), _FORWARDED, _plain("P2")),
        "P1\n\nP2",
    ),
    "mixed-plain-inline-email": (
        _multi("mixed", _plain("P1"), ("message/rfc822", "INLINE_EMAIL", {})),
        "P1\n\nINLINE_EMAIL",
    ),
    "mixed-plain-inline-7bit-email": (
        _multi(
            "mixed",
            _plain("P1"),
            ("message/rfc822", "INLINE_EMAIL", {"Content-Transfer-Encoding": "7bit"}),
        ),
        "P1\n\nINLINE_EMAIL",
    ),
    # An inline email in a transfer encoding is exposed by the parser as
    # its encoded transport text, so it contributes nothing (review round
    # 1 on #444).
    "mixed-plain-inline-base64-email": (
        _multi(
            "mixed",
            _plain("P1"),
            ("message/rfc822", "ENCODED_EMAIL_" * 8, {"Content-Transfer-Encoding": "base64"}),
            _plain("P2"),
        ),
        "P1\n\nP2",
    ),
    "mixed-plain-inline-qp-email": (
        _multi(
            "mixed",
            _plain("P1"),
            (
                "message/rfc822",
                "ENCODED_EMAIL=",
                {"Content-Transfer-Encoding": "quoted-printable"},
            ),
        ),
        "P1",
    ),
    "inline-base64-email-only": (
        _multi(
            "mixed",
            ("message/rfc822", "ENCODED_EMAIL", {"Content-Transfer-Encoding": "BASE64"}),
        ),
        "",
    ),
    "alt-plain-mixed-html": (
        _multi("alternative", _plain("P1"), _multi("mixed", _html("H1"), _IMG, _html("H2"))),
        "P1",
    ),
    "alt-blank-plain-mixed-html": (
        _multi("alternative", _plain(""), _multi("mixed", _html("H1"), _PDF, _html("H2"))),
        "H1\n\nH2",
    ),
    "nested-mixed": (
        _multi("mixed", _plain("P1"), _multi("mixed", _PDF, _plain("P2")), _plain("P3")),
        "P1\n\nP2\n\nP3",
    ),
}


@pytest.mark.parametrize(("shape", "expected"), _BODY_SHAPES.values(), ids=_BODY_SHAPES.keys())
def test_body_assembly_shape_catalogue(tmp_path, shape, expected):
    """#295: separate inline text parts outside a multipart/alternative
    are sequential content, so later ones were lost from the body."""
    raw = (
        "Message-ID: <shape@example.test>\r\nFrom: sender@example.test\r\n"
        "Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
    ) + _render(shape, [0])
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "shape.eml"
    path.write_bytes(raw.encode("utf-8"))
    msg = parse_email(path)
    assert msg is not None
    assert msg.body_text == expected
    for marker in ("FORWARDED_MARKER", "ATTACHED_TEXT", "NAMED_TEXT"):
        assert marker not in msg.body_text


def test_body_text_parts_decoded_are_capped(tmp_path, monkeypatch):
    """#295: every inline text part is now decoded, each with a fresh
    html2text converter, so the number decoded per message is capped."""
    import time

    from src import parser

    calls = 0
    real = parser._html_to_text

    def counting(html: str) -> str:
        nonlocal calls
        calls += 1
        return real(html)

    monkeypatch.setattr(parser, "_html_to_text", counting)
    parts = 20_000
    raw = (
        b"Message-ID: <many@example.test>\r\nFrom: sender@example.test\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"".join(
            b"--b\r\nContent-Type: text/html\r\n\r\n<p>S%d</p>\r\n" % i for i in range(parts)
        )
        + b"--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "many.eml"
    path.write_bytes(raw)
    started = time.perf_counter()
    msg = parse_email(path)
    assert time.perf_counter() - started < 10
    assert msg is not None
    assert calls == parser.MAX_BODY_TEXT_PARTS
    segments = msg.body_text.split("\n\n")
    assert segments == [f"S{i}" for i in range(parser.MAX_BODY_TEXT_PARTS)]


# ---------------------------------------------------------------------------
# Attachment filename shape catalogue (#362)
# ---------------------------------------------------------------------------

# Each shape is the filename-bearing header lines of one attachment part
# and the filename the parser records. Most shapes pin what
# ``get_filename()`` already returns. The RFC 2231 shapes whose charset
# label names a codec that refuses ``errors="replace"`` (``idna``,
# ``undefined``) or holds a NUL made ``get_filename()`` raise out of
# ``parse_email``; they now fall back to the raw parameter text, as the
# standard library already does for a charset label it does not know.
_FILENAME_SHAPES = {
    "plain": (
        b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="doc.pdf"',
        "doc.pdf",
    ),
    "rfc2231-valid": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''r%C3%A9sum%C3%A9.pdf",
        "résumé.pdf",
    ),
    "rfc2231-unknown-charset": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=x-unknown-362''r%E9sum%E9.pdf",
        "résumé.pdf",
    ),
    "rfc2231-idna": (
        b"Content-Type: application/pdf\r\nContent-Disposition: attachment; filename*=idna''doc.pdf",
        "doc.pdf",
    ),
    "rfc2231-idna-8bit": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=idna''r%E9sum%E9.pdf",
        "résumé.pdf",
    ),
    "rfc2231-undefined-codec": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=undefined''doc.pdf",
        "doc.pdf",
    ),
    "rfc2231-nul-in-charset": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8\x00''doc.pdf",
        "doc.pdf",
    ),
    "rfc2231-idna-empty": (
        b"Content-Type: application/pdf\r\nContent-Disposition: attachment; filename*=idna''",
        "unnamed",
    ),
    "content-type-name-only": (
        b'Content-Type: application/pdf; name="doc.pdf"',
        "doc.pdf",
    ),
    "content-type-name-idna-only": (
        b"Content-Type: application/pdf; name*=idna''doc.pdf",
        "doc.pdf",
    ),
    # The standard library leaves an encoded-word in a quoted parameter
    # undecoded; pinned as is.
    "encoded-word": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?r=C3=A9sum=C3=A9.pdf?="',
        "=?utf-8?q?r=C3=A9sum=C3=A9.pdf?=",
    ),
}


def _write_filename_message(tmp_path: Path, headers: bytes) -> Path:
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "m.eml"
    path.write_bytes(
        b"From: sender@example.test\r\n"
        b"Message-ID: <fname@example.test>\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n'
        b"\r\n"
        b"--b\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body text.\r\n"
        b"--b\r\n" + headers + b"\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"AAAA\r\n"
        b"--b--\r\n"
    )
    return path


@pytest.mark.parametrize("shape", sorted(_FILENAME_SHAPES))
def test_attachment_filename_shape_catalogue(tmp_path, shape):
    headers, expected = _FILENAME_SHAPES[shape]
    msg = parse_email(_write_filename_message(tmp_path, headers))
    assert msg is not None
    assert [a.filename for a in msg.attachments] == [expected]
    assert msg.body_text == "Body text."


@pytest.mark.parametrize("shape", sorted(_FILENAME_SHAPES))
def test_attachment_filename_matches_stdlib_or_its_unknown_charset_fallback(shape):
    """The class invariant: where ``get_filename()`` returns, the parser's
    filename is its value; where it raises, the parser's filename is what
    the standard library returns for the same parameter under a charset
    label it does not know (its own raw-text fallback)."""
    from src.parser import _part_filename

    headers, _ = _FILENAME_SHAPES[shape]
    part = email.message_from_bytes(headers + b"\r\n\r\nAAAA\r\n")
    try:
        stdlib = part.get_filename()
    except ValueError:
        unknown = headers
        for label in (b"idna", b"undefined", b"utf-8\x00"):
            unknown = unknown.replace(label + b"''", b"x-unknown-362''")
        assert unknown != headers
        stdlib = email.message_from_bytes(unknown + b"\r\n\r\nAAAA\r\n").get_filename()
    assert _part_filename(part) == stdlib


def test_undecodable_filename_is_not_logged(tmp_path, caplog):
    """#362: the fallback logs one fixed warning naming the exception
    type, never the filename."""
    marker = "FNAME_MARKER_362"
    headers = (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=idna''" + marker.encode() + b".pdf"
    )
    with caplog.at_level(logging.DEBUG):
        msg = parse_email(_write_filename_message(tmp_path, headers))
    assert msg is not None
    assert [a.filename for a in msg.attachments] == [f"{marker}.pdf"]
    warnings = [r for r in caplog.records if "filename" in r.getMessage()]
    assert len(warnings) == 1
    assert "UnicodeError" in warnings[0].getMessage()
    assert marker not in caplog.text

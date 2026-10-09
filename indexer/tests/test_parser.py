"""
Tests for src/parser.py.

Covers: plain text, HTML, multipart, attachments, inline Content-Disposition,
encoded headers, address parsing, date fallback, and folder derivation.
"""

import base64
import dataclasses
import email.errors
import email.utils
import hashlib
import logging
import quopri
import re
import textwrap
import time
from collections import Counter
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from src.parser import (
    MESSAGE_ID_MAX_CHARS,
    PARSE_CAPS,
    PARSE_REPEATS,
    SUBJECT_MAX_CHARS,
    OversizedMessageError,
    SourceMetadata,
    _clean_id,
    _decode_header,
    _derive_folder,
    _parse_addrs,
    parse_email,
    parse_email_bytes,
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
        ["unparseable", "type_error"],
    )
    def test_fallback_date_value_not_logged(self, tmp_path, monkeypatch, caplog, branch):
        """#257: the Date header is attacker-controlled mail content, so no
        fallback branch may log it. ``parsedate_to_datetime`` on 3.14
        raises only ``ValueError`` for a ``str``; the ``TypeError`` branch
        is reached by stubbing it."""
        marker = "SYNTHETIC-DATE-MARKER-257"
        if branch == "type_error":

            def _raise_type_error(_value):
                raise TypeError(marker)

            monkeypatch.setattr(email.utils, "parsedate_to_datetime", _raise_type_error)
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

    def test_date_overflowing_utc_falls_back(self, tmp_path):
        """Review round 2: a Date header whose UTC conversion passes year
        9999 is treated as unparseable (fallback date, flagged) instead
        of failing the parse and dead-lettering the message."""
        path = tmp_path / "INBOX" / "cur" / "overflow.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"From: alice@example.com\nSubject: s\nMessage-ID: <overflow@example.com>\n"
            b"Date: Fri, 31 Dec 9999 23:59:59 -2359\n\nBody.\n"
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.date_is_fallback is True

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
        ``path.parent.parent.name``, which collapses
        ``Clients/.ABC/cur/msg`` to just ``.ABC`` and loses the parent
        context. Passing the root preserves the full folder name."""
        folder = tmp_path / "Clients" / ".ABC" / "cur"
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


def _legacy_dir(name: str) -> Path:
    """The directory isync writes a folder to under ``SubFolders Legacy``
    (``maildir_join_path``, unchanged from 1.4.4 to 1.5.1): the first
    component as it is, then ``/.`` before each later one."""
    first, *rest = name.split("/")
    return Path(first, *(f".{component}" for component in rest))


# Folder names as isync stores them, covering every shape the layout
# treats differently. isync 1.5.1 (#833) decodes Bridge's modified UTF-7
# to UTF-8 (Folders/Café, Folders/A&B); the encoded spelling isync 1.4.4
# kept (Folders/Caf&AOk-) remains on an install not yet migrated. Also:
# top level, nested, Maildir's own directory names as children, dots, a
# leading dot, "!", spaces, INBOX below the top level and deep nesting.
LEGACY_FOLDER_NAMES = [
    "INBOX",
    "Trash",
    "Spam",
    "Folders",
    "INBOX/Child",
    "Folders/Clients",
    "Folders/Parent/cur",
    "Folders/Parent/new",
    "Folders/Parent/tmp",
    "Folders/cur",
    "Folders/a.b",
    "Folders/.dot",
    "Folders/..two",
    "Folders/x!y",
    "Folders/with space",
    "Folders/Caf&AOk-",
    "Folders/Café",
    "Folders/A&B",
    "Folders/INBOX",
    "Folders/Deep/Er/Est",
]


class TestDeriveFolderLegacyLayout:
    """mbsync writes child folders with ``SubFolders Legacy`` (#281): the
    folder name is the path below the root with the one leading dot
    isync adds to every component after the first removed."""

    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            ("INBOX/cur/m", "INBOX"),
            ("Folders/.Clients/cur/m", "Folders/Clients"),
            ("Folders/.Parent/.cur/cur/m", "Folders/Parent/cur"),
            ("Folders/.Parent/.new/new/m", "Folders/Parent/new"),
            ("Folders/..dot/cur/m", "Folders/.dot"),
            ("INBOX/.Child/new/m", "INBOX/Child"),
        ],
    )
    def test_isync_paths_map_to_folder_names(self, tmp_path, relative, expected):
        assert _derive_folder(tmp_path / relative, tmp_path) == expected

    @pytest.mark.parametrize("name", LEGACY_FOLDER_NAMES)
    @pytest.mark.parametrize("subdir", ["cur", "new"])
    def test_every_name_round_trips(self, tmp_path, name, subdir):
        path = tmp_path / _legacy_dir(name) / subdir / "1700000000.synthetic:2,S"
        assert _derive_folder(path, tmp_path) == name

    def test_distinct_names_get_distinct_directories(self):
        dirs = {_legacy_dir(name) for name in LEGACY_FOLDER_NAMES}
        assert len(dirs) == len(LEGACY_FOLDER_NAMES)


# ---------------------------------------------------------------------------
# parse_email — file identity
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
# parse_email_bytes — the pure half, and the Maildir adapter (#1077)
# ---------------------------------------------------------------------------

_SPLIT_RAW = (
    b"From: alice@example.com\nTo: bob@example.com\nSubject: Split\n"
    b"Message-ID: <split@example.com>\nDate: Mon, 01 Jan 2024 12:00:00 +0000\n\nBody.\n"
)


class TestParseEmailBytes:
    def test_bytes_and_file_give_the_same_message(self, tmp_path):
        """The split is output-identical: the bytes with the file's
        ``SourceMetadata`` parse to the ``Message`` the file does."""
        path = tmp_path / "INBOX" / "cur" / "m:2,S"
        path.parent.mkdir(parents=True)
        path.write_bytes(_SPLIT_RAW)
        from_file = parse_email(path, maildir_root=tmp_path)
        assert from_file is not None
        source = SourceMetadata(
            folder="INBOX",
            flags=frozenset("S"),
            size=len(_SPLIT_RAW),
            mtime_ns=from_file.mtime_ns,
            path=str(path),
        )
        path.unlink()  # nothing is left to read
        assert parse_email_bytes(_SPLIT_RAW, source) == from_file

    def test_reads_nothing_from_disk(self, tmp_path, monkeypatch):
        """``parse_email_bytes`` opens no file: the path is only a label."""

        def _no_open(*_args, **_kwargs):
            raise AssertionError("parse_email_bytes opened a file")

        monkeypatch.setattr("builtins.open", _no_open)
        monkeypatch.setattr(Path, "open", _no_open)
        source = SourceMetadata(
            folder="Folders/Clients",
            flags=frozenset(),
            size=len(_SPLIT_RAW),
            mtime_ns=None,
            path=str(tmp_path / "Folders" / ".Clients" / "cur" / "missing"),
        )
        msg = parse_email_bytes(_SPLIT_RAW, source)
        assert msg is not None
        assert msg.folder == "Folders/Clients"
        assert msg.filepath == source.path
        assert msg.mtime_ns is None
        assert (msg.size, msg.content_hash) == (
            len(_SPLIT_RAW),
            hashlib.sha256(_SPLIT_RAW).hexdigest(),
        )
        assert parse_email_bytes(b"Subject: no id\n\nBody.\n", source) is None

    def test_source_metadata_is_frozen(self):
        source = SourceMetadata(folder="INBOX", flags=frozenset(), size=1, mtime_ns=None, path="p")
        with pytest.raises(dataclasses.FrozenInstanceError):
            source.folder = "Sent"  # type: ignore[misc]

    def test_adapter_builds_source_metadata_from_the_file(self, tmp_path, monkeypatch):
        """``parse_email`` reads the file and hands its bytes with the
        folder, flags, size, mtime and path to ``parse_email_bytes``."""
        from src import parser

        path = tmp_path / "Folders" / ".Clients" / "cur" / "m:2,FS"
        path.parent.mkdir(parents=True)
        path.write_bytes(_SPLIT_RAW)
        seen: list[tuple[bytes, SourceMetadata]] = []

        def _capture(raw: bytes, source: SourceMetadata):
            seen.append((raw, source))
            return None

        monkeypatch.setattr(parser, "parse_email_bytes", _capture)
        assert parse_email(path, maildir_root=tmp_path) is None
        assert seen == [
            (
                _SPLIT_RAW,
                SourceMetadata(
                    folder="Folders/Clients",
                    flags=frozenset("FS"),
                    size=len(_SPLIT_RAW),
                    mtime_ns=path.stat().st_mtime_ns,
                    path=str(path),
                ),
            )
        ]


class TestClaimantId:
    """#217: the per-message key is the Message-ID plus the first sixteen
    hex digits (#454) of the SHA-256 of the file's raw bytes, so two files
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
        assert msg.claimant_id == f"claimant@example.com#{digest[:16]}"

    def test_files_sharing_a_32_bit_hash_prefix_get_distinct_keys(self, tmp_path):
        """#454: a 32-bit suffix lets a crafted file that collides on the
        first eight hex digits take over another claimant's rows. These
        two bodies (found by a birthday search) share that prefix but
        not the full hash, and must still get distinct claimant IDs."""
        head = self._RAW.replace(b"Body.\r\n", b"")
        first_raw = head + b"Body 8176.\r\n"
        second_raw = head + b"Body 71374.\r\n"
        first_digest = hashlib.sha256(first_raw).hexdigest()
        second_digest = hashlib.sha256(second_raw).hexdigest()
        assert first_digest[:8] == second_digest[:8]
        assert first_digest != second_digest

        first = self._parse(tmp_path, "INBOX/cur/a:2,S", first_raw)
        second = self._parse(tmp_path, "INBOX/cur/b:2,S", second_raw)
        assert first.message_id == second.message_id
        assert first.claimant_id != second.claimant_id

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


def _raw_with_subject(subject: bytes) -> bytes:
    return (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: " + subject + b"\r\n"
        b"Message-ID: <long-subject@example.com>\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"\r\n"
        b"Body.\r\n"
    )


class TestSubjectCap:
    """#541: the stored subject is cut to ``SUBJECT_MAX_CHARS`` at parse
    time, so every reader of ``messages.subject`` / ``threads.subject``
    (the ``threads_fts`` subject scan, the rerank reply-subject scan)
    reads at most that many characters per row, whatever the header."""

    def _parse(self, tmp_path, raw: bytes):
        path = tmp_path / "INBOX" / "cur" / "long:2,S"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        msg = parse_email(path, maildir_root=tmp_path)
        assert msg is not None
        return msg

    def test_cap_is_well_above_a_real_subject(self):
        from src.parser import SUBJECT_MAX_CHARS

        # RFC 5322 caps a header line at 998 characters; a real subject
        # is far shorter. The cap leaves room for a folded one.
        assert SUBJECT_MAX_CHARS == 2000

    def test_multi_megabyte_ascii_subject_is_capped(self, tmp_path):
        import time

        from src.parser import SUBJECT_MAX_CHARS

        started = time.perf_counter()
        msg = self._parse(tmp_path, _raw_with_subject(b"A" * 3_000_000))
        assert time.perf_counter() - started < 10
        assert msg.subject == "A" * SUBJECT_MAX_CHARS

    def test_multi_megabyte_8bit_subject_is_capped_in_characters(self, tmp_path):
        """A raw 8-bit header comes back as a ``Header``; the cap counts
        decoded characters, not bytes."""
        from src.parser import SUBJECT_MAX_CHARS

        msg = self._parse(tmp_path, _raw_with_subject("é".encode() * 1_500_000))
        assert len(msg.subject) == SUBJECT_MAX_CHARS
        assert set(msg.subject) == {"é"}

    def test_long_encoded_word_subject_is_capped_after_decoding(self, tmp_path):
        from src.parser import SUBJECT_MAX_CHARS

        word = b"=?utf-8?b?" + base64.b64encode(b"x" * 45) + b"?="
        msg = self._parse(tmp_path, _raw_with_subject(b" ".join([word] * 20_000)))
        assert msg.subject == "x" * SUBJECT_MAX_CHARS

    @pytest.mark.parametrize("length", [1, 200, 1999, 2000])
    def test_subject_within_the_cap_is_unchanged(self, tmp_path, length):
        subject = ("Quarterly report " * 200)[:length].strip() or "Q"
        msg = self._parse(tmp_path, _raw_with_subject(subject.encode()))
        assert msg.subject == subject


# ---------------------------------------------------------------------------
# parse_email — occurred_at (top Received: header)
# ---------------------------------------------------------------------------


def _received_eml(tmp_path: Path, headers: bytes, name: str = "received.eml") -> Path:
    """A delivered message whose header block starts with ``headers``."""
    path = tmp_path / "INBOX" / "cur" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        headers
        + b"From: alice@example.com\r\n"
        + b"To: bob@example.com\r\n"
        + b"Subject: Delivery time\r\n"
        + b"Message-ID: <received@example.com>\r\n"
        + b"Date: Mon, 01 Jan 2024 08:00:00 +0000\r\n"
        + b"Content-Type: text/plain; charset=utf-8\r\n"
        + b"\r\nBody.\r\n"
    )
    return path


class TestOccurredAt:
    """``occurred_at`` is the delivery time from the topmost ``Received:``
    header: the text after its last ``;``, parsed by the stdlib. It is
    never taken from ``Date:``, and is ``None`` when it cannot be read."""

    def test_top_received_date_is_parsed_to_utc(self, tmp_path):
        path = _received_eml(
            tmp_path,
            b"Received: from mx.example.net (mx.example.net [192.0.2.1])\r\n"
            b"\tby mail.example.org with ESMTPS id abc123;\r\n"
            b"\tTue, 02 Jan 2024 15:30:00 +0500 (PKT)\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 2, 10, 30, tzinfo=UTC)
        assert msg.occurred_at.tzinfo is UTC
        # sent_at is unchanged: it stays the Date: header.
        assert msg.date == datetime(2024, 1, 1, 8, 0, tzinfo=UTC)

    def test_topmost_of_several_received_headers_wins(self, tmp_path):
        path = _received_eml(
            tmp_path,
            b"Received: by mail.example.org; Wed, 03 Jan 2024 09:00:00 +0000\r\n"
            b"Received: from relay.example.net by mx.example.org;"
            b" Tue, 02 Jan 2024 09:00:00 +0000\r\n"
            b"Received: from client.example.com by relay.example.net;"
            b" Mon, 01 Jan 2024 09:00:00 +0000\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 3, 9, 0, tzinfo=UTC)

    def test_text_after_the_last_semicolon_is_the_date(self, tmp_path):
        """A ``;`` inside the trace clauses does not hide the date."""
        path = _received_eml(
            tmp_path,
            b"Received: from a.example (helo=x; y) by b.example (z; w);"
            b" Thu, 04 Jan 2024 11:00:00 -0100\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 4, 12, 0, tzinfo=UTC)

    def test_received_without_semicolon_is_none(self, tmp_path):
        path = _received_eml(
            tmp_path,
            b"Received: from mx.example.net by mail.example.org"
            b" Tue, 02 Jan 2024 09:00:00 +0000\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None

    @pytest.mark.parametrize(
        "tail",
        [
            b"",
            b" not a date",
            b" Tue, 99 Foo 2024 25:61:00 +0000",
            b" 1 Jan 99999 10:00:00 +0000",
        ],
        ids=["empty", "gibberish", "out-of-range", "huge-year"],
    )
    def test_malformed_received_date_is_none(self, tmp_path, tail):
        path = _received_eml(tmp_path, b"Received: by mail.example.org;" + tail + b"\r\n")
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None
        # The Date: header is not a fallback for occurred_at.
        assert msg.date == datetime(2024, 1, 1, 8, 0, tzinfo=UTC)

    def test_absent_received_is_none_and_date_is_not_used(self, tmp_path):
        """Sent mail carries no Received: header."""
        path = _received_eml(tmp_path, b"")
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None
        assert msg.date_is_fallback is False

    def test_folded_long_received_header(self, tmp_path):
        clauses = b"".join(b"\r\n\t(via hop-%d.example.net; id %d)" % (i, i) for i in range(200))
        path = _received_eml(
            tmp_path,
            b"Received: from mx.example.net" + clauses + b"\r\n\tby mail.example.org;\r\n"
            b"\tFri, 05 Jan 2024 06:07:08 +0000\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 5, 6, 7, 8, tzinfo=UTC)

    @pytest.mark.parametrize(
        "received",
        [
            b"Received: from caf\xc3\xa9.example by mx.example.org;"
            b" Sat, 06 Jan 2024 10:00:00 +0000",
            b"Received: from x by mx.example.org; Sat, 06 Jan 2024 10:00:00 +0000\xe9",
            b"Received: from =?utf-8?q?caf=C3=A9?= by mx.example.org;"
            b" Sat, 06 Jan 2024 10:00:00 +0000",
        ],
        ids=["8bit-clause", "8bit-after-zone", "encoded-word"],
    )
    def test_8bit_and_encoded_received_header(self, tmp_path, received):
        path = _received_eml(tmp_path, received + b"\r\n")
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 6, 10, 0, tzinfo=UTC)

    def test_8bit_only_received_date_is_none(self, tmp_path):
        path = _received_eml(tmp_path, b"Received: by mx.example.org; \xe9\xe9\xe9\r\n")
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError,
            TypeError,
            OverflowError,
            email.errors.HeaderParseError,
            email.errors.MessageDefect,
            UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "SYNTHETIC-RECEIVED-MARKER"),
            LookupError,
        ],
    )
    def test_parse_errors_degrade_to_none_without_logging_the_header(
        self, tmp_path, monkeypatch, caplog, exc
    ):
        """Every error the stdlib can raise on the header degrades to
        ``None``; the header text is attacker-controlled and never logged."""
        marker = "SYNTHETIC-RECEIVED-MARKER"
        real = email.utils.parsedate_to_datetime

        def _raise(value):
            if marker not in value:
                return real(value)  # the Date: header
            raise exc(marker) if isinstance(exc, type) else exc

        monkeypatch.setattr(email.utils, "parsedate_to_datetime", _raise)
        path = _received_eml(tmp_path, f"Received: by {marker}; {marker}\r\n".encode())
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None
        assert marker not in caplog.text

    @pytest.mark.parametrize(
        "tail",
        [b" Fri, 31 Dec 9999 23:59:59 -2359", b" Fri, 31 Dec 9999 23:59:59 -0100"],
        ids=["max-offset", "one-hour"],
    )
    def test_date_overflowing_utc_is_none(self, tmp_path, tail):
        """Review round 2: a date that parses but whose UTC conversion
        passes year 9999 degrades to None instead of failing the parse."""
        path = _received_eml(tmp_path, b"Received: by mx.example.org;" + tail + b"\r\n")
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None
        assert msg.date == datetime(2024, 1, 1, 8, 0, tzinfo=UTC)

    def test_naive_received_date_is_utc(self, tmp_path):
        path = _received_eml(
            tmp_path, b"Received: by mx.example.org; Sun, 07 Jan 2024 10:00:00 -0000\r\n"
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 7, 10, 0, tzinfo=UTC)
        assert msg.occurred_at.tzinfo is UTC

    def test_huge_received_header_is_bounded(self, tmp_path, monkeypatch):
        """A crafted multi-megabyte top Received header full of ``;``
        hands the date parser at most ``RECEIVED_DATE_MAX_CHARS``
        characters, once."""
        from src.parser import RECEIVED_DATE_MAX_CHARS

        seen: list[int] = []
        real = email.utils.parsedate_to_datetime

        def _spy(value):
            seen.append(len(value))
            return real(value)

        monkeypatch.setattr(email.utils, "parsedate_to_datetime", _spy)
        filler = b"; x" * 1_000_000
        path = _received_eml(
            tmp_path,
            b"Received: from mx.example.net" + filler + b"; Mon, 08 Jan 2024 10:00:00 +0000\r\n",
        )
        started = time.perf_counter()
        msg = parse_email(path)
        elapsed = time.perf_counter() - started
        assert msg is not None
        assert msg.occurred_at == datetime(2024, 1, 8, 10, 0, tzinfo=UTC)
        # Two parses: Date: and the Received date, each bounded.
        assert len(seen) == 2
        assert max(seen) <= RECEIVED_DATE_MAX_CHARS
        assert elapsed < 10

    def test_date_longer_than_the_cap_is_none(self, tmp_path, monkeypatch):
        """No ``;`` within the last ``RECEIVED_DATE_MAX_CHARS`` characters
        means the date text would be longer than any real one: ``None``,
        and the date parser is not called for it."""
        from src.parser import RECEIVED_DATE_MAX_CHARS

        calls: list[str] = []
        real = email.utils.parsedate_to_datetime

        def _spy(value):
            calls.append(value)
            return real(value)

        monkeypatch.setattr(email.utils, "parsedate_to_datetime", _spy)
        path = _received_eml(
            tmp_path,
            b"Received: by mx.example.org; Mon, 08 Jan 2024 10:00:00 +0000 ("
            + b"x" * RECEIVED_DATE_MAX_CHARS
            + b")\r\n",
        )
        msg = parse_email(path)
        assert msg is not None
        assert msg.occurred_at is None
        assert len(calls) == 1  # the Date: header only


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

    # #942: every way a sender's encoded-word charset label can fail the
    # codec (unknown label, a codec that refuses ``errors="replace"``, a
    # NUL in the label) takes the same UTF-8 fallback, in Subject and in
    # the From fallback (a From with no parseable address), and logs one
    # rate-limited WARNING per word naming the exception type only.
    UNDECODABLE_CHARSET_LABELS = {
        "unknown-label": ("x-unknown-942", "LookupError"),
        "codec-rejects-replace": ("idna", "UnicodeError"),
        "nul-in-label": ("utf-8\x00", "ValueError"),
        "nul-only-label": ("\x00", "ValueError"),
    }

    @staticmethod
    def _charset_fallback_warnings(caplog) -> list[tuple[int, str]]:
        return [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if "encoded-word charset" in r.getMessage()
        ]

    @staticmethod
    def _charset_fallback_line(exc_name: str) -> tuple[int, str]:
        return (
            logging.WARNING,
            f"header encoded-word charset could not be decoded ({exc_name}); "
            "decoded 1 word as UTF-8",
        )

    @staticmethod
    def _write_header_message(tmp_path: Path, subject: bytes, from_: bytes) -> Path:
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "charset.eml"
        path.write_bytes(
            b"From: " + from_ + b"\r\n"
            b"Subject: " + subject + b"\r\n"
            b"Message-ID: <charset-942@example.test>\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Body.\r\n"
        )
        return path

    @pytest.mark.parametrize("shape", sorted(UNDECODABLE_CHARSET_LABELS))
    def test_undecodable_charset_label_subject_falls_back_to_utf8(self, tmp_path, caplog, shape):
        marker = "SUBJMARKER942"
        label, exc_name = self.UNDECODABLE_CHARSET_LABELS[shape]
        label = label.encode()
        word = b"=?" + label + b"?q?" + marker.encode() + b"_caf=C3=A9?="
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(
                self._write_header_message(tmp_path, b"Re: " + word, b"a@example.test")
            )
        assert msg is not None
        assert msg.subject == f"Re: {marker} café"
        assert msg.from_addr == "a@example.test"
        assert msg.body_text == "Body."
        assert self._charset_fallback_warnings(caplog) == [self._charset_fallback_line(exc_name)]
        assert marker not in caplog.text

    @pytest.mark.parametrize("shape", sorted(UNDECODABLE_CHARSET_LABELS))
    def test_undecodable_charset_label_from_fallback_falls_back_to_utf8(
        self, tmp_path, caplog, shape
    ):
        """``From: <word> <>`` has no address, so the whole header goes
        through ``_decode_header``, the same path as Subject."""
        marker = "FROMMARKER942"
        label, exc_name = self.UNDECODABLE_CHARSET_LABELS[shape]
        label = label.encode()
        word = b"=?" + label + b"?q?" + marker.encode() + b"_caf=C3=A9?="
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(self._write_header_message(tmp_path, b"Hello", word + b" <>"))
        assert msg is not None
        assert msg.from_addrs == []
        assert msg.from_addr == f"{marker} café <>"
        assert msg.subject == "Hello"
        assert self._charset_fallback_warnings(caplog) == [self._charset_fallback_line(exc_name)]
        assert marker not in caplog.text

    def test_nul_charset_label_filename_logs_the_charset_fallback(self, tmp_path, caplog):
        """A filename encoded-word with a NUL label decodes through the
        same fallback, so it logs the header line, not the filename
        kept-as-sent line, and never the filename."""
        marker = "FNAMEMARKER942"
        headers = (
            b"Content-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="=?utf-8\x00?q?'
            + marker.encode()
            + b'.pdf?="'
        )
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(_write_filename_message(tmp_path, headers))
        assert msg is not None
        assert [a.filename for a in msg.attachments] == [f"{marker}.pdf"]
        assert self._charset_fallback_warnings(caplog) == [
            self._charset_fallback_line("ValueError")
        ]
        assert "kept 1 filename as sent" not in caplog.text
        assert marker not in caplog.text

    def test_nul_charset_label_subject_decodes_one_call_per_word(self, tmp_path, monkeypatch):
        """#942: the fallback adds no rescan; a Subject of many NUL-label
        words costs one ``decode_header`` call per word."""
        import email.header

        calls = 0
        real = email.header.decode_header

        def counting(value):
            nonlocal calls
            calls += 1
            return real(value)

        monkeypatch.setattr(email.header, "decode_header", counting)
        words = 20_000
        subject = b" ".join([b"=?utf-8\x00?q?caf=C3=A9?="] * words)
        start = time.perf_counter()
        msg = parse_email(self._write_header_message(tmp_path, subject, b"a@example.test"))
        elapsed = time.perf_counter() - start
        assert msg is not None
        assert msg.subject == ("café" * words)[:SUBJECT_MAX_CHARS]
        assert calls == words
        assert elapsed < 10

    # #1147: compat32 labels header bytes sent without an encoded-word
    # (raw 8-bit) ``unknown-8bit``, which no codec is named. Valid UTF-8
    # under that label decodes exactly and logs nothing; anything else
    # is decoded with replacement characters and logs one rate-limited
    # WARNING with fixed text. A genuinely unknown label keeps the #942
    # line. Each shape: raw header lines, the expected parsed fields, and
    # the expected (replacement, unknown-label) WARNING counts. The
    # decoded output of every shape is what ``main`` produced before
    # #1147; only the log lines changed.
    RAW_8BIT_MARKER = "RAWMARKER1147"
    RAW_8BIT_SHAPES: dict[str, tuple[bytes, dict[str, object], tuple[int, int, int]]] = {
        "subject-raw-utf8": (
            b"Subject: RAWMARKER1147 caf\xc3\xa9\r\n",
            {"subject": "RAWMARKER1147 café"},
            (0, 0, 0),
        ),
        # Valid UTF-8 may encode U+FFFD itself: that is not a loss.
        "subject-raw-utf8-literal-fffd": (
            b"Subject: RAWMARKER1147 a\xef\xbf\xbdb\r\n",
            {"subject": "RAWMARKER1147 a\ufffdb"},
            (0, 0, 0),
        ),
        "subject-raw-latin1": (
            b"Subject: RAWMARKER1147 caf\xe9\r\n",
            {"subject": "RAWMARKER1147 caf\ufffd"},
            (1, 0, 0),
        ),
        # compat32 returns a raw 8-bit header as one ``unknown-8bit``
        # chunk, so an encoded-word next to raw bytes stays as sent
        # (pre-existing on ``main``, pinned here, #1186). Review round 1:
        # that partial decode logs its own line in Subject and the From
        # fallback; an address display name decodes the word later, so
        # it logs nothing.
        "subject-raw-utf8-and-encoded-word": (
            b"Subject: RAWMARKER1147 caf\xc3\xa9 =?utf-8?q?na=C3=AFve?=\r\n",
            {"subject": "RAWMARKER1147 café =?utf-8?q?na=C3=AFve?="},
            (0, 0, 1),
        ),
        "subject-raw-latin1-and-encoded-word": (
            b"Subject: RAWMARKER1147 caf\xe9 =?utf-8?q?na=C3=AFve?=\r\n",
            {"subject": "RAWMARKER1147 caf\ufffd =?utf-8?q?na=C3=AFve?="},
            (1, 0, 1),
        ),
        "from-raw-utf8-no-address-and-encoded-word": (
            b"From: RAWMARKER1147 Jos\xc3\xa9 =?utf-8?q?na=C3=AFve?= <>\r\n",
            {"from_addrs": [], "from_addr": "RAWMARKER1147 José =?utf-8?q?na=C3=AFve?= <>"},
            (0, 0, 1),
        ),
        "to-raw-utf8-name-and-encoded-word": (
            b"From: a@example.test\r\n"
            b"To: RAWMARKER1147 Ren\xc3\xa9e =?utf-8?q?na=C3=AFve?= <b@example.test>\r\n",
            {"to_addrs": ["RAWMARKER1147 Renée naïve <b@example.test>"]},
            (0, 0, 0),
        ),
        "from-raw-utf8-name-and-encoded-word": (
            b"From: RAWMARKER1147 Jos\xc3\xa9 =?utf-8?q?na=C3=AFve?= <a@example.test>\r\n",
            {"from_addrs": ["RAWMARKER1147 José naïve <a@example.test>"]},
            (0, 0, 0),
        ),
        # Text that only looks like the start of one is not an
        # encoded-word.
        "subject-raw-utf8-and-encoded-word-prefix": (
            b"Subject: RAWMARKER1147 caf\xc3\xa9 =?utf-8\r\n",
            {"subject": "RAWMARKER1147 café =?utf-8"},
            (0, 0, 0),
        ),
        # The label the standard library writes when it re-encodes raw
        # bytes; it means the same as the compat32 chunk label.
        "subject-encoded-word-unknown-8bit-utf8": (
            b"Subject: =?unknown-8bit?q?RAWMARKER1147_caf=C3=A9?=\r\n",
            {"subject": "RAWMARKER1147 café"},
            (0, 0, 0),
        ),
        "subject-encoded-word-unknown-8bit-upper-utf8": (
            b"Subject: =?UNKNOWN-8BIT?q?RAWMARKER1147_caf=C3=A9?=\r\n",
            {"subject": "RAWMARKER1147 café"},
            (0, 0, 0),
        ),
        "subject-encoded-word-unknown-8bit-latin1": (
            b"Subject: =?unknown-8bit?q?RAWMARKER1147_caf=E9?=\r\n",
            {"subject": "RAWMARKER1147 caf\ufffd"},
            (1, 0, 0),
        ),
        "subject-encoded-word-unknown-label": (
            b"Subject: =?x-unknown-1147?q?RAWMARKER1147_caf=C3=A9?=\r\n",
            {"subject": "RAWMARKER1147 café"},
            (0, 1, 0),
        ),
        "from-raw-utf8-name": (
            b"From: RAWMARKER1147 Jos\xc3\xa9 <a@example.test>\r\n",
            {"from_addrs": ["RAWMARKER1147 José <a@example.test>"]},
            (0, 0, 0),
        ),
        "from-raw-latin1-name": (
            b"From: RAWMARKER1147 Jos\xe9 <a@example.test>\r\n",
            {"from_addrs": ["RAWMARKER1147 Jos\ufffd <a@example.test>"]},
            (1, 0, 0),
        ),
        # No address: the header is decoded for the address parse and
        # again for the From fallback, so a loss is counted twice.
        "from-raw-utf8-no-address": (
            b"From: RAWMARKER1147 Jos\xc3\xa9 <>\r\n",
            {"from_addrs": [], "from_addr": "RAWMARKER1147 José <>"},
            (0, 0, 0),
        ),
        "from-raw-latin1-no-address": (
            b"From: RAWMARKER1147 Jos\xe9 <>\r\n",
            {"from_addrs": [], "from_addr": "RAWMARKER1147 Jos\ufffd <>"},
            (2, 0, 0),
        ),
        "to-cc-raw-utf8-names": (
            b"From: a@example.test\r\n"
            b"To: RAWMARKER1147 Ren\xc3\xa9e <b@example.test>, Z\xc3\xb6e <c@example.test>\r\n"
            b"Cc: \xc3\x85sa <d@example.test>\r\n",
            {
                "to_addrs": ["RAWMARKER1147 Renée <b@example.test>", "Zöe <c@example.test>"],
                "cc_addrs": ["Åsa <d@example.test>"],
            },
            (0, 0, 0),
        ),
        "to-cc-raw-latin1-names": (
            b"From: a@example.test\r\n"
            b"To: RAWMARKER1147 Ren\xe9e <b@example.test>\r\n"
            b"Cc: \xc5sa <d@example.test>\r\n",
            {
                "to_addrs": ["RAWMARKER1147 Ren\ufffde <b@example.test>"],
                "cc_addrs": ["\ufffdsa <d@example.test>"],
            },
            (2, 0, 0),
        ),
    }
    RAW_8BIT_REPLACED_LINE = (
        "raw 8-bit header is not UTF-8; decoded 1 header chunk with replacement characters"
    )
    RAW_8BIT_KEPT_ENCODED_LINE = (
        "raw 8-bit header holds encoded-words that were not decoded; kept 1 header as sent"
    )

    @staticmethod
    def _write_raw_header_message(tmp_path: Path, headers: bytes) -> Path:
        if not headers.startswith(b"From:") and b"\r\nFrom:" not in headers:
            headers = b"From: a@example.test\r\n" + headers
        if b"Subject:" not in headers:
            headers += b"Subject: Hello\r\n"
        folder = tmp_path / "INBOX" / "cur"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "raw8bit.eml"
        path.write_bytes(
            headers + b"Message-ID: <raw-1147@example.test>\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Body.\r\n"
        )
        return path

    @pytest.mark.parametrize("shape", sorted(RAW_8BIT_SHAPES))
    def test_raw_8bit_header_catalogue(self, tmp_path, caplog, shape):
        from src import extractors

        headers, expected, (replaced, unknown_label, kept_encoded) = self.RAW_8BIT_SHAPES[shape]
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(self._write_raw_header_message(tmp_path, headers))
        assert msg is not None
        for field, value in expected.items():
            assert getattr(msg, field) == value, field
        assert msg.body_text == "Body."
        lines = [(r.levelno, r.getMessage()) for r in caplog.records]
        assert [line for line in lines if "replacement characters" in line[1]] == [
            (logging.WARNING, self.RAW_8BIT_REPLACED_LINE)
        ] * replaced
        assert [line for line in lines if "not decoded" in line[1]] == [
            (logging.WARNING, self.RAW_8BIT_KEPT_ENCODED_LINE)
        ] * kept_encoded
        assert (
            self._charset_fallback_warnings(caplog)
            == [self._charset_fallback_line("LookupError")] * unknown_label
        )
        # Nothing was withheld, so a silent shape spent no budget.
        assert extractors.drain_suppressed_lines() == 0
        assert self.RAW_8BIT_MARKER not in caplog.text

    def test_raw_8bit_encoded_word_search_is_linear(self, tmp_path, caplog):
        """Review round 1: the search for a kept encoded-word runs over
        the whole decoded Subject before the cap. Near-miss prefixes
        (``=?a=?a...``) each fail after a constant number of steps."""
        prefixes = 300_000
        headers = b"Subject: caf\xc3\xa9 " + b"=?a" * prefixes + b"\r\n"
        start = time.perf_counter()
        with caplog.at_level(logging.DEBUG):
            msg = parse_email(self._write_raw_header_message(tmp_path, headers))
        elapsed = time.perf_counter() - start
        assert msg is not None
        assert msg.subject == ("café " + "=?a" * prefixes)[:SUBJECT_MAX_CHARS]
        assert "not decoded" not in caplog.text
        assert elapsed < 10

    def test_raw_8bit_replacement_line_is_rate_limited_and_counted(
        self, tmp_path, monkeypatch, caplog
    ):
        from src import extractors

        caplog.set_level("INFO")
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        extractors.drain_suppressed_lines()
        headers = self.RAW_8BIT_SHAPES["subject-raw-latin1"][0]
        for _ in range(5):
            msg = parse_email(self._write_raw_header_message(tmp_path, headers))
            assert msg is not None
            assert msg.subject == "RAWMARKER1147 caf\ufffd"
        lines = [r for r in caplog.records if "raw 8-bit header" in r.getMessage()]
        assert [r.levelname for r in lines] == ["WARNING", "WARNING"]
        assert extractors.drain_suppressed_lines() == 3
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 0
        assert self.RAW_8BIT_MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("chunk", "expected_text", "expected_calls"),
        [
            (b"caf\xc3\xa9 " * 200_000, "café " * 200_000, 1),
            (b"caf\xe9 " * 200_000, "caf\ufffd " * 200_000, 2),
        ],
        ids=["utf8", "latin1"],
    )
    def test_raw_8bit_chunk_is_decoded_at_most_twice(self, chunk, expected_text, expected_calls):
        """The strict UTF-8 attempt adds at most one linear decode per
        ``unknown-8bit`` chunk: one call when it succeeds, two when it
        falls back to replacement."""
        from src.parser import _decode_header_parts

        calls = 0

        class CountingBytes(bytes):
            def decode(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                return super().decode(*args, **kwargs)

        start = time.perf_counter()
        text = _decode_header_parts([(CountingBytes(chunk), "unknown-8bit")])
        elapsed = time.perf_counter() - start
        assert calls == expected_calls
        assert text == expected_text
        assert elapsed < 10

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


def _id_of(length: int, marker: str = "MSGID998MARKER") -> str:
    """A synthetic Message-ID of exactly ``length`` characters, no brackets."""
    domain = "@example.test"
    return marker + "x" * (length - len(marker) - len(domain)) + domain


def _id_eml(tmp_path: Path, headers: str, name: str = "id.eml") -> Path:
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(
        (
            "From: alice@example.com\r\nTo: bob@example.com\r\nSubject: x\r\n"
            + headers
            + "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\nbody\r\n"
        ).encode("ascii")
    )
    return path


class TestMessageIdLength:
    """A Message-ID longer than ``MESSAGE_ID_MAX_CHARS`` (998) is
    unindexable, like a missing one, so a crafted root ID cannot become
    an unbounded thread ID. The count is of the ID as stored: after the
    surrounding whitespace (including folding) and angle brackets are
    removed, so ``<`` + 998 characters + ``>`` is accepted."""

    def test_limit_is_998(self):
        assert MESSAGE_ID_MAX_CHARS == 998

    @pytest.mark.parametrize("bracketed", [True, False])
    def test_998_characters_are_accepted(self, tmp_path, bracketed):
        mid = _id_of(998)
        value = f"<{mid}>" if bracketed else mid
        msg = parse_email(_id_eml(tmp_path, f"Message-ID: {value}\r\n"))
        assert msg is not None
        assert msg.message_id == mid

    @pytest.mark.parametrize("bracketed", [True, False])
    def test_999_characters_are_unindexable(self, tmp_path, bracketed, caplog):
        mid = _id_of(999)
        value = f"<{mid}>" if bracketed else mid
        with caplog.at_level(logging.DEBUG, logger="indexer.parser"):
            assert parse_email(_id_eml(tmp_path, f"Message-ID: {value}\r\n")) is None
        assert "MSGID998MARKER" not in caplog.text

    def test_folded_header_counts_the_id_not_the_folding(self, tmp_path):
        mid = _id_of(998)
        msg = parse_email(_id_eml(tmp_path, f"Message-ID:\r\n <{mid}>\r\n"))
        assert msg is not None
        assert msg.message_id == mid

    def test_folded_over_long_id_is_unindexable(self, tmp_path):
        mid = _id_of(999)
        assert parse_email(_id_eml(tmp_path, f"Message-ID:\r\n <{mid}>\r\n")) is None

    def test_over_long_in_reply_to_is_dropped(self, tmp_path):
        headers = f"Message-ID: <m@example.test>\r\nIn-Reply-To: <{_id_of(999)}>\r\n"
        msg = parse_email(_id_eml(tmp_path, headers))
        assert msg is not None
        assert msg.in_reply_to is None

    def test_998_character_in_reply_to_is_kept(self, tmp_path):
        parent = _id_of(998)
        headers = f"Message-ID: <m@example.test>\r\nIn-Reply-To: <{parent}>\r\n"
        msg = parse_email(_id_eml(tmp_path, headers))
        assert msg is not None
        assert msg.in_reply_to == parent

    def test_over_long_references_entries_are_dropped(self, tmp_path):
        """Only the over-long entries go; the rest keep their order,
        including across folded lines."""
        kept = _id_of(998, marker="KEPT")
        headers = (
            "Message-ID: <m@example.test>\r\n"
            f"References: <a@example.test> <{_id_of(999)}>\r\n"
            f" <{kept}>\r\n <{_id_of(5000)}> <b@example.test>\r\n"
        )
        msg = parse_email(_id_eml(tmp_path, headers))
        assert msg is not None
        assert msg.references == ["a@example.test", kept, "b@example.test"]


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
# Folded address header shape catalogue (#688)
# ---------------------------------------------------------------------------
#
# Each shape is an address header with ``{eol}`` where the line is folded,
# and the (display name, address) pairs the parser records. Invariant:
# folding never changes an address, and no recorded display name keeps a
# line break: the header is unfolded (RFC 5322 2.2.3) before it is parsed.

_FOLDED_ADDRESS_SHAPES = {
    "fold-inside-quoted-name": (
        '"Jane{eol} Doe (ACME)" <jane@example.com>',
        [("Jane Doe (ACME)", "jane@example.com")],
    ),
    "fold-inside-unquoted-phrase": (
        "Jane{eol} Doe <jane@example.com>",
        [("Jane Doe", "jane@example.com")],
    ),
    "fold-between-name-and-angle-addr": (
        "Jane Doe{eol} <jane@example.com>",
        [("Jane Doe", "jane@example.com")],
    ),
    "fold-between-encoded-words": (
        "=?utf-8?q?Jane?={eol} =?utf-8?q?_Doe?= <jane@example.com>",
        [("Jane Doe", "jane@example.com")],
    ),
    "fold-between-quoted-encoded-words": (
        '"=?utf-8?q?Jane?={eol} =?utf-8?q?_Doe?=" <jane@example.com>',
        [("Jane Doe", "jane@example.com")],
    ),
    "fold-inside-comment-name": (
        "jane@example.com (Jane{eol} Doe)",
        [("Jane Doe", "jane@example.com")],
    ),
    "fold-in-each-of-several-addresses": (
        '"Ann{eol}\tLee" <ann@example.com>,{eol} "Bo{eol} Kim" <bo@example.org>, carl@example.net',
        [("Ann Lee", "ann@example.com"), ("Bo Kim", "bo@example.org"), ("", "carl@example.net")],
    ),
    "fold-inside-8bit-quoted-name": (
        '"J\u00f6rg{eol} Doe" <jorg@example.com>',
        [("J\u00f6rg Doe", "jorg@example.com")],
    ),
}


def _folded_address_eml(tmp_path: Path, header: str, eol: str) -> Path:
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "folded.eml"
    lines = [
        f"From: {header}",
        f"To: {header}",
        f"Cc: {header}",
        "Message-ID: <folded@example.test>",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
        "",
        "Body.",
        "",
    ]
    path.write_bytes(eol.join(lines).encode())
    return path


@pytest.mark.parametrize("eol", ["\r\n", "\n"], ids=["crlf", "lf"])
@pytest.mark.parametrize("shape", sorted(_FOLDED_ADDRESS_SHAPES))
def test_folded_address_header_shape_catalogue(tmp_path, shape, eol):
    from email.utils import parseaddr

    template, expected = _FOLDED_ADDRESS_SHAPES[shape]
    header = template.format(eol=eol)
    assert [parseaddr(a) for a in _parse_addrs(header)] == expected
    # End to end: as parse_email reads the header from a file (a raw
    # 8-bit header arrives as an ``email.header.Header``).
    msg = parse_email(_folded_address_eml(tmp_path, header, eol))
    assert msg is not None
    for parsed in (msg.from_addrs, msg.to_addrs, msg.cc_addrs):
        pairs = [parseaddr(a) for a in parsed]
        assert [addr for _, addr in pairs] == [addr for _, addr in expected]
        assert not any("\r" in name or "\n" in name for name, _ in pairs)
        assert pairs == expected


def test_unfolding_a_long_folded_address_header_is_one_linear_pass(monkeypatch):
    """#688: unfolding runs once over the whole header, before the split,
    so a header folded at every opportunity costs one pass, not one per
    element or per fold."""
    from email.utils import parseaddr

    from src import parser

    calls: list[int] = []
    unfold = parser._unfold

    def counting(text: str) -> str:
        calls.append(len(text))
        return unfold(text)

    monkeypatch.setattr(parser, "_unfold", counting)
    one = '"N{i}\r\n x\r\n\ty" <u{i}@example.com>,\r\n '
    header = "".join(one.format(i=i) for i in range(6000))
    assert len(header) < parser._MAX_ADDRESS_HEADER_CHARS
    started = time.perf_counter()
    out = _parse_addrs(header)
    assert time.perf_counter() - started < 10
    assert calls == [len(header)]
    assert len(out) == 6000
    assert parseaddr(out[-1]) == ("N5999 x y", "u5999@example.com")
    assert not any("\r" in a or "\n" in a for a in out)


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


# ---------------------------------------------------------------------------
# multipart/related root shape catalogue (#450)
# ---------------------------------------------------------------------------
#
# Invariant: a multipart/related contributes only its first child, and
# contributes nothing when that child is presented as an attachment (a
# filename or an attachment disposition); a later part is never promoted
# to the root. The standard library's ``get_body()`` also takes the first
# part as the root candidate and never a later one (it differs only on an
# inline root with a filename, which this parser treats as an attachment
# everywhere). A ``start`` parameter naming another root is not read
# (pinned below).

_CID_IMG = ("image/png", "iVBORw0=", {"Content-ID": "<img1@example.test>"})
_BARE_IMG = ("image/png", "iVBORw0=", {})
_ROOT_ATTACHMENTS = {
    "named-attachment": _plain("ROOT_MARKER", 'attachment; filename="r.txt"'),
    "nameless-attachment": _plain("ROOT_MARKER", "attachment"),
    "named-inline": _plain("ROOT_MARKER", 'inline; filename="r.txt"'),
    "named-html": (
        "text/html",
        "<p>ROOT_MARKER</p>",
        {"Content-Disposition": 'inline; filename="r.html"'},
    ),
    "image-with-filename": _IMG,
    "pdf": _PDF,
    "forwarded-email": _FORWARDED,
}
_RESOURCE = ("text/html", "<p>RESOURCE</p>", {"Content-ID": "<res@example.test>"})

_RELATED_SHAPES = {
    # Shapes handled correctly before #450; the expected bodies pin that.
    "related-html-root-cid-image": (_multi("related", _html("H1"), _CID_IMG), "H1"),
    "related-html-root-bare-image": (_multi("related", _html("H1"), _BARE_IMG), "H1"),
    "related-plain-root-cid-resource": (_multi("related", _plain("P1"), _RESOURCE), "P1"),
    "related-8bit-root": (_multi("related", _html("café H1"), _CID_IMG), "café H1"),
    "related-empty": (_multi("related"), ""),
    "related-cid-image-root": (_multi("related", _CID_IMG, _RESOURCE), ""),
    "related-bare-image-root": (_multi("related", _BARE_IMG, _RESOURCE), ""),
    "mixed-related-root-then-attachment": (
        _multi("mixed", _multi("related", _html("H1"), _CID_IMG), _PDF),
        "H1",
    ),
    "mixed-plain-then-related": (
        _multi("mixed", _plain("P1"), _multi("related", _html("H2"), _RESOURCE)),
        "P1\n\nH2",
    ),
    "alt-plain-related-attachment-root": (
        _multi("alternative", _plain("P1"), _multi("related", _PDF, _RESOURCE)),
        "P1",
    ),
    # Not read: the root is the first child even when ``start`` names
    # another part.
    "related-start-names-second-part": (
        (
            'multipart/related; start="<root@example.test>"',
            [_html("FIRST"), ("text/html", "<p>NAMED</p>", {"Content-ID": "<root@example.test>"})],
            {},
        ),
        "FIRST",
    ),
}
# The #450 class: each root presented as an attachment, followed by a
# filename-less text resource, alone and nested.
for _name, _root in _ROOT_ATTACHMENTS.items():
    _RELATED_SHAPES[f"related-{_name}-root"] = (_multi("related", _root, _RESOURCE), "")
    _RELATED_SHAPES[f"mixed-related-{_name}-root-then-plain"] = (
        _multi("mixed", _multi("related", _root, _RESOURCE), _plain("P2")),
        "P2",
    )
    _RELATED_SHAPES[f"alt-blank-plain-related-{_name}-root"] = (
        _multi("alternative", _plain(""), _multi("related", _root, _RESOURCE)),
        "",
    )


@pytest.mark.parametrize(
    ("shape", "expected"), _RELATED_SHAPES.values(), ids=_RELATED_SHAPES.keys()
)
def test_related_root_shape_catalogue(tmp_path, caplog, shape, expected):
    """#450: a related whose root was presented as an attachment got no
    body node, so its next text resource was taken as the root."""
    raw = (
        "Message-ID: <related@example.test>\r\nFrom: sender@example.test\r\n"
        "Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
    ) + _render(shape, [0])
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "related.eml"
    path.write_bytes(raw.encode("utf-8"))
    with caplog.at_level(logging.DEBUG):
        msg = parse_email(path)
    assert msg is not None
    assert msg.body_text == expected
    for marker in ("ROOT_MARKER", "RESOURCE", "FORWARDED_MARKER"):
        assert marker not in msg.body_text
        assert marker not in caplog.text


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
    # undecoded; it is decoded as Subject is (#924).
    "encoded-word": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?r=C3=A9sum=C3=A9.pdf?="',
        "résumé.pdf",
    ),
    # #688: a header folded inside the filename parameter. Unfolding
    # (RFC 5322 2.2.3) removes the line break and keeps the whitespace
    # after it; the standard library alone kept both.
    "fold-quoted-crlf": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report\r\n 10-22.pdf"',
        "report 10-22.pdf",
    ),
    "fold-quoted-lf": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report\n 10-22.pdf"',
        "report 10-22.pdf",
    ),
    "fold-quoted-tab": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="report\r\n\t10-22.pdf"',
        "report\t10-22.pdf",
    ),
    "fold-rfc2231-continuation": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment;\r\n filename*0="rep\r\n ort";\r\n filename*1="x.pdf"',
        "rep ortx.pdf",
    ),
    "fold-rfc2231-extended": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''rep\r\n ort.pdf",
        "rep ort.pdf",
    ),
    "fold-rfc2231-extended-continuation": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*0*=utf-8''rep%20\r\n ort;\r\n filename*1*=x.pdf",
        "rep  ortx.pdf",
    ),
    "fold-rfc2231-idna": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=idna''rep\n ort.pdf",
        "rep ort.pdf",
    ),
    "fold-content-type-name": (
        b'Content-Type: application/pdf; name="rep\r\n ort.pdf"',
        "rep ort.pdf",
    ),
    "fold-between-encoded-words": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?rep?=\r\n =?utf-8?q?ort.pdf?="',
        "report.pdf",
    ),
    # #688 review round 1: only syntactic folds are removed. A line break
    # an RFC 2231 value percent-encodes is filename content and survives.
    "rfc2231-percent-encoded-lf-space": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''a%0A%20b.txt",
        "a\n b.txt",
    ),
    "rfc2231-percent-encoded-crlf-space": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''a%0D%0A%20b.txt",
        "a\r\n b.txt",
    ),
    "rfc2231-percent-encoded-lf-space-idna": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=idna''a%0A%20b.txt",
        "a\n b.txt",
    ),
    "8bit-raw": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="r\xc3\xa9sum\xc3\xa9.pdf"',
        "r\ufffd\ufffdsum\ufffd\ufffd.pdf",
    ),
    "fold-8bit-raw": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="r\xc3\xa9sum\xc3\xa9\r\n .pdf"',
        "r\ufffd\ufffdsum\ufffd\ufffd .pdf",
    ),
    "no-filename": (
        b"Content-Type: application/pdf\r\nContent-Disposition: attachment",
        "unnamed",
    ),
    "empty-filename": (
        b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename=""',
        "unnamed",
    ),
    # #924: encoded-words the standard library leaves in a parameter.
    "encoded-word-two-adjacent": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?caf=C3=A9_?= =?utf-8?q?men=C3=BC.pdf?="',
        "café menü.pdf",
    ),
    "encoded-word-b-folded-unquoted-name": (
        b"Content-Type: image/png;\r\n name==?utf-8?B?Y2Fmw6kg?=\r\n =?utf-8?B?bWVuw7wucG5n?=",
        "café menü.png",
    ),
    "encoded-word-not-adjacent": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="Q3 =?utf-8?q?caf=C3=A9?= notes.pdf"',
        "Q3 café notes.pdf",
    ),
    "encoded-word-content-type-name": (
        b'Content-Type: application/pdf; name="=?utf-8?q?r=C3=A9sum=C3=A9.pdf?="',
        "résumé.pdf",
    ),
    "encoded-word-in-rfc2231": (
        b"Content-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename*=utf-8''%3D%3Futf-8%3Fq%3Fcaf%3DC3%3DA9.pdf%3F%3D",
        "café.pdf",
    ),
    "encoded-word-after-8bit": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="r\xc3\xa9 =?utf-8?q?caf=C3=A9.pdf?="',
        "r\ufffd\ufffd café.pdf",
    ),
    "encoded-word-unknown-charset": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?x-unknown-924?q?caf=C3=A9.pdf?="',
        "café.pdf",
    ),
    "encoded-word-malformed": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8?q?caf=C3=A9.pdf"',
        "=?utf-8?q?caf=C3=A9.pdf",
    ),
    "encoded-word-nul-in-charset": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?utf-8\x00?q?caf=C3=A9.pdf?="',
        # #942: decoded like Subject, with the unknown-label UTF-8 fallback.
        "café.pdf",
    ),
    "encoded-word-lone-surrogate": (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="=?unicode_escape?q?a=5Cud83d.pdf?="',
        "=?unicode_escape?q?a=5Cud83d.pdf?=",
    ),
    "encoded-word-decodes-empty": (
        b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="=?utf-8?q?_?="',
        "=?utf-8?q?_?=",
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
    before = (part.as_bytes(), list(part.raw_items()))
    # #688: the standard library reads the header as folded. Its value on
    # the same header unfolded first (RFC 5322 2.2.3) is the ground truth.
    headers = re.sub(rb"\r?\n(?=[ \t])", b"", headers)
    try:
        stdlib = email.message_from_bytes(headers + b"\r\n\r\nAAAA\r\n").get_filename()
    except ValueError:
        unknown = headers
        for label in (b"idna", b"undefined", b"utf-8\x00"):
            unknown = unknown.replace(label + b"''", b"x-unknown-362''")
        assert unknown != headers
        stdlib = email.message_from_bytes(unknown + b"\r\n\r\nAAAA\r\n").get_filename()
    # #924: encoded-words the standard library leaves in the value are
    # decoded the same way as Subject; where that raises, is not valid
    # Unicode or leaves nothing, the standard library's value is kept.
    if stdlib and "=?" in stdlib:
        try:
            subject = _decode_header(email.message_from_string(f"Subject: {stdlib}\n\n")["Subject"])
            subject.encode("utf-8")
        except ValueError, LookupError, email.errors.HeaderParseError:
            subject = ""
        stdlib = subject or stdlib
    assert _part_filename(part) == stdlib
    if shape.startswith("fold-"):
        assert stdlib is not None
        assert "\r" not in stdlib and "\n" not in stdlib
    # The part itself is never changed: attached emails are re-serialized.
    assert (part.as_bytes(), list(part.raw_items())) == before


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


@pytest.mark.parametrize(
    ("charset", "text", "exc_name"),
    [
        ("unicode_escape", "{}=5Cud83d.pdf", "UnicodeEncodeError"),
    ],
)
def test_undecodable_filename_encoded_word_is_kept_and_not_logged(
    tmp_path, caplog, charset, text, exc_name
):
    """#924: an encoded-word filename that does not decode keeps the
    standard library's value and logs one fixed WARNING naming the
    exception type, never the filename."""
    marker = "FNAMEMARKER924"
    raw = f"=?{charset}?q?{text.format(marker)}?="
    headers = (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="' + raw.encode() + b'"'
    )
    with caplog.at_level(logging.DEBUG):
        msg = parse_email(_write_filename_message(tmp_path, headers))
    assert msg is not None
    assert [a.filename for a in msg.attachments] == [raw]
    warnings = [r for r in caplog.records if "filename" in r.getMessage()]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert warnings[0].getMessage() == (
        f"attachment filename encoded-words could not be decoded ({exc_name}); "
        "kept 1 filename as sent"
    )
    assert marker not in caplog.text


def test_filename_encoded_word_decoding_is_one_call_per_word(tmp_path, monkeypatch):
    """#924: a crafted filename of many encoded-words is decoded in one
    linear pass: one ``decode_header`` call per word, never a rescan of
    the rest of the value, and none for a flood of ``=?`` prefixes that
    are not encoded-words."""
    import email.header

    calls = 0
    real = email.header.decode_header

    def counting(value):
        nonlocal calls
        calls += 1
        return real(value)

    monkeypatch.setattr(email.header, "decode_header", counting)
    words = 20_000
    crafted = " ".join(["=?utf-8?q?caf=C3=A9?="] * words)
    headers = (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="' + crafted.encode() + b'"'
    )
    start = time.perf_counter()
    msg = parse_email(_write_filename_message(tmp_path / "words", headers))
    elapsed = time.perf_counter() - start
    assert msg is not None
    assert [a.filename for a in msg.attachments] == ["café" * words]
    assert calls == words
    assert elapsed < 10

    calls = 0
    flood = "=?" * 200_000 + ".pdf"
    headers = (
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="' + flood.encode() + b'"'
    )
    start = time.perf_counter()
    msg = parse_email(_write_filename_message(tmp_path / "flood", headers))
    elapsed = time.perf_counter() - start
    assert msg is not None
    assert [a.filename for a in msg.attachments] == [flood]
    assert calls == 0
    assert elapsed < 10


class TestMessageSortTime:
    """``message_sort_time`` orders a first full index (#699): the
    message's effective time from its header block alone."""

    def _write(self, path, headers: list[str], body: bytes = b"body\r\n") -> None:
        path.write_bytes("\r\n".join(headers).encode() + b"\r\n\r\n" + body)

    def test_prefers_the_top_received_date(self, tmp_path):
        from src.parser import message_sort_time

        path = tmp_path / "m"
        self._write(
            path,
            [
                "Received: from a by mail.example.org; Tue, 02 Jan 2024 10:00:00 +0000",
                "Received: from b by c; Mon, 01 Jan 2024 09:00:00 +0000",
                "Date: Sun, 31 Dec 2023 08:00:00 +0000",
                "Message-ID: <x@example.com>",
            ],
        )
        assert message_sort_time(path) == datetime(2024, 1, 2, 10, 0, tzinfo=UTC)

    def test_falls_back_to_date_like_sent_mail(self, tmp_path):
        from src.parser import message_sort_time

        path = tmp_path / "m"
        self._write(path, ["Date: Sun, 31 Dec 2023 08:00:00 -0500", "Message-ID: <x@example.com>"])
        assert message_sort_time(path) == datetime(2023, 12, 31, 13, 0, tzinfo=UTC)

    def test_matches_parse_email_effective_date(self, tmp_path):
        from src.parser import message_sort_time, parse_email

        for name, headers in (
            ("received", ["Received: from a by b; Tue, 02 Jan 2024 10:00:00 +0000"]),
            ("date", []),
        ):
            path = tmp_path / name
            self._write(
                path,
                [
                    *headers,
                    "Date: Sun, 31 Dec 2023 08:00:00 +0000",
                    "Message-ID: <x@example.com>",
                    "From: a@example.com",
                ],
            )
            message = parse_email(path, tmp_path)
            assert message is not None
            assert message_sort_time(path) == message.effective_date

    def test_no_usable_date_is_none(self, tmp_path):
        from src.parser import message_sort_time

        path = tmp_path / "m"
        self._write(path, ["Date: not a date", "Message-ID: <x@example.com>"])
        assert message_sort_time(path) is None
        assert message_sort_time(tmp_path / "missing") is None

    def test_reads_only_the_header_block(self, tmp_path, monkeypatch):
        """A large body is not read: the read stops at the blank line."""
        from src import parser

        path = tmp_path / "m"
        self._write(path, ["Date: Sun, 31 Dec 2023 08:00:00 +0000"], body=b"x" * (4 * 1024 * 1024))
        read = _count_reads(monkeypatch)
        assert parser.message_sort_time(path) is not None
        assert sum(read) <= parser._SORT_READ_CHUNK

    def test_a_huge_header_block_is_read_up_to_the_cap(self, tmp_path, monkeypatch):
        """Synthetic worst case: an 8 MB folded header and no blank
        line. The read stops at ``SORT_HEADER_MAX_BYTES``, quickly."""
        import time

        from src import parser

        path = tmp_path / "m"
        path.write_bytes(
            b"Date: Sun, 31 Dec 2023 08:00:00 +0000\r\nX-Pad: "
            + b"a\r\n b" * (8 * 1024 * 1024 // 5)
        )
        read = _count_reads(monkeypatch)
        started = time.monotonic()
        assert parser.message_sort_time(path) is not None
        assert time.monotonic() - started < 2
        assert sum(read) == parser.SORT_HEADER_MAX_BYTES


def _count_reads(monkeypatch) -> list[int]:
    """Record the size of every read ``message_sort_time`` makes."""
    import builtins

    sizes: list[int] = []
    real_open = builtins.open

    class _Counting:
        def __init__(self, f):
            self._f = f

        def read(self, n=-1):
            data = self._f.read(n)
            sizes.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()

    def counting_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        return _Counting(f) if "b" in mode else f

    monkeypatch.setattr("src.parser.open", counting_open, raising=False)
    return sizes


# ---------------------------------------------------------------------------
# Parser work caps are logged (#872)
# ---------------------------------------------------------------------------

_CAP_HEAD = (
    b"Message-ID: <caps@example.test>\r\nFrom: sender@example.test\r\n"
    b"Date: Mon, 28 Sep 2026 12:00:00 +0000\r\nMIME-Version: 1.0\r\n"
)
_CAP_FILENAME = b'Content-Disposition: attachment; filename="SYNTHETIC_FILENAME_MARKER.eml"\r\n'
# A container named ``.txt`` is extracted as text, so an empty payload
# loses that text; one named ``.eml`` selects no extractor (review round 1).
_TXT_FILENAME = b'Content-Disposition: attachment; filename="SYNTHETIC_FILENAME_MARKER.txt"\r\n'


def _with_attachment(headers: bytes, body: bytes, disposition: bytes = _CAP_FILENAME) -> bytes:
    """A message whose body is PARENT_BODY plus one attachment part with
    ``headers`` (beside the marker filename) and ``body``."""
    return (
        _CAP_HEAD + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
        b"--b\r\n" + headers + disposition + b"\r\n" + body + b"\r\n--b--\r\n"
    )


def _nested_rfc822(levels: int) -> bytes:
    leaf = b"Content-Type: text/plain\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
    return b"Content-Type: message/rfc822\r\n\r\n" * (levels - 1) + leaf


def _addresses(to: bytes) -> bytes:
    return _CAP_HEAD + b"To: " + to + b"\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"


_INNER_EMAIL = (
    b"From: a@example.test\r\nSubject: SYNTHETIC_HEADER_MARKER\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
)
_LONG_LOCAL = b"SYNTHETIC_HEADER_MARKER" + b"x" * 1000
_LONG_SUBJECT = b"SYNTHETIC_HEADER_MARKER" + b"s" * 3000
_LONG_ID = _id_of(999, marker="SYNTHETIC_HEADER_MARKER")


def _headers(extra: bytes) -> bytes:
    """A plain-text message with ``extra`` (CRLF-terminated) header lines."""
    return _CAP_HEAD + extra + b"Content-Type: text/plain\r\n\r\nPARENT_BODY\r\n"


def _only_attachment_is_empty(msg) -> bool:
    return msg.body_text == "PARENT_BODY" and [a.payload for a in msg.attachments] == [b""]


_BASE64_RFC822 = b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n"


def _base64_chain(levels: int) -> bytes:
    """An attached email holding a base64 attached email, ``levels`` deep."""
    inner = _INNER_EMAIL
    for i in range(levels):
        inner = (
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="c%d"\r\n\r\n'
            b"--c%d\r\n"
            % (i, i)
            + _BASE64_RFC822
            + _CAP_FILENAME
            + b"\r\n"
            + base64.encodebytes(inner)
            + b"--c%d--\r\n" % i
        )
    return _with_attachment(_BASE64_RFC822, base64.encodebytes(inner))


def _chain_stops_at_the_depth_cap(msg) -> bool:
    # The top attachment and one per decoded level are recorded, the last
    # at the decode-depth cap and never decoded; only the top one is
    # serialized.
    payloads = [a.payload for a in msg.attachments]
    return (
        msg.body_text == "PARENT_BODY"
        and len(payloads) == 21
        and payloads[0] != b""
        and payloads[1:] == [b""] * 20
    )


# Each shape fires one cap. ``small_decode_budget`` lowers the decoded-byte
# budget (64 MB by default) so a small fixture can exhaust it; a dict
# instead sets those parser limits for the shape (the #1144 address
# budget, so a few short headers can exhaust it). ``pinned`` is the
# parse result before the caps were logged.
def _nested_rfc822_with_note(levels: int) -> bytes:
    """``levels`` identity-encoded attached emails around a message that
    carries a ``note.txt`` attachment."""
    leaf = (
        b'Content-Type: multipart/mixed; boundary="n"\r\n\r\n'
        b"--n\r\nContent-Type: text/plain\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
        b'--n\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename="note.txt"'
        b"\r\n\r\nNOTE_BODY\r\n--n--\r\n"
    )
    return b"Content-Type: message/rfc822\r\n\r\n" * (levels - 1) + leaf


_CAP_SHAPES = {
    "attached_depth": (
        _with_attachment(b"Content-Type: message/rfc822\r\n", _nested_rfc822(21), _TXT_FILENAME),
        False,
        "attached_depth=1",
        _only_attachment_is_empty,
    ),
    "attached_fields": (
        _with_attachment(
            b"Content-Type: message/delivery-status\r\n",
            b"Reporting-MTA: dns; mx.example.test\r\n"
            + b"X-Field: SYNTHETIC_HEADER_MARKER\r\n" * 20_000,
            _TXT_FILENAME,
        ),
        False,
        "attached_fields=1",
        _only_attachment_is_empty,
    ),
    "attached_depth_decoded": (
        _with_attachment(
            _BASE64_RFC822, base64.encodebytes(b"MIME-Version: 1.0\r\n" + _nested_rfc822(21))
        ),
        False,
        "attached_depth=1",
        _only_attachment_is_empty,
    ),
    "attached_depth_decode_chain": (
        _base64_chain(25),
        False,
        "attached_depth=1",
        _chain_stops_at_the_depth_cap,
    ),
    "transport_decode_base64": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n", b"A"
        ),
        False,
        "transport_decode=1",
        _only_attachment_is_empty,
    ),
    "transport_decode_8bit": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: quoted-printable\r\n",
            b"From: a@example.test\r\nSubject: SYNTHETIC_HEADER_MARKER caf\xc3\xa9\r\n\r\nhello",
        ),
        False,
        "transport_decode=1",
        _only_attachment_is_empty,
    ),
    "decoded_bytes": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n",
            base64.encodebytes(_INNER_EMAIL),
        ),
        True,
        "decoded_bytes=1",
        _only_attachment_is_empty,
    ),
    "container_serialize": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\n",
            b"From: a@example.test\r\nX: SYNTHETIC_HEADER_MARKER\x0brest\r\n\r\nhello",
            _TXT_FILENAME,
        ),
        False,
        "container_serialize=1",
        _only_attachment_is_empty,
    ),
    "container_serialize_decoded": (
        _with_attachment(
            _BASE64_RFC822,
            base64.encodebytes(
                b"From: a@example.test\r\nX: SYNTHETIC_HEADER_MARKER\x0brest\r\n\r\nhello"
            ),
            _TXT_FILENAME,
        ),
        False,
        "container_serialize=1",
        _only_attachment_is_empty,
    ),
    # #922: an attached email's payload is read by the ``eml``
    # extractor, so one a cap emptied loses its text even when it is
    # named ``.eml`` and the walk still keeps the attachments inside it.
    "attached_depth_keeps_inner_attachment": (
        _with_attachment(b"Content-Type: message/rfc822\r\n", _nested_rfc822_with_note(21)),
        False,
        "attached_depth=1",
        lambda msg: (
            [(a.filename, a.payload) for a in msg.attachments]
            == [("SYNTHETIC_FILENAME_MARKER.eml", b""), ("note.txt", b"NOTE_BODY")]
        ),
    ),
    "attached_fields_eml": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\n",
            b"From: a@example.test\r\n" + b"X-Field: SYNTHETIC_HEADER_MARKER\r\n" * 20_000,
        ),
        False,
        "attached_fields=1",
        _only_attachment_is_empty,
    ),
    "container_serialize_eml": (
        _with_attachment(
            b"Content-Type: message/rfc822\r\n",
            b"From: a@example.test\r\nX: SYNTHETIC_HEADER_MARKER\x0brest\r\n\r\nhello",
        ),
        False,
        "container_serialize=1",
        _only_attachment_is_empty,
    ),
    "container_serialize_decoded_eml": (
        _with_attachment(
            _BASE64_RFC822,
            base64.encodebytes(
                b"From: a@example.test\r\nX: SYNTHETIC_HEADER_MARKER\x0brest\r\n\r\nhello"
            ),
        ),
        False,
        "container_serialize=1",
        _only_attachment_is_empty,
    ),
    "body_parts": (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"".join(b"--b\r\nContent-Type: text/plain\r\n\r\nS%d\r\n" % i for i in range(205))
        + b"--b--\r\n",
        False,
        "body_parts=5",
        lambda msg: msg.body_text.split("\n\n") == [f"S{i}" for i in range(200)],
    ),
    # #996: the walk stops at MAX_WALKED_PARTS (the root counts as one),
    # so the attachment after the cap is never recorded.
    "mime_parts": (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: text/plain\r\n\r\nS0\r\n"
        + b"--b\r\nContent-Type: application/x-empty\r\n\r\n\r\n" * 9_998
        + b"--b\r\nContent-Type: text/plain\r\n"
        + _TXT_FILENAME
        + b"\r\nSYNTHETIC_TEXT_MARKER\r\n"
        + b"--b--\r\n",
        False,
        "mime_parts=1",
        lambda msg: msg.body_text == "S0" and msg.attachments == [],
    ),
    "address_header": (
        _addresses(b"bob@example.test, " + b"SYNTHETIC_HEADER_MARKER@example.test, " * 7_000),
        False,
        "address_header=1",
        lambda msg: msg.to_addrs == [] and msg.body_text == "PARENT_BODY",
    ),
    "address_element": (
        _addresses(b"bob@example.test, " + b"SYNTHETIC_HEADER_MARKER" * 6_000 + b"@example.test"),
        False,
        "address_element=1",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    "address_length": (
        _addresses(_LONG_LOCAL + b"@example.test, bob@example.test, " + _LONG_LOCAL + b"@x.test"),
        False,
        "address_length=2",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    # #1144: the per-message address budget. ``_CAP_HEAD`` holds four
    # fields, one of them From.
    "address_fields": (
        _addresses(b"SYNTHETIC_HEADER_MARKER@example.test"),
        {"MAX_ADDRESS_HEADER_FIELDS": 4},
        "address_fields=1",
        lambda msg: (
            msg.to_addrs == [] and msg.from_addr == "sender@example.test" and msg.sender_ambiguous
        ),
    ),
    "address_occurrences": (
        _addresses(b"bob@example.test\r\nTo: SYNTHETIC_HEADER_MARKER@example.test"),
        {"MAX_ADDRESS_OCCURRENCES": 2},
        "address_occurrences=1",
        lambda msg: msg.to_addrs == ["bob@example.test"] and not msg.sender_ambiguous,
    ),
    "address_chars": (
        _addresses(b"bob@example.test\r\nTo: SYNTHETIC_HEADER_MARKER@example.test"),
        {"_MAX_ADDRESS_TOTAL_CHARS": 40},
        "address_chars=1",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    "address_elements": (
        _addresses(b"bob@example.test, SYNTHETIC_HEADER_MARKER@example.test, c@example.test"),
        {"MAX_ADDRESS_ELEMENTS": 2},
        "address_elements=2",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    "address_count": (
        _addresses(b"bob@example.test, SYNTHETIC_HEADER_MARKER@example.test, c@example.test"),
        {"MAX_MESSAGE_ADDRESSES": 2},
        "address_count=2",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    # #1086: an element that yields no address the participant rows
    # can store (a comment only, here) is dropped, as before, and now
    # counted.
    "address_unparsed": (
        _addresses(b"bob@example.test, (SYNTHETIC_HEADER_MARKER)"),
        False,
        "address_unparsed=1",
        lambda msg: msg.to_addrs == ["bob@example.test"],
    ),
    # #1140: display names past the first for a participant spend one
    # per-message budget; the recipient is kept.
    "participant_names": (
        _addresses(b"Bob <bob@example.test>, SYNTHETIC_HEADER_MARKER <bob@example.test>"),
        {"MAX_EXTRA_PARTICIPANT_NAMES": 0},
        "participant_names=1",
        lambda msg: (
            len(msg.to_addrs) == 2 and msg.participant_names == [("to", "bob@example.test", "Bob")]
        ),
    ),
    # #902: the header caps. The subject is cut to ``SUBJECT_MAX_CHARS``;
    # an In-Reply-To or References entry over ``MESSAGE_ID_MAX_CHARS`` is
    # dropped and threading sees the rest.
    "subject_length": (
        _headers(b"Subject: " + _LONG_SUBJECT + b"\r\n"),
        False,
        "subject_length=1",
        lambda msg: msg.subject == _LONG_SUBJECT.decode()[:SUBJECT_MAX_CHARS],
    ),
    "in_reply_to_length": (
        _headers(f"In-Reply-To: <{_LONG_ID}>\r\n".encode()),
        False,
        "in_reply_to_length=1",
        lambda msg: msg.in_reply_to is None and msg.body_text == "PARENT_BODY",
    ),
    "references_length": (
        _headers(
            f"References: <a@example.test> <{_LONG_ID}>\r\n"
            f" <b@example.test> <{_id_of(5000, marker='SYNTHETIC_HEADER_MARKER')}>\r\n".encode()
        ),
        False,
        "references_length=2",
        lambda msg: msg.references == ["a@example.test", "b@example.test"],
    ),
}


def _parse_cap_shape(tmp_path, monkeypatch, shape: str):
    from src import parser

    raw, small_decode_budget, _, _ = _CAP_SHAPES[shape]
    if isinstance(small_decode_budget, dict):
        for name, limit in small_decode_budget.items():
            monkeypatch.setattr(parser, name, limit)
    elif small_decode_budget:
        budget = parser._SerializationBudget
        monkeypatch.setattr(parser, "_SerializationBudget", lambda: budget(decodable=10))
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "caps.eml"
    path.write_bytes(raw)
    msg = parse_email(path)
    assert msg is not None
    return msg, path


@pytest.mark.parametrize("shape", sorted(_CAP_SHAPES))
def test_cap_shape_parse_result_is_pinned(tmp_path, monkeypatch, shape):
    """The parse result each cap produces, pinned before the caps were
    logged: counting them must not change it."""
    msg, _ = _parse_cap_shape(tmp_path, monkeypatch, shape)
    assert _CAP_SHAPES[shape][3](msg)


@pytest.mark.parametrize("shape", sorted(_CAP_SHAPES))
def test_cap_that_drops_content_logs_one_warning(tmp_path, monkeypatch, caplog, shape):
    """#872: a message a work cap cut is logged once with its Maildir
    path and the caps that fired, by fixed name and count; no header
    value, filename or text. WARNING, since content is lost (review
    round 1)."""
    caplog.set_level("DEBUG")
    _, path = _parse_cap_shape(tmp_path, monkeypatch, shape)
    lines = [r for r in caplog.records if "parser work caps" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        ("WARNING", f"parser work caps dropped content from {path}: {_CAP_SHAPES[shape][2]}")
    ]
    for marker in ("SYNTHETIC_HEADER_MARKER", "SYNTHETIC_FILENAME_MARKER", "SYNTHETIC_TEXT_MARKER"):
        assert marker not in caplog.text


# #1086: the completeness flags each cap shape clears; every other flag
# stays True. The address caps clear the role they fired in (To in
# these shapes), except the header scan cut short, which loses later
# headers of every role.
_ROLES = {"from_addresses_complete", "to_addresses_complete", "cc_addresses_complete"}
_MANIFEST = {"attachments_manifest_complete"}
_CAP_COMPLETENESS: dict[str, set[str]] = {
    "attached_depth": _MANIFEST,
    "attached_fields": _MANIFEST,
    "attached_depth_decoded": _MANIFEST,
    "attached_depth_decode_chain": _MANIFEST,
    "transport_decode_base64": _MANIFEST,
    "transport_decode_8bit": _MANIFEST,
    "decoded_bytes": _MANIFEST,
    "container_serialize": _MANIFEST,
    "container_serialize_decoded": _MANIFEST,
    "attached_depth_keeps_inner_attachment": _MANIFEST,
    "attached_fields_eml": _MANIFEST,
    "container_serialize_eml": _MANIFEST,
    "container_serialize_decoded_eml": _MANIFEST,
    "body_parts": {"body_complete"},
    "mime_parts": {"body_complete", *_MANIFEST},
    "address_header": {"to_addresses_complete"},
    "address_element": {"to_addresses_complete"},
    "address_length": {"to_addresses_complete"},
    "address_fields": _ROLES,
    "address_occurrences": {"to_addresses_complete"},
    "address_chars": {"to_addresses_complete"},
    "address_elements": {"to_addresses_complete"},
    "address_count": {"to_addresses_complete"},
    "address_unparsed": {"to_addresses_complete"},
    # Covered by ``participant_names_complete`` (#1140), not by these.
    "participant_names": set(),
    "subject_length": {"subject_complete"},
    # Threading input only; no filter reads them.
    "in_reply_to_length": set(),
    "references_length": set(),
}
_COMPLETENESS_FLAGS = (
    "subject_complete",
    "from_addresses_complete",
    "to_addresses_complete",
    "cc_addresses_complete",
    "attachments_manifest_complete",
    "body_complete",
)


def _completeness(msg) -> dict[str, bool]:
    return {name: getattr(msg, name) for name in _COMPLETENESS_FLAGS}


def test_every_cap_shape_states_its_completeness():
    assert set(_CAP_COMPLETENESS) == set(_CAP_SHAPES)


@pytest.mark.parametrize("shape", sorted(_CAP_SHAPES))
def test_cap_shape_clears_exactly_its_completeness_flags(tmp_path, monkeypatch, caplog, shape):
    """#1086: a cap that loses content clears the flag of what it lost,
    and only that; ``parse_caps`` holds the names and counts the cap
    line shows, never content."""
    caplog.set_level("DEBUG")
    msg, _ = _parse_cap_shape(tmp_path, monkeypatch, shape)
    cleared = _CAP_COMPLETENESS[shape]
    assert _completeness(msg) == {name: name not in cleared for name in _COMPLETENESS_FLAGS}
    expected = dict(
        (name, int(count)) for name, count in re.findall(r"(\w+)=(\d+)", _CAP_SHAPES[shape][2])
    )
    assert msg.parse_caps == expected
    assert set(msg.parse_caps) <= set(PARSE_CAPS)
    for marker in ("SYNTHETIC_HEADER_MARKER", "SYNTHETIC_FILENAME_MARKER", "SYNTHETIC_TEXT_MARKER"):
        assert marker not in repr(msg.parse_caps)
        assert marker not in caplog.text


class TestCompletenessByRole:
    """#1086: an address loss clears the completeness flag of the role
    it happened in, never another's."""

    def test_a_plain_message_is_complete_with_no_caps(self):
        msg, _ = _parse_headers(
            b"From: a@example.test\r\nTo: b@example.test\r\nCc: c@example.test\r\n"
        )
        assert all(_completeness(msg).values())
        assert msg.parse_caps == {}

    def test_absent_headers_lose_nothing(self):
        msg, _ = _parse_headers(b"")
        assert all(_completeness(msg).values())

    def test_repeated_to_and_cc_lose_nothing(self):
        msg, _ = _parse_headers(
            b"From: a@example.test\r\nTo: b@example.test\r\nTo: c@example.test\r\n"
            b"Cc: d@example.test\r\nCc: e@example.test\r\n"
        )
        assert all(_completeness(msg).values())
        assert msg.parse_caps == {"address_repeated": 2}

    def test_a_repeated_from_is_an_incomplete_from_list(self):
        """Only the first From header is parsed, so the others'
        addresses are not stored."""
        msg, _ = _parse_headers(
            b"From: a@example.test\r\nFrom: b@example.test\r\nTo: c@example.test\r\n"
        )
        assert msg.from_addresses_complete is False
        assert (msg.to_addresses_complete, msg.cc_addresses_complete) == (True, True)
        assert msg.parse_caps == {"from_repeated": 1}

    @pytest.mark.parametrize("role", ["From", "To", "Cc"])
    @pytest.mark.parametrize(
        "value",
        [
            # Over ``_MAX_ADDRESS_HEADER_CHARS``: the whole header lost.
            b"x@example.test, " + b"SYNTHETIC_HEADER_MARKER@example.test, " * 7_000,
            # One element over ``_MAX_ADDRESS_ELEMENT_CHARS``.
            b"x@example.test, " + b"SYNTHETIC_HEADER_MARKER" * 6_000 + b"@example.test",
            # One address over ``_MAX_ADDRESS_CHARS``.
            b"x@example.test, " + _LONG_LOCAL + b"@example.test",
            # Elements that yield no storable address.
            b"x@example.test, (SYNTHETIC_HEADER_MARKER)",
            b"x@example.test, SYNTHETIC_HEADER_MARKER",
            b"x@example.test, <>",
        ],
        ids=["header", "element", "length", "comment", "no_at", "empty_angle"],
    )
    def test_a_loss_clears_only_its_role(self, role, value, caplog):
        caplog.set_level("DEBUG")
        others = {"From": b"f@example.test", "To": b"t@example.test", "Cc": b"c@example.test"}
        headers = b"".join(
            name.encode() + b": " + (value if name == role else others[name]) + b"\r\n"
            for name in ("From", "To", "Cc")
        )
        msg, _ = _parse_headers(headers)
        flags = _completeness(msg)
        assert {name for name, ok in flags.items() if not ok} == {
            f"{role.lower()}_addresses_complete"
        }
        assert msg.parse_caps
        assert "SYNTHETIC_HEADER_MARKER" not in caplog.text

    def test_an_unparsed_from_with_no_address_is_incomplete(self):
        """A From with no parseable address keeps its text as
        ``from_addr`` for display, but no participant row can hold it."""
        msg, _ = _parse_headers(b"From: SYNTHETIC_HEADER_MARKER\r\nTo: t@example.test\r\n")
        assert msg.from_addresses_complete is False
        assert msg.to_addresses_complete is True
        assert msg.parse_caps == {"address_unparsed": 1}

    def test_a_budget_spent_in_one_role_clears_the_role_that_lost(self, monkeypatch):
        """The occurrence budget is shared; the role whose occurrence it
        refused is the incomplete one."""
        from src import parser

        monkeypatch.setattr(parser, "MAX_ADDRESS_OCCURRENCES", 2)
        msg, _ = _parse_headers(
            b"From: f@example.test\r\nTo: t@example.test\r\nCc: c@example.test\r\n"
        )
        assert (
            msg.from_addresses_complete,
            msg.to_addresses_complete,
            msg.cc_addresses_complete,
        ) == (True, True, False)

    def test_counting_unparsed_elements_adds_no_parse_work(self, monkeypatch):
        """Each element still costs at most the two ``parseaddr`` calls
        the budget counts; the new counter reads only their results."""
        from src import parser

        calls = {"n": 0}
        real = parser.email.utils.parseaddr

        def counting(value, *args, **kwargs):
            calls["n"] += 1
            return real(value, *args, **kwargs)

        made = _budget_spy(monkeypatch)
        monkeypatch.setattr(parser.email.utils, "parseaddr", counting)
        to = b", ".join(b"(c%d)" % i for i in range(5_000))
        start = time.perf_counter()
        msg, _ = _parse_headers(b"To: " + to + b"\r\n")
        elapsed = time.perf_counter() - start
        (budget,) = made
        assert msg.parse_caps == {"address_unparsed": 5_000}
        assert budget.parse_calls == 5_000
        assert calls["n"] == budget.parse_calls
        assert elapsed < 10


def test_several_caps_share_one_line_in_a_fixed_order(tmp_path, caplog):
    caplog.set_level("INFO")
    raw = (
        _CAP_HEAD
        + b"To: bob@example.test, "
        + _LONG_LOCAL
        + b"@example.test\r\n"
        + b"Subject: "
        + _LONG_SUBJECT
        + b"\r\n"
        + f"In-Reply-To: <{_LONG_ID}>\r\nReferences: <a@example.test> <{_LONG_ID}>\r\n".encode()
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"".join(b"--b\r\nContent-Type: text/plain\r\n\r\nS%d\r\n" % i for i in range(202))
        + b"--b\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: base64\r\n"
        + _CAP_FILENAME
        + b"\r\nA\r\n--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "many.eml"
    path.write_bytes(raw)
    assert parse_email(path) is not None
    assert [r.getMessage() for r in caplog.records if "parser work caps" in r.getMessage()] == [
        f"parser work caps dropped content from {path}: "
        "transport_decode=1,body_parts=2,address_length=1,"
        "subject_length=1,in_reply_to_length=1,references_length=1"
    ]


def test_message_within_every_cap_logs_no_cap_line(tmp_path, caplog):
    caplog.set_level("DEBUG")
    raw = _with_attachment(b"Content-Type: message/rfc822\r\n", _INNER_EMAIL)
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    (folder / "ok.eml").write_bytes(raw)
    msg = parse_email(folder / "ok.eml")
    assert msg is not None and msg.attachments[0].payload != b""
    assert "parser work caps" not in caplog.text


# Review round 1: caps that empty a container's payload lose nothing
# when no extractor would read that payload. Since #922 every attached
# email's payload is read (``eml``), so those shapes moved to
# ``_CAP_SHAPES``. Each shape's parse result is pinned (unchanged from
# before the caps were logged) and no cap line is logged. #902: a header
# value at its cap is kept whole, so it is not counted either.
_NO_LOSS_SHAPES = {
    "subject_at_the_cap": (
        _headers(b"Subject: " + _LONG_SUBJECT[:SUBJECT_MAX_CHARS] + b"\r\n"),
        lambda msg: msg.subject == _LONG_SUBJECT[:SUBJECT_MAX_CHARS].decode(),
    ),
    "in_reply_to_at_the_cap": (
        _headers(f"In-Reply-To: <{_id_of(998)}>\r\n".encode()),
        lambda msg: msg.in_reply_to == _id_of(998),
    ),
    "references_at_the_cap": (
        _headers(f"References: <a@example.test> <{_id_of(998)}>\r\n".encode()),
        lambda msg: msg.references == ["a@example.test", _id_of(998)],
    ),
}


@pytest.mark.parametrize("shape", sorted(_NO_LOSS_SHAPES))
def test_cap_that_loses_nothing_logs_no_cap_line(tmp_path, caplog, shape):
    caplog.set_level("DEBUG")
    raw, pinned = _NO_LOSS_SHAPES[shape]
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    (folder / "m.eml").write_bytes(raw)
    msg = parse_email(folder / "m.eml")
    assert msg is not None
    assert pinned(msg)
    assert "parser work caps" not in caplog.text


# Review round 4 on #884: ``body_parts`` counts a capped text part only
# when it could have contributed to the assembled body, by the same
# selection ``_assemble_body`` makes (an alternative after the selected
# one is not a loss). Each shape: (parts before the tail, the tail, the
# body before the caps were logged, the expected body_parts count).
def _parts(count: int, start: int = 0) -> bytes:
    return b"".join(
        b"--b\r\nContent-Type: text/plain\r\n\r\nS%d\r\n" % i for i in range(start, start + count)
    )


def _alternative(*children: tuple[bytes, bytes]) -> bytes:
    return (
        b'--b\r\nContent-Type: multipart/alternative; boundary="a"\r\n\r\n'
        + b"".join(
            b"--a\r\nContent-Type: " + ctype + b"\r\n\r\n" + body + b"\r\n"
            for ctype, body in children
        )
        + b"--a--\r\n"
    )


_BODY_CAP_SHAPES = {
    # The plain alternative is the 200th text part; the HTML one after it
    # is capped but would never be chosen.
    "alternative_after_selected": (
        _parts(199)
        + _alternative((b"text/plain", b"ALT_PLAIN"), (b"text/html", b"<p>ALT_HTML</p>")),
        [f"S{i}" for i in range(199)] + ["ALT_PLAIN"],
        0,
    ),
    # Both alternatives are capped: the plain one would have been the
    # alternative's body, the HTML one would not.
    "alternative_fully_capped": (
        _parts(200)
        + _alternative((b"text/plain", b"ALT_PLAIN"), (b"text/html", b"<p>ALT_HTML</p>")),
        [f"S{i}" for i in range(200)],
        1,
    ),
    # The HTML alternative is within the cap and chosen only because the
    # plain one, which would be preferred, is capped.
    "preferred_alternative_capped": (
        _parts(199)
        + _alternative((b"text/html", b"<p>ALT_HTML</p>"), (b"text/plain", b"ALT_PLAIN")),
        [f"S{i}" for i in range(199)] + ["ALT_HTML"],
        1,
    ),
    # Sequential parts past the cap each contribute.
    "sequential": (
        _parts(203),
        [f"S{i}" for i in range(200)],
        3,
    ),
}


def _parse_body_cap_shape(tmp_path, shape):
    raw = (
        b"Message-ID: <body@example.test>\r\nFrom: sender@example.test\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + _BODY_CAP_SHAPES[shape][0]
        + b"--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "body.eml"
    path.write_bytes(raw)
    msg = parse_email(path)
    assert msg is not None
    return msg, path


@pytest.mark.parametrize("shape", sorted(_BODY_CAP_SHAPES))
def test_body_cap_shape_assembled_body_is_pinned(tmp_path, shape):
    msg, _ = _parse_body_cap_shape(tmp_path, shape)
    assert msg.body_text.split("\n\n") == _BODY_CAP_SHAPES[shape][1]


@pytest.mark.parametrize("shape", sorted(_BODY_CAP_SHAPES))
def test_body_parts_counts_only_parts_that_could_contribute(tmp_path, caplog, shape):
    caplog.set_level("INFO")
    _, path = _parse_body_cap_shape(tmp_path, shape)
    lines = [r.getMessage() for r in caplog.records if "parser work caps" in r.getMessage()]
    count = _BODY_CAP_SHAPES[shape][2]
    expected = (
        [f"parser work caps dropped content from {path}: body_parts={count}"] if count else []
    )
    assert lines == expected


def test_parser_cap_lines_are_rate_limited_and_counted(tmp_path, monkeypatch, caplog):
    """Review round 5 on #884 (security): every capped message logged its
    own WARNING, so a stream of crafted messages could flood the log. The
    line shares the extractor warning budget; every capped message is
    counted as ``parser_caps_messages`` for the attachments aggregate,
    and each withheld line as ``warnings_suppressed``. Parse results are
    unchanged."""
    from src import extractors

    caplog.set_level("INFO")
    monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
    extractors.drain_extractor_counts()
    raw, _, _, pinned = _CAP_SHAPES["body_parts"]
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    for i in range(5):
        path = folder / f"caps{i}.eml"
        path.write_bytes(raw)
        msg = parse_email(path)
        assert msg is not None and pinned(msg)
    lines = [r for r in caplog.records if "parser work caps" in r.getMessage()]
    assert [r.levelname for r in lines] == ["WARNING", "WARNING"]
    counts = extractors.drain_extractor_counts()
    assert counts["parser_caps_messages"] == 5
    assert counts["warnings_suppressed"] == 3


def test_message_within_every_cap_is_not_counted(tmp_path):
    from src import extractors

    extractors.drain_extractor_counts()
    raw = _with_attachment(b"Content-Type: message/rfc822\r\n", _INNER_EMAIL)
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    (folder / "ok.eml").write_bytes(raw)
    assert parse_email(folder / "ok.eml") is not None
    assert extractors.drain_extractor_counts()["parser_caps_messages"] == 0


@pytest.mark.parametrize("shape", ["subject_length", "in_reply_to_length", "references_length"])
def test_header_caps_count_the_message_for_the_aggregate(tmp_path, monkeypatch, shape):
    """#902: a message whose subject was cut or whose reply headers
    were dropped counts as ``parser_caps_messages`` like any other."""
    from src import extractors

    extractors.drain_extractor_counts()
    _parse_cap_shape(tmp_path, monkeypatch, shape)
    assert extractors.drain_extractor_counts()["parser_caps_messages"] == 1


def test_every_parse_cap_is_counted_at_a_site_and_pinned_by_a_shape():
    """#902 (#905): the cap line renders ``PARSE_CAPS`` names only, so a
    cap counted under a name the tuple lacks is left off the line with
    no test failing. The counted names are derived from the parser's
    source (every ``caps[...] +=`` site names a literal or calls
    ``_nesting_cap``, whose two names are read the same way) and must
    equal ``PARSE_CAPS``, and every name must have a pinned shape in
    ``_CAP_SHAPES``, except the two ``PARSE_REPEATS`` names, which lose
    nothing and are logged on their own lines, pinned by
    ``TestRepeatedAddressHeaders`` (#1144)."""
    import inspect

    from src import parser

    sites = re.findall(r"\bcaps\[([^\]]+)\] \+=", inspect.getsource(parser))
    assert sites
    counted: set[str] = set()
    for site in sites:
        if site == "_nesting_cap(budget)":
            counted |= set(re.findall(r'"(\w+)"', inspect.getsource(parser._nesting_cap)))
        else:
            assert re.fullmatch(r'"\w+"', site), site
            counted.add(site.strip('"'))
    shaped = {
        name for _, _, line, _ in _CAP_SHAPES.values() for name in re.findall(r"(\w+)=\d+", line)
    }
    assert len(set(PARSE_CAPS)) == len(PARSE_CAPS)
    assert set(PARSE_REPEATS) <= set(PARSE_CAPS)
    assert counted == set(PARSE_CAPS) == shaped | set(PARSE_REPEATS)


# #996: ordinary multipart shapes, pinned on main before the walk was
# capped: the cap must not change their body or attachments.
_PDF = b'Content-Type: application/pdf\r\nContent-Disposition: attachment; filename="a.pdf"\r\n'
_PNG = (
    b"Content-Type: image/png\r\nContent-ID: <img1>\r\n"
    b'Content-Disposition: inline; filename="i.png"\r\n'
)
_ORDINARY_SHAPES = {
    "alternative": (
        b'Content-Type: multipart/alternative; boundary="a"\r\n\r\n'
        b"--a\r\nContent-Type: text/plain\r\n\r\nPLAIN\r\n"
        b"--a\r\nContent-Type: text/html\r\n\r\n<p>HTML</p>\r\n--a--\r\n",
        "PLAIN",
        [],
    ),
    "related": (
        b'Content-Type: multipart/related; boundary="r"\r\n\r\n'
        b"--r\r\nContent-Type: text/html\r\n\r\n<p>ROOT</p>\r\n"
        b"--r\r\n" + _PNG + b"\r\nPNGDATA\r\n--r--\r\n",
        "ROOT",
        [("i.png", "image/png", 7)],
    ),
    "mixed_with_attachments": (
        b'Content-Type: multipart/mixed; boundary="m"\r\n\r\n'
        b"--m\r\nContent-Type: text/plain\r\n\r\nFIRST\r\n"
        b"--m\r\n" + _PDF + b"\r\nPDFDATA\r\n"
        b"--m\r\nContent-Type: text/plain\r\n\r\nSECOND\r\n--m--\r\n",
        "FIRST\n\nSECOND",
        [("a.pdf", "application/pdf", 7)],
    ),
    # Alternative holding a related, an attachment, and an attached
    # email with its own attachment (recorded, its text not the body).
    "nested_containers": (
        b'Content-Type: multipart/mixed; boundary="m"\r\n\r\n'
        b'--m\r\nContent-Type: multipart/alternative; boundary="a"\r\n\r\n'
        b"--a\r\nContent-Type: text/plain\r\n\r\nALT_PLAIN\r\n"
        b'--a\r\nContent-Type: multipart/related; boundary="r"\r\n\r\n'
        b"--r\r\nContent-Type: text/html\r\n\r\n<p>ALT_HTML</p>\r\n"
        b"--r\r\n" + _PNG + b"\r\nPNGDATA\r\n--r--\r\n"
        b"--a--\r\n"
        b"--m\r\n" + _PDF + b"\r\nPDFDATA\r\n"
        b"--m\r\nContent-Type: message/rfc822\r\n"
        b'Content-Disposition: attachment; filename="f.eml"\r\n\r\n'
        b'From: a@example.test\r\nContent-Type: multipart/mixed; boundary="i"\r\n\r\n'
        b"--i\r\nContent-Type: text/plain\r\n\r\nINNER\r\n"
        b"--i\r\n" + _PDF + b"\r\nINNERPDF\r\n--i--\r\n"
        b"--m--\r\n",
        "ALT_PLAIN",
        [
            ("i.png", "image/png", 7),
            ("a.pdf", "application/pdf", 7),
            ("f.eml", "message/rfc822", -1),  # its serialization's size
            ("a.pdf", "application/pdf", 8),
        ],
    ),
}


@pytest.mark.parametrize("shape", sorted(_ORDINARY_SHAPES))
def test_ordinary_multipart_shape_parse_result_is_pinned(tmp_path, caplog, shape):
    caplog.set_level("INFO")
    raw, body, attachments = _ORDINARY_SHAPES[shape]
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "ordinary.eml"
    path.write_bytes(_CAP_HEAD + raw)
    msg = parse_email(path)
    assert msg is not None
    assert msg.body_text == body
    got = [(a.filename, a.content_type, a.size) for a in msg.attachments]
    assert [g if s >= 0 else (*g[:2], -1) for g, (_, _, s) in zip(got, attachments)] == attachments
    assert len(got) == len(attachments)
    assert "parser work caps" not in caplog.text


def test_message_at_the_walk_cap_is_not_cut(tmp_path, caplog):
    """#996: exactly MAX_WALKED_PARTS parts (the root included) are all
    walked: the attachment that is the last of them is kept."""
    from src.parser import MAX_WALKED_PARTS

    caplog.set_level("INFO")
    raw = (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: application/x-empty\r\n\r\n\r\n" * (MAX_WALKED_PARTS - 2)
        + b"--b\r\n"
        + _PDF
        + b"\r\nPDFDATA\r\n--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    (folder / "edge.eml").write_bytes(raw)
    msg = parse_email(folder / "edge.eml")
    assert msg is not None
    assert [a.filename for a in msg.attachments] == ["a.pdf"]
    assert "parser work caps" not in caplog.text


def test_many_part_message_walk_is_bounded(tmp_path, monkeypatch, caplog):
    """#996 (Codex security review on PR #444): every MIME part became a
    body node before any cap applied, so a crafted message of 700,000
    empty parts (4.9 MB) took seconds of the single ingestion worker
    on top of the stdlib parse. The walk now stops at MAX_WALKED_PARTS:
    assert the parts visited, not only the time, and the WARNING."""
    from src import parser

    caplog.set_level("INFO")
    visited = 0
    real = parser._part_filename

    def counting(part):
        nonlocal visited
        visited += 1
        return real(part)

    monkeypatch.setattr(parser, "_part_filename", counting)
    raw = (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: text/plain\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
        + b"--b\r\n\r\n" * 200_000
        + b"--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "many.eml"
    path.write_bytes(raw)
    msg_tree = email.message_from_bytes(raw)
    start = time.perf_counter()
    body, attachments = parser._extract_body_and_attachments(msg_tree)
    elapsed = time.perf_counter() - start
    assert visited == parser.MAX_WALKED_PARTS
    assert body == "SYNTHETIC_TEXT_MARKER" and attachments == []
    assert elapsed < 5
    caplog.clear()
    visited = 0
    msg = parse_email(path)
    assert msg is not None and visited == parser.MAX_WALKED_PARTS
    # The empty parts are text/plain by default: those walked past the
    # 200-text-part cap (all but the root and the first 200) count too.
    lines = [r for r in caplog.records if "parser work caps" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        (
            "WARNING",
            f"parser work caps dropped content from {path}: "
            f"body_parts={parser.MAX_WALKED_PARTS - 201},mime_parts=1",
        )
    ]
    assert "SYNTHETIC_TEXT_MARKER" not in caplog.text


class _CountingParts(list):
    """A part list that counts the children taken from it, forwards or
    backwards, so a test can assert how many a walk queued."""

    taken = 0

    def __iter__(self):
        for child in super().__iter__():
            _CountingParts.taken += 1
            yield child

    def __reversed__(self):
        for child in super().__reversed__():
            _CountingParts.taken += 1
            yield child


def test_attached_container_children_are_queued_lazily(monkeypatch, caplog):
    """Review round 1 on #1020 (Codex): a container marked as an
    attachment went through the serialization preflight
    (``_nesting_exceeds``), which queued every child of a part at once,
    so 200,000 children were all queued before any cap applied. Both
    that preflight and the walk now take children as they visit them:
    assert the children taken, not only the result and the time."""
    from src import parser

    caplog.set_level("INFO")
    raw = (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"--b\r\nContent-Type: text/plain\r\n\r\nPARENT_BODY\r\n"
        + b'--b\r\nContent-Type: multipart/mixed; boundary="c"\r\n'
        + _CAP_FILENAME
        + b"\r\n"
        + b"--c\r\n\r\n" * 200_000
        + b"--c--\r\n--b--\r\n"
    )
    tree = email.message_from_bytes(raw)
    bundle = tree.get_payload()[1]
    bundle.set_payload(_CountingParts(bundle.get_payload()))
    _CountingParts.taken = 0
    caps: Counter[str] = Counter()
    start = time.perf_counter()
    body, attachments = parser._extract_body_and_attachments(tree, caps=caps)
    elapsed = time.perf_counter() - start
    assert body == "PARENT_BODY"
    assert [a.filename for a in attachments] == ["SYNTHETIC_FILENAME_MARKER.eml"]
    assert attachments[0].payload == b""
    # The preflight stops once the field budget is spent, and the walk
    # at the part cap: neither takes much past its cap.
    assert _CountingParts.taken <= parser.MAX_ATTACHED_MESSAGE_FIELDS + parser.MAX_WALKED_PARTS
    # The bundle is named ``.eml``, which the ``eml`` extractor reads
    # (#922), so its emptied payload is lost content too.
    assert caps == Counter({"mime_parts": 1, "attached_fields": 1})
    assert elapsed < 5


def test_walk_cap_keeps_the_first_parts_in_document_order(tmp_path, caplog):
    """Review round 1 on #1020 (Codex): the cap was charged for every
    child as its parent was popped, so a root with thousands of
    container children spent the budget at once and the text inside
    the first child, the third part in document order, was dropped
    while later containers were still visited. The cap now keeps the
    first MAX_WALKED_PARTS parts in document order."""
    from src import parser

    caplog.set_level("INFO")
    raw = (
        _CAP_HEAD
        + b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        + b"".join(
            b'--b\r\nContent-Type: multipart/mixed; boundary="c"\r\n\r\n'
            b"--c\r\nContent-Type: text/plain\r\n\r\nT%d\r\n--c--\r\n" % i
            for i in range(parser.MAX_WALKED_PARTS)
        )
        + b"--b--\r\n"
    )
    folder = tmp_path / "INBOX" / "cur"
    folder.mkdir(parents=True)
    path = folder / "order.eml"
    path.write_bytes(raw)
    msg = parse_email(path)
    assert msg is not None
    assert msg.body_text.split("\n\n") == [f"T{i}" for i in range(200)]
    # The root and 4,999 (container, text) pairs, then one more container:
    # the text parts walked past the 200th count as body_parts.
    walked_texts = (parser.MAX_WALKED_PARTS - 1) // 2
    lines = [r.getMessage() for r in caplog.records if "parser work caps" in r.getMessage()]
    assert lines == [
        f"parser work caps dropped content from {path}: "
        f"body_parts={walked_texts - 200},mime_parts=1"
    ]


# ---------------------------------------------------------------------------
# Repeated address headers and the per-message address budget (#1144)
# ---------------------------------------------------------------------------

_REPEAT_HEAD = (
    b"Message-ID: <repeat@example.test>\r\nDate: Mon, 28 Sep 2026 12:00:00 +0000\r\n"
    b"Subject: Repeat\r\n"
)


def _parse_headers(extra: bytes):
    """Parse a plain message whose headers are ``_REPEAT_HEAD`` plus
    ``extra`` (CRLF-terminated lines). Returns the Message and its path."""
    raw = _REPEAT_HEAD + extra + b"Content-Type: text/plain\r\n\r\nBODY\r\n"
    source = SourceMetadata(
        folder="INBOX", flags=frozenset(), size=len(raw), mtime_ns=None, path="/m/repeat.eml"
    )
    msg = parse_email_bytes(raw, source)
    assert msg is not None
    return msg, source.path


def _budget_spy(monkeypatch):
    """Record every ``_AddressBudget`` the parser creates."""
    from src import parser

    made: list = []
    real = parser._AddressBudget

    def spy():
        budget = real()
        made.append(budget)
        return budget

    monkeypatch.setattr(parser, "_AddressBudget", spy)
    return made


def _cap_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "parser work caps" in r.getMessage()]


class TestRepeatedAddressHeaders:
    def test_repeated_to_and_cc_are_merged_in_order_each_role_on_its_own(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        made = _budget_spy(monkeypatch)
        msg, path = _parse_headers(
            b"From: SYNTHETIC_HEADER_MARKER <sender@example.test>\r\n"
            b"To: a@example.test, SYNTHETIC_HEADER_MARKER <b@example.test>\r\n"
            b"Cc: c@example.test\r\n"
            b"To: d@example.test\r\n"
            b"Cc: e@example.test\r\nTo: f@example.test\r\n"
        )
        assert msg.to_addrs == [
            "a@example.test",
            "SYNTHETIC_HEADER_MARKER <b@example.test>",
            "d@example.test",
            "f@example.test",
        ]
        assert msg.cc_addrs == ["c@example.test", "e@example.test"]
        assert msg.from_addrs == ["SYNTHETIC_HEADER_MARKER <sender@example.test>"]
        assert msg.sender_ambiguous is False
        # The work done: every header field scanned once, the six
        # occurrences parsed, seven elements split, seven addresses kept.
        (budget,) = made
        assert (budget.fields, budget.occurrences, budget.elements, budget.addresses) == (
            10,
            6,
            7,
            7,
        )
        lines = [r for r in caplog.records if "repeated" in r.getMessage()]
        assert [(r.levelname, r.getMessage()) for r in lines] == [
            ("INFO", f"parser merged 3 repeated To/Cc headers in {path}")
        ]
        assert _cap_lines(caplog) == []
        assert "SYNTHETIC_HEADER_MARKER" not in caplog.text

    def test_single_headers_are_unchanged_and_log_nothing(self, caplog):
        caplog.set_level("DEBUG")
        msg, _ = _parse_headers(
            b"From: a@example.test, b@example.test\r\nTo: c@example.test\r\nCc: d@example.test\r\n"
        )
        assert msg.from_addr == "a@example.test"
        assert msg.from_addrs == ["a@example.test", "b@example.test"]
        assert (msg.to_addrs, msg.cc_addrs) == (["c@example.test"], ["d@example.test"])
        # A single From listing several authors is valid and unambiguous.
        assert msg.sender_ambiguous is False
        assert "repeated" not in caplog.text
        assert "From headers" not in caplog.text

    def test_repeated_from_keeps_the_first_and_flags_the_sender(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        made = _budget_spy(monkeypatch)
        msg, path = _parse_headers(
            b"From: First <first@example.test>\r\nTo: bob@example.test\r\n"
            b"From: SYNTHETIC_HEADER_MARKER <second@example.test>\r\n"
            b"From: third@example.test\r\n"
        )
        assert msg.from_addr == "First <first@example.test>"
        assert msg.from_addrs == ["First <first@example.test>"]
        assert msg.to_addrs == ["bob@example.test"]
        assert msg.sender_ambiguous is True
        # The later From headers are counted, never parsed.
        (budget,) = made
        assert (budget.occurrences, budget.elements, budget.addresses) == (2, 2, 2)
        lines = [r for r in caplog.records if "From headers" in r.getMessage()]
        assert [(r.levelname, r.getMessage()) for r in lines] == [
            (
                "WARNING",
                f"parser kept the first of 3 From headers in {path}; sender ambiguous, "
                "no source authority or subject-fallback threading",
            )
        ]
        assert _cap_lines(caplog) == []
        assert "SYNTHETIC_HEADER_MARKER" not in caplog.text

    def test_header_names_match_case_insensitively(self):
        msg, _ = _parse_headers(
            b"FROM: a@example.test\r\nto: b@example.test\r\nTO: c@example.test\r\n"
            b"from: d@example.test\r\n"
        )
        assert msg.to_addrs == ["b@example.test", "c@example.test"]
        assert msg.from_addr == "a@example.test"
        assert msg.sender_ambiguous is True

    def test_occurrences_past_the_budget_are_lost_and_counted(self, monkeypatch, caplog):
        from src import parser

        caplog.set_level("INFO")
        made = _budget_spy(monkeypatch)
        cap = parser.MAX_ADDRESS_OCCURRENCES
        extra = b"From: sender@example.test\r\n" + b"".join(
            b"To: r%d@example.test\r\n" % i for i in range(cap + 5)
        )
        msg, path = _parse_headers(extra)
        # From takes one occurrence; To gets the rest of the budget.
        assert msg.to_addrs == [f"r{i}@example.test" for i in range(cap - 1)]
        assert msg.from_addr == "sender@example.test"
        (budget,) = made
        assert budget.occurrences == cap
        assert budget.elements == cap
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: address_occurrences=6"
        ]
        assert f"parser merged {cap - 2} repeated To/Cc headers in {path}" in caplog.text

    def test_from_is_parsed_before_recipients_whatever_the_header_order(self):
        from src import parser

        cap = parser.MAX_ADDRESS_OCCURRENCES
        extra = b"".join(b"To: r%d@example.test\r\n" % i for i in range(cap + 5))
        msg, _ = _parse_headers(extra + b"From: sender@example.test\r\n")
        assert msg.from_addr == "sender@example.test"
        assert len(msg.to_addrs) == cap - 1

    def test_total_raw_chars_past_the_budget_are_lost_before_any_parse(self, monkeypatch, caplog):
        from src import parser

        caplog.set_level("INFO")
        monkeypatch.setattr(parser, "_MAX_ADDRESS_TOTAL_CHARS", 200)
        made = _budget_spy(monkeypatch)
        long_to = b"To: " + b"x" * 30 + b"@example.test, SYNTHETIC_HEADER_MARKER@example.test\r\n"
        msg, path = _parse_headers(b"From: s@example.test\r\n" + long_to * 3)
        assert len(msg.to_addrs) == 4
        (budget,) = made
        # The third To would pass the total: it is neither decoded nor split.
        assert budget.occurrences == 4
        assert budget.elements == 1 + 4
        assert budget.chars <= 200
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: address_chars=1"
        ]
        assert "SYNTHETIC_HEADER_MARKER" not in caplog.text

    def test_an_over_long_8bit_header_is_rejected_before_it_is_decoded(self, monkeypatch):
        """#1144: ``_parse_addrs`` decoded a raw 8-bit header (an
        ``email.header.Header``) before its length check, so a header far
        over the cap was decoded whole and then dropped."""
        from src import parser

        decoded: list[int] = []
        real = parser._decode_header

        def counting(value, degraded=None):
            decoded.append(1)
            return real(value, degraded)

        monkeypatch.setattr(parser, "_decode_header", counting)
        size = parser._MAX_ADDRESS_HEADER_CHARS + 10
        to = b"To: " + ("é" * (size // 2)).encode() + b"@example.test\r\n"
        msg, _ = _parse_headers(b"From: s@example.test\r\n" + to)
        assert msg.to_addrs == []
        # The Subject is decoded; the To header never is.
        assert decoded == [1]

    def test_an_over_long_first_from_leaves_no_fallback_text(self):
        """The undecodable-From fallback reads the first From only when
        it is within the per-value cap: an over-long one is never decoded
        into ``from_addr``."""
        from src import parser

        size = parser._MAX_ADDRESS_HEADER_CHARS + 10
        msg, _ = _parse_headers(b"From: SYNTHETIC_HEADER_MARKER" + b"x" * size + b"\r\n")
        assert msg.from_addr == ""
        assert msg.from_addrs == []

    def test_unparseable_from_still_falls_back_to_its_decoded_text(self):
        msg, _ = _parse_headers(b"From: =?utf-8?q?No_Address?= <>\r\nTo: b@example.test\r\n")
        assert msg.from_addrs == []
        assert msg.from_addr == "No Address <>"
        assert msg.sender_ambiguous is False

    def test_elements_past_the_budget_are_lost_and_counted(self, monkeypatch, caplog):
        from src import parser

        caplog.set_level("INFO")
        monkeypatch.setattr(parser, "MAX_ADDRESS_ELEMENTS", 5)
        made = _budget_spy(monkeypatch)
        msg, path = _parse_headers(
            b"From: s@example.test\r\nTo: a@x.test, b@x.test, ,c@x.test\r\n"
            b"To: d@x.test, e@x.test, f@x.test\r\n"
        )
        assert msg.to_addrs == ["a@x.test", "b@x.test", "c@x.test", "d@x.test"]
        (budget,) = made
        assert budget.elements == 5
        assert budget.parse_calls <= 2 * 5
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: address_elements=2"
        ]

    def test_addresses_past_the_budget_are_lost_and_counted(self, monkeypatch, caplog):
        from src import parser

        caplog.set_level("INFO")
        monkeypatch.setattr(parser, "MAX_MESSAGE_ADDRESSES", 3)
        made = _budget_spy(monkeypatch)
        msg, path = _parse_headers(
            b"From: s@example.test\r\nTo: a@x.test, b@x.test\r\nCc: c@x.test, d@x.test\r\n"
        )
        assert msg.from_addrs == ["s@example.test"]
        assert msg.to_addrs == ["a@x.test", "b@x.test"]
        assert msg.cc_addrs == []
        (budget,) = made
        assert budget.addresses == 3
        # Once the address budget is spent, no element is parsed.
        assert budget.elements == 3
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: address_count=2"
        ]

    def test_fields_past_the_scan_budget_are_lost_and_the_sender_ambiguous(
        self, monkeypatch, caplog
    ):
        """Past the scan budget no later From, To or Cc is seen, so a
        second From cannot be ruled out: the sender is ambiguous."""
        from src import parser

        caplog.set_level("INFO")
        monkeypatch.setattr(parser, "MAX_ADDRESS_HEADER_FIELDS", 6)
        made = _budget_spy(monkeypatch)
        msg, path = _parse_headers(
            b"From: s@example.test\r\nX-A: 1\r\nX-B: 2\r\nTo: late@example.test\r\n"
        )
        assert msg.from_addr == "s@example.test"
        assert msg.to_addrs == []
        assert msg.sender_ambiguous is True
        (budget,) = made
        assert budget.fields == 6
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: address_fields=1"
        ]

    def test_a_scan_that_reads_every_field_at_the_limit_is_complete(self, monkeypatch):
        """The boundary: a message with exactly ``MAX_ADDRESS_HEADER_FIELDS``
        fields is scanned to the end, so its single From is unambiguous;
        one more field leaves input unread and makes it ambiguous."""
        from src import parser

        # _REPEAT_HEAD's three fields, From, and Content-Type.
        monkeypatch.setattr(parser, "MAX_ADDRESS_HEADER_FIELDS", 5)
        msg, _ = _parse_headers(b"From: s@example.test\r\n")
        assert msg.sender_ambiguous is False
        msg, _ = _parse_headers(b"From: s@example.test\r\nX-A: 1\r\n")
        assert msg.sender_ambiguous is True

    def test_repeat_lines_are_rate_limited_and_counted(self, monkeypatch, caplog):
        from src import extractors

        caplog.set_level("INFO")
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        extractors.drain_extractor_counts()
        extractors.drain_suppressed_lines()
        for _ in range(3):
            _parse_headers(b"From: a@example.test\r\nFrom: b@example.test\r\n")
        for _ in range(2):
            _parse_headers(b"From: a@example.test\r\nTo: b@x.test\r\nTo: c@x.test\r\n")
        lines = [
            r
            for r in caplog.records
            if "repeated" in r.getMessage() or "From headers" in r.getMessage()
        ]
        assert len(lines) == 2
        counts = extractors.drain_extractor_counts()
        assert counts["parser_sender_ambiguous_messages"] == 3
        assert counts["parser_recipients_merged_messages"] == 2
        assert counts["parser_caps_messages"] == 0
        # Review round 3: these lines lose no attachment text, so what
        # the limit withholds is counted on the heartbeat
        # (``suppressed_lines``), not as ``warnings_suppressed``.
        assert counts["warnings_suppressed"] == 0
        assert extractors.drain_suppressed_lines() == 3

    def test_merged_only_traffic_keeps_the_attachments_line_at_info(self, monkeypatch):
        """Review round 3: suppressed merge notices made the attachments
        summary a WARNING through ``warnings_suppressed``."""
        from src import attachment_indexing, extractors

        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        attachment_indexing.attachment_outcomes.drain()
        extractors.drain_suppressed_lines()
        for _ in range(25):
            _parse_headers(b"From: a@example.test\r\nTo: b@x.test\r\nTo: c@x.test\r\n")
        counts = attachment_indexing.attachment_outcomes.drain()
        assert counts["parser_recipients_merged_messages"] == 25
        assert counts["warnings_suppressed"] == 0
        assert attachment_indexing.format_attachment_outcomes(counts)
        assert not attachment_indexing.attachment_outcomes_degraded(counts)
        assert extractors.drain_suppressed_lines() == 23

    def test_worst_case_message_parses_within_a_bound(self, monkeypatch):
        """Every address dimension at its cap at once: many fields, the
        occurrence budget spent on long headers full of tiny elements.
        Asserts the work done, not only the time."""
        import time

        from src import parser

        made = _budget_spy(monkeypatch)
        element = b"a@b.c,"
        per_header = parser._MAX_ADDRESS_HEADER_CHARS // len(element)
        to = b"To: " + element * per_header + b"\r\n"
        extra = b"X-F: 1\r\n" * 5_000 + b"From: s@example.test\r\n" + to * 70
        start = time.perf_counter()
        msg, _ = _parse_headers(extra)
        elapsed = time.perf_counter() - start
        (budget,) = made
        assert len(msg.from_addrs) + len(msg.to_addrs) == parser.MAX_MESSAGE_ADDRESSES
        assert budget.elements <= parser.MAX_ADDRESS_ELEMENTS
        assert budget.chars <= parser._MAX_ADDRESS_TOTAL_CHARS
        assert budget.occurrences <= parser.MAX_ADDRESS_OCCURRENCES
        assert elapsed < 30


# ---------------------------------------------------------------------------
# Every display name per participant (#1140)
# ---------------------------------------------------------------------------


class TestParticipantNames:
    """#1140: ``participant_names`` holds every distinct display name
    each (role, address) was written with, not only the first, so name
    matching sees all of them. The first name of each (role, address)
    is always kept; every further one spends a per-message budget."""

    def test_one_address_under_two_names_keeps_both(self):
        msg, _ = _parse_headers(
            b"From: sender@example.test\r\n"
            b"To: Jane Roe <jane@example.test>, J. Roe <JANE@example.test>\r\n"
        )
        assert msg.participant_names == [
            ("to", "jane@example.test", "Jane Roe"),
            ("to", "jane@example.test", "J. Roe"),
        ]
        assert msg.participant_names_complete is True

    def test_each_role_keeps_its_own_names_in_header_order(self):
        msg, _ = _parse_headers(
            b"From: Jane Roe <jane@example.test>\r\n"
            b"To: bob@example.test, Jane (work) <jane@example.test>\r\n"
            b"Cc: Bob B <bob@example.test>\r\n"
            b"To: Robert <bob@example.test>\r\n"
        )
        assert msg.participant_names == [
            ("from", "jane@example.test", "Jane Roe"),
            ("to", "jane@example.test", "Jane (work)"),
            ("to", "bob@example.test", "Robert"),
            ("cc", "bob@example.test", "Bob B"),
        ]

    def test_exact_duplicates_are_stored_once_and_casing_is_kept(self):
        msg, _ = _parse_headers(
            b"From: sender@example.test\r\n"
            b"To: Jane <jane@example.test>, Jane <jane@example.test>, JANE <jane@example.test>,"
            b" jane@example.test\r\n"
        )
        assert msg.participant_names == [
            ("to", "jane@example.test", "Jane"),
            ("to", "jane@example.test", "JANE"),
        ]

    def test_encoded_names_are_decoded(self):
        msg, _ = _parse_headers(
            b"From: sender@example.test\r\n"
            b"To: =?utf-8?q?Jos=C3=A9?= <jose@example.test>, Pepe <jose@example.test>\r\n"
        )
        assert [name for _, _, name in msg.participant_names] == ["José", "Pepe"]

    def test_names_past_the_count_budget_are_dropped_and_counted(self, monkeypatch, caplog):
        from src import extractors, parser

        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        monkeypatch.setattr(parser, "MAX_EXTRA_PARTICIPANT_NAMES", 2)
        others = b", ".join(b"SYNTHETIC_HEADER_MARKER %d <jane@example.test>" % i for i in range(5))
        msg, path = _parse_headers(
            b"From: Sam <sam@example.test>\r\n"
            b"To: Jane <jane@example.test>, " + others + b", Kim <kim@example.test>\r\n"
        )
        # Every participant's first name is kept; two further names fit.
        assert msg.participant_names == [
            ("from", "sam@example.test", "Sam"),
            ("to", "jane@example.test", "Jane"),
            ("to", "jane@example.test", "SYNTHETIC_HEADER_MARKER 0"),
            ("to", "jane@example.test", "SYNTHETIC_HEADER_MARKER 1"),
            ("to", "kim@example.test", "Kim"),
        ]
        # The message keeps every recipient, and records the loss.
        assert len(msg.to_addrs) == 7
        assert msg.participant_names_complete is False
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: participant_names=3"
        ]
        assert extractors.drain_extractor_counts()["parser_caps_messages"] == 1
        assert "SYNTHETIC_HEADER_MARKER" not in caplog.text

    def test_names_past_the_byte_budget_are_dropped(self, monkeypatch, caplog):
        from src import parser

        caplog.set_level("DEBUG")
        # "Jo" (2 bytes) fits; "José" is 5 UTF-8 bytes and does not.
        monkeypatch.setattr(parser, "MAX_EXTRA_PARTICIPANT_NAME_BYTES", 6)
        msg, path = _parse_headers(
            b"From: sender@example.test\r\n"
            b"To: Jane <j@example.test>, Jo <j@example.test>,"
            b" =?utf-8?q?Jos=C3=A9?= <j@example.test>, Al <j@example.test>\r\n"
        )
        assert [name for _, _, name in msg.participant_names] == ["Jane", "Jo", "Al"]
        assert msg.participant_names_complete is False
        assert _cap_lines(caplog) == [
            f"parser work caps dropped content from {path}: participant_names=1"
        ]

    def test_worst_case_names_are_bounded(self, monkeypatch):
        """Every address a message may keep, all one address under
        distinct names: the stored names stop at the budget, and the
        name pass costs two ``parseaddr`` calls per kept address."""
        from src import parser

        calls = {"n": 0}
        real = parser.email.utils.parseaddr

        def counting(value, *args, **kwargs):
            calls["n"] += 1
            return real(value, *args, **kwargs)

        to = b", ".join(b"N%05d <j@x.test>" % i for i in range(parser.MAX_MESSAGE_ADDRESSES))
        made = _budget_spy(monkeypatch)
        monkeypatch.setattr(parser.email.utils, "parseaddr", counting)
        start = time.perf_counter()
        msg, _ = _parse_headers(b"From: s@example.test\r\nTo: " + to + b"\r\n")
        elapsed = time.perf_counter() - start
        (budget,) = made
        kept = len(msg.from_addrs) + len(msg.to_addrs)
        assert kept == parser.MAX_MESSAGE_ADDRESSES
        assert len(msg.participant_names) == 1 + parser.MAX_EXTRA_PARTICIPANT_NAMES
        # The address parse (``parse_calls``, plus ``_format_address``'s
        # identity check on each named To address), then two per kept
        # address for the names.
        assert calls["n"] == budget.parse_calls + (kept - 1) + 2 * kept
        assert elapsed < 30


# #1242: the container attachments left unserialized, per cap shape (none
# for a shape that drops no attachment payload). An emptied payload is
# not the attachment's, so its extracted text never counts as complete.
_PAYLOAD_LOSS = {
    "attached_depth": 1,
    "attached_fields": 1,
    "attached_depth_decoded": 1,
    # The 19 nested containers kept empty by design (only the top one is
    # serialized), plus the one the depth cap stopped.
    "attached_depth_decode_chain": 20,
    "transport_decode_base64": 1,
    "transport_decode_8bit": 1,
    "decoded_bytes": 1,
    "container_serialize": 1,
    "container_serialize_decoded": 1,
    "attached_depth_keeps_inner_attachment": 1,
    "attached_fields_eml": 1,
    "container_serialize_eml": 1,
    "container_serialize_decoded_eml": 1,
}


@pytest.mark.parametrize("shape", sorted(_CAP_SHAPES))
def test_cap_shape_marks_exactly_the_emptied_payloads(tmp_path, monkeypatch, shape):
    msg, _ = _parse_cap_shape(tmp_path, monkeypatch, shape)
    lost = [a for a in msg.attachments if not a.payload_complete]
    assert len(lost) == _PAYLOAD_LOSS.get(shape, 0)
    assert all(a.payload == b"" for a in lost)


def test_an_attachment_with_its_payload_is_complete(tmp_path):
    path = tmp_path / "plain.eml"
    path.write_bytes(_with_attachment(b"Content-Type: text/plain\r\n", b"SYNTHETIC_TEXT"))
    msg = parse_email(path)
    assert msg is not None
    assert [(a.payload, a.payload_complete) for a in msg.attachments] == [(b"SYNTHETIC_TEXT", True)]


def test_a_nested_container_left_unserialized_is_incomplete(tmp_path):
    """Review round 1 on #1286: a container inside an attachment keeps
    the empty payload by design (only the outermost is serialized), with
    no cap counted. Under an extractable label (``.txt``) its empty bytes
    would read as a complete empty text, so it is marked incomplete; the
    outer container, serialized, is complete."""
    inner = (
        b"From: a@example.test\r\n"
        b'Content-Type: multipart/mixed; boundary="i"\r\n\r\n'
        b"--i\r\nContent-Type: text/plain\r\n\r\nINNER_BODY\r\n"
        b"--i\r\nContent-Type: message/rfc822\r\n" + _TXT_FILENAME + b"\r\n"
        b"From: b@example.test\r\n\r\nSYNTHETIC_TEXT_MARKER\r\n"
        b"--i--\r\n"
    )
    path = tmp_path / "nested.eml"
    path.write_bytes(_with_attachment(b"Content-Type: message/rfc822\r\n", inner))
    msg = parse_email(path)
    assert msg is not None
    assert [(a.payload != b"", a.payload_complete) for a in msg.attachments] == [
        (True, True),
        (False, False),
    ]
    assert msg.parse_caps == {}


@pytest.mark.parametrize(
    "body, payload, complete",
    [
        # No base64 characters at all: nothing decodes.
        (b"!!!!", b"", False),
        # Invalid characters skipped: a partial decode.
        (b"QUJD!!!!REVG", b"ABCDEF", False),
        # A truncated quantum: the last bytes are lost.
        (b"QUJDR", None, False),
        # Missing padding only: lossless.
        (b"QUJDREVGSA", b"ABCDEFH", True),
        # A valid empty payload.
        (b"", b"", True),
    ],
    ids=["no_base64", "invalid_chars", "bad_length", "padding_only", "valid_empty"],
)
def test_a_base64_decode_defect_marks_the_payload_incomplete(tmp_path, body, payload, complete):
    """Review round 2 on #1286: the stdlib decodes malformed base64
    leniently and records a defect on the part; a payload decoded with an
    invalid-character or invalid-length defect is not the attachment's
    bytes, so it is marked incomplete. A padding defect alone loses
    nothing."""
    path = tmp_path / "b64.eml"
    path.write_bytes(
        _with_attachment(
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n",
            body,
            _TXT_FILENAME,
        )
    )
    msg = parse_email(path)
    assert msg is not None
    [attachment] = msg.attachments
    if payload is not None:
        assert attachment.payload == payload
    assert attachment.payload_complete is complete


def test_a_defect_on_a_serialized_container_does_not_matter(tmp_path):
    """Only a leaf's own decode defects count: a serialized container's
    payload is its body, whatever defects its subparts carry."""
    inner = (
        b"From: a@example.test\r\n"
        b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!!\r\n"
    )
    path = tmp_path / "container.eml"
    path.write_bytes(_with_attachment(b"Content-Type: message/rfc822\r\n", inner))
    msg = parse_email(path)
    assert msg is not None
    assert [(a.payload != b"", a.payload_complete) for a in msg.attachments] == [(True, True)]


class TestContainerTransportDecodeLoss:
    """Review round 4 on #1286: a base64 attached email decoded leniently
    (``_decode_transport_form`` keeps today's decoder and bytes) is checked
    once more through the stdlib leaf decoder, as a diagnostic only; an
    invalid-character or invalid-length defect marks the container's
    payload incomplete. Quoted-printable is not checked (#1288)."""

    _INNER = b"From: a@example.test\r\nSubject: s\r\n\r\nSYNTHETIC_TEXT_MARKER body text\r\n"

    def _parse(self, tmp_path, monkeypatch, headers: bytes, body: bytes):
        from src import parser as parser_module

        calls: list[int] = []
        real = parser_module._base64_transport_lost

        def counting(data):
            calls.append(len(data))
            return real(data)

        monkeypatch.setattr(parser_module, "_base64_transport_lost", counting)
        path = tmp_path / "t.eml"
        path.write_bytes(_with_attachment(headers, body))
        msg = parse_email(path)
        assert msg is not None
        return msg, calls

    def test_a_clean_base64_attached_email_is_complete(self, tmp_path, monkeypatch):
        msg, calls = self._parse(
            tmp_path, monkeypatch, _BASE64_RFC822, base64.encodebytes(self._INNER)
        )
        [attachment] = msg.attachments
        assert attachment.payload and attachment.payload_complete is True
        assert len(calls) == 1

    def test_replaced_non_alphabet_characters_make_it_incomplete(self, tmp_path, monkeypatch):
        encoded = base64.encodebytes(self._INNER)
        lossy = encoded[:8] + b"!!!!" + encoded[12:]
        msg, calls = self._parse(tmp_path, monkeypatch, _BASE64_RFC822, lossy)
        [attachment] = msg.attachments
        # The returned bytes are today's lenient decode, unchanged.
        assert attachment.payload != b""
        assert attachment.payload_complete is False
        assert len(calls) == 1
        assert msg.parse_caps == {}

    def test_an_undecodable_transport_keeps_todays_behaviour(self, tmp_path, monkeypatch):
        msg, calls = self._parse(tmp_path, monkeypatch, _BASE64_RFC822, b"A")
        [attachment] = msg.attachments
        assert (attachment.payload, attachment.payload_complete) == (b"", False)
        assert msg.parse_caps == {"transport_decode": 1}
        # The diagnostic runs only on a transport that decoded.
        assert calls == []

    def test_quoted_printable_is_not_checked(self, tmp_path, monkeypatch):
        import quopri

        msg, calls = self._parse(
            tmp_path,
            monkeypatch,
            b"Content-Type: message/rfc822\r\nContent-Transfer-Encoding: quoted-printable\r\n",
            quopri.encodestring(self._INNER),
        )
        [attachment] = msg.attachments
        assert attachment.payload and attachment.payload_complete is True
        assert calls == []

    def test_the_check_runs_behind_the_decodable_budget(self, tmp_path, monkeypatch):
        """Over ``budget.decodable`` the transport is never decoded, so
        the diagnostic does not run either."""
        from src import parser as parser_module

        budget = parser_module._SerializationBudget
        monkeypatch.setattr(parser_module, "_SerializationBudget", lambda: budget(decodable=10))
        msg, calls = self._parse(
            tmp_path, monkeypatch, _BASE64_RFC822, base64.encodebytes(self._INNER)
        )
        assert msg.parse_caps == {"decoded_bytes": 1}
        assert calls == []

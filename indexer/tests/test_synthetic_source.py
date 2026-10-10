"""#1483: a synthetic non-Proton source through the ingestion handoff.

PLAN.md decision 45 puts provider policy and the transport (mbsync and
Maildir today) on one side of the bytes-plus-``SourceMetadata`` seam
(#1077) and the core on the other, and requires that the core never
needs a provider identity. These tests feed the seam what an ordinary
IMAP server would deliver: mailbox names that are not Proton's
(``Folders/*``, ``Labels/*``), no ``X-Pm-*`` headers, and missing
optional metadata. They record what ``parse_email_bytes`` and the
Maildir adapter produce today, including where the handoff stops short
of what a general source would need:

- Byte-identical copies in two folders share one claimant ID. The
  writer then keeps one ``messages`` row tracking a single path
  (``TestByteIdenticalCopies`` in ``test_reconciler.py``), so this is
  not independent occurrence storage; that is #1083.
- ``SourceMetadata.flags`` and ``SourceMetadata.size`` reach nothing:
  ``Message`` has no flag field, the writer reads the read / flagged /
  replied state back from the Maildir file name (``maildir.message_state``),
  and ``Message.size`` is the length of the bytes parsed (#1485).

The ``.eml`` files under ``fixtures/source`` are written by hand for
these tests, never derived from real mail, and use ``.example``
domains and role names only.
"""

import dataclasses
import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.parser import Message, SourceMetadata, parse_email, parse_email_bytes

_FIXTURES = Path(__file__).parent / "fixtures" / "source"
_EMLS = sorted(_FIXTURES.glob("*.eml"))
_MARKER = "SRCFIXTURE-1483"

# Mailbox names an ordinary IMAP server uses, none of them Proton's.
_FOLDERS = ("INBOX", "Sent Items", "Deleted Items", "Archive/2024", "INBOX/Projects")


def _raw(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _source(raw: bytes, folder: str = "INBOX", **overrides) -> SourceMetadata:
    fields = {
        "folder": folder,
        "flags": frozenset(),
        "size": len(raw),
        "mtime_ns": None,
        "path": f"synthetic://{folder}/1",
    }
    fields.update(overrides)
    return SourceMetadata(**fields)


def _parse(raw: bytes, source: SourceMetadata) -> Message:
    msg = parse_email_bytes(raw, source)
    assert msg is not None
    return msg


def test_the_fixture_set_is_present():
    assert [p.name for p in _EMLS] == [
        "ordinary-imap-no-optional.eml",
        "ordinary-imap-received.eml",
        "ordinary-imap-sent.eml",
    ]


@pytest.mark.parametrize("eml", _EMLS, ids=lambda p: p.name)
def test_fixtures_are_synthetic_and_carry_no_proton_headers(eml):
    """Each fixture's header block has no ``X-Pm-*`` field and every
    address in it is at a ``.example`` domain."""
    head = eml.read_bytes().split(b"\n\n", 1)[0].decode("ascii")
    names = [line.split(":", 1)[0] for line in head.splitlines() if line[:1] not in " \t"]
    assert names
    assert not [n for n in names if n.lower().startswith("x-pm-")]
    domains = re.findall(r"@([A-Za-z0-9.-]+)", head)
    assert domains
    assert all(d.endswith(".example") for d in domains)


class TestSourceMetadataHandoff:
    @pytest.mark.parametrize("folder", _FOLDERS)
    @pytest.mark.parametrize("eml", _EMLS, ids=lambda p: p.name)
    def test_folder_path_and_mtime_come_from_the_source(self, eml, folder, caplog):
        """The folder and path are the adapter's, copied verbatim; the
        size and content hash are the bytes'. The body is not logged."""
        caplog.set_level(logging.DEBUG)
        raw = eml.read_bytes()
        source = _source(raw, folder, mtime_ns=1_704_186_930_000_000_000)

        msg = _parse(raw, source)

        assert msg.folder == folder
        assert msg.filepath == source.path
        assert msg.mtime_ns == source.mtime_ns
        assert (msg.size, msg.content_hash) == (len(raw), hashlib.sha256(raw).hexdigest())
        assert _MARKER in msg.body_text
        assert _MARKER not in caplog.text

    def test_delivered_message_from_an_ordinary_server(self):
        """The ordinary path: addresses, the send date from ``Date:`` and
        the delivery time from the topmost ``Received:``."""
        raw = _raw("ordinary-imap-received.eml")
        msg = _parse(raw, _source(raw))

        assert msg.message_id == "synthetic-source-001@mail.example"
        assert msg.claimant_id == (
            f"synthetic-source-001@mail.example#{hashlib.sha256(raw).hexdigest()[:16]}"
        )
        assert msg.from_addr == "Sender One <sender-one@mail.example>"
        assert msg.to_addrs == ["Recipient <recipient@imap.example>"]
        assert msg.cc_addrs == ["Copy Reader <copy-reader@mail.example>"]
        assert (msg.date, msg.date_status) == (datetime(2024, 1, 2, 9, 15, tzinfo=UTC), "parsed")
        assert msg.occurred_at == datetime(2024, 1, 2, 9, 15, 30, tzinfo=UTC)

    def test_sent_copy_without_received_has_no_delivery_time(self):
        raw = _raw("ordinary-imap-sent.eml")
        msg = _parse(raw, _source(raw, "Sent Items"))

        assert msg.in_reply_to == "synthetic-source-001@mail.example"
        assert msg.references == ["synthetic-source-001@mail.example"]
        assert msg.date == datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
        assert msg.occurred_at is None

    def test_absent_metadata_stays_unknown(self, caplog):
        """No ``Date:``, no ``Received:`` and no mtime: the send date and
        delivery time stay unknown and nothing is filled in from another
        field. The unknown send date is logged with the source's path
        label, not the content."""
        caplog.set_level(logging.INFO)
        raw = _raw("ordinary-imap-no-optional.eml")
        source = _source(raw)
        msg = _parse(raw, source)

        assert msg.mtime_ns is None
        assert (msg.date, msg.date_status) == (None, "missing")
        assert msg.occurred_at is None
        assert msg.subject == "(no subject)"
        (line,) = [r for r in caplog.records if "Date header missing" in r.getMessage()]
        assert line.levelno == logging.WARNING
        assert source.path in line.getMessage()
        assert _MARKER not in caplog.text

    def test_a_known_mtime_is_not_taken_as_a_date(self):
        """A source mtime is recorded as ``mtime_ns`` only: it supplies
        neither the send date nor the delivery time."""
        raw = _raw("ordinary-imap-no-optional.eml")
        msg = _parse(raw, _source(raw, mtime_ns=1_704_186_930_000_000_000))

        assert msg.mtime_ns == 1_704_186_930_000_000_000
        assert msg.date is None
        assert msg.occurred_at is None

    @pytest.mark.parametrize("eml", _EMLS, ids=lambda p: p.name)
    def test_source_flags_and_size_do_not_reach_the_message(self, eml):
        """Today's handoff limit: ``Message`` has no flag field and its
        size is the bytes', so two sources that differ only in ``flags``
        and ``size`` give equal messages. The stored read / flagged /
        replied state comes from the Maildir file name instead."""
        raw = eml.read_bytes()
        plain = _parse(raw, _source(raw))
        flagged = _parse(raw, _source(raw, flags=frozenset("SFR"), size=len(raw) + 99))

        assert flagged == plain
        assert flagged.size == len(raw)
        assert not [f.name for f in dataclasses.fields(Message) if "flag" in f.name]


class TestMaildirAdapterNonProtonFolders:
    @pytest.mark.parametrize(
        ("rel_dir", "folder"),
        [
            ("INBOX", "INBOX"),
            ("Sent Items", "Sent Items"),
            ("INBOX/.Projects", "INBOX/Projects"),
            ("Archive/.2024/.Q1", "Archive/2024/Q1"),
        ],
    )
    def test_folder_is_read_back_from_the_legacy_layout(self, tmp_path, rel_dir, folder):
        """mbsync's ``SubFolders Legacy`` layout for non-Proton mailbox
        names reads back as the mailbox name, with the file's mtime."""
        path = tmp_path / rel_dir / "cur" / "1704186930.1.host:2,S"
        path.parent.mkdir(parents=True)
        path.write_bytes(_raw("ordinary-imap-received.eml"))

        msg = parse_email(path, maildir_root=tmp_path)

        assert msg is not None
        assert msg.folder == folder
        assert msg.filepath == str(path)
        assert msg.mtime_ns == path.stat().st_mtime_ns


class TestByteIdenticalCopiesAcrossFolders:
    def test_byte_identical_copies_in_two_folders_share_one_claimant_id(self, tmp_path):
        """The same bytes filed in two mailboxes give one claimant ID,
        because the key is the Message-ID plus the bytes' hash (#217).
        Each parse keeps its own folder and path, but the writer stores
        one ``messages`` row per claimant tracking a single path, so
        this shows the limit of today's handoff, not independent
        occurrence storage (#1083)."""
        raw = _raw("ordinary-imap-received.eml")
        first = tmp_path / "INBOX" / "cur" / "1704186930.1.host:2,S"
        second = tmp_path / "Archive" / ".2024" / "cur" / "1704186999.2.host:2,"
        for path in (first, second):
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)

        a = parse_email(first, maildir_root=tmp_path)
        b = parse_email(second, maildir_root=tmp_path)

        assert a is not None and b is not None
        assert a.claimant_id == b.claimant_id
        assert a.content_hash == b.content_hash
        assert (a.folder, b.folder) == ("INBOX", "Archive/2024")
        assert a.filepath != b.filepath

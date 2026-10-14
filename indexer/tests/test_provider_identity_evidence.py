"""#1316: Proton identity headers do not change today's evidence identity.

The Proton Mail Bridge writes ``X-Pm-Internal-Id`` (Proton's message
ID) into the messages it builds, and Gluon writes ``X-Pm-Gluon-Id``
(its own internal ID). A file can also carry them forged, duplicated
or not at all: Gluon keeps a failed APPEND's literal unchanged in
``Recovered Messages`` when it can parse it, the failure is not the
size limit and no literal with the same recovery hash is there already
(S7), and Bridge keeps existing ``X-Pm-Internal-Id``
fields below one that matches (see the "Proton identity evidence"
section of ``docs/architecture.md`` for the upstream source review). These tests pin what the index does with such files today,
before any proposed use of the headers (#1313):

- The headers are ordinary header evidence (#1079). Like every other
  top-level field they are kept in ``Message.headers`` and stored as
  ``message_headers`` rows of their own claimant, and nothing reads
  them as an identity. Two files that differ in anything get two
  claimant IDs (#217), each with its own rows in every table keyed by
  claimant ID, and neither header ever merges, overwrites or deletes
  another claimant's rows, in either arrival order, whether the
  headers sit at the top of the header block or below the others.
- Like any header fields, enough of them reach existing work caps: past
  the header budget (``MAX_STORED_HEADER_FIELDS`` top-level fields, or
  its per-field or total bytes) header capture stops
  and says so (``headers_budget``, ``headers_complete`` false), past
  ``MAX_ADDRESS_HEADER_FIELDS`` the address scan stops and says so
  (#1144), and above ``Date`` past ``SORT_HEADER_MAX_BYTES`` they leave
  a first full index's sort time unknown.
- Two byte versions of one logical message (a rebuild with a new MIME
  boundary) are two claimants with disjoint chunk IDs; reaping one
  leaves the other's chunks as they were and records the reaped
  claimant, so a citation is never moved to the other bytes.
- A recovery-style file with no Message-ID is dead-lettered with fixed
  text whatever identity headers it carries (the #1082 decision may
  change this; this test records today's behaviour only).
- No identity value reaches the log or ``last_error``, and the only
  stored copies are the ``message_headers`` rows of the file that
  carries them, removed when that claimant is reaped.
- A source with no Proton headers, in a folder outside Proton's layout,
  is indexed like any other message.

Every message here is written by the test, with ``.example`` domains
and synthetic identity values; nothing is derived from real mail.
"""

import dataclasses
import email
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from src import main, parser
from src.database import Database
from src.queue import REASON_INITIAL_SCAN
from src.reconciler import Reconciler, ReconcilerConfig
from src.threader import Threader

from tests.conftest import TEST_ACCOUNT, make_mock_embedder
from tests.test_main import _UNIT_VECTOR, _drain, _make_queue

# Synthetic identity values. They must never appear in a log line, a
# persisted error or a stored row other than their own claimant's
# ``message_headers`` rows.
_PM_ID = "SYNTHPMID1316aaaa=="
_PM_OTHER = "SYNTHPMID1316bbbb=="
_GLUON_ID = "SYNTHGLUON1316-0000-0000-0000-000000000001"
_GLUON_OTHER = "SYNTHGLUON1316-0000-0000-0000-000000000002"
_ID_MARKERS = ("SYNTHPMID1316", "SYNTHGLUON1316")


def _leaks(text: str, needles: tuple[str, ...] = _ID_MARKERS) -> bool:
    """Whether ``text`` holds one of ``needles`` in any case, so a copy
    normalized on the way (lowercased, case-folded) is still found."""
    folded = text.casefold()
    return any(n.casefold() in folded for n in needles)


def _eml(
    message_id: str | None,
    body: str,
    *,
    identity: tuple[tuple[str, str], ...] = (),
    identity_first: bool = False,
    boundary: str = "b1316one",
    subject: str = "Identity evidence",
    received: str | None = None,
) -> bytes:
    """A small multipart message: a text body and a text attachment,
    with display names on the addresses so every claimant-keyed table
    gets rows. ``identity`` is a list of extra ``(name, value)`` header
    fields, written in order (so a name can repeat), after
    ``Message-ID`` or, with ``identity_first``, at the top of the header
    block, above ``From``; ``boundary`` changes only the MIME
    representation. ``received``, when given, is the date of a
    ``Received:`` field written first, above everything else."""
    identity_fields = [f"{name}: {value}" for name, value in identity]
    headers = [
        "From: Sender One <sender@mail.example>",
        "To: Recipient Two <recipient@mail.example>",
        "Cc: Copied Three <copied@mail.example>",
        f"Subject: {subject}",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
    ]
    if message_id is not None:
        headers.append(f"Message-ID: <{message_id}>")
    headers = [*identity_fields, *headers] if identity_first else [*headers, *identity_fields]
    if received is not None:
        headers.insert(0, f"Received: from relay.mail.example by mx.mail.example; {received}")
    headers += ["MIME-Version: 1.0", f'Content-Type: multipart/mixed; boundary="{boundary}"']
    return (
        "\r\n".join(headers)
        + f"\r\n\r\n--{boundary}\r\n"
        + "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + f"{body}\r\n"
        + f"--{boundary}\r\n"
        + "Content-Type: text/plain; name=note.txt\r\n"
        + "Content-Disposition: attachment; filename=note.txt\r\n\r\n"
        + "attachment text epsilonword\r\n"
        + f"--{boundary}--\r\n"
    ).encode()


_INTERNAL = "X-Pm-Internal-Id"
_GLUON = "X-Pm-Gluon-Id"

# The second file's identity headers, against a first file that carries
# ``X-Pm-Internal-Id: _PM_ID`` and ``X-Pm-Gluon-Id: _GLUON_ID``.
_SECOND_IDENTITY: dict[str, tuple[tuple[str, str], ...]] = {
    "equal": ((_INTERNAL, _PM_ID), (_GLUON, _GLUON_ID)),
    "duplicate-internal-forged-first": ((_INTERNAL, _PM_ID), (_INTERNAL, _PM_OTHER)),
    "duplicate-internal-forged-second": ((_INTERNAL, _PM_OTHER), (_INTERNAL, _PM_ID)),
    "conflicting-internal": ((_INTERNAL, _PM_OTHER),),
    "duplicate-gluon": ((_GLUON, _GLUON_OTHER), (_GLUON, _GLUON_ID)),
    "gluon-only": ((_GLUON, _GLUON_ID),),
    "absent": (),
}
_FIRST_IDENTITY = ((_INTERNAL, _PM_ID), (_GLUON, _GLUON_ID))


class _Index:
    """A Maildir, a database and the queue path ``main`` uses."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.maildir = tmp_path / "maildir"
        self.inbox = self.maildir / "INBOX" / "cur"
        self.inbox.mkdir(parents=True)
        monkeypatch.setattr(main, "MAILDIR_PATH", self.maildir)
        self.db = Database(
            tmp_path / "mail.db",
            account=dataclasses.replace(TEST_ACCOUNT, maildir_root=self.maildir),
        )
        self.threader = Threader(self.db)
        self.embedder = make_mock_embedder(_UNIT_VECTOR)
        self.queue = _make_queue(self.db)

    def write(self, name: str, raw: bytes) -> Path:
        path = self.inbox / f"{name}:2,S"
        path.write_bytes(raw)
        return path

    def index(self, *paths: Path) -> None:
        # One file per drain: the arrival order is part of the shape.
        for path in paths:
            self.queue.enqueue(str(path), REASON_INITIAL_SCAN)
            _drain(self.queue, self.db, self.embedder, self.threader, batch_size=1)

    def reconciler(self, grace_days: int) -> Reconciler:
        return Reconciler(
            self.db,
            self.embedder,
            ReconcilerConfig(
                enabled=True,
                grace_days=grace_days,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
            maildir_root=self.maildir,
        )

    @staticmethod
    def trash(path: Path) -> Path:
        """Set the Maildir ``T`` flag on ``path``; the trashed path."""
        trashed = path.with_name(path.name + "T")
        path.rename(trashed)
        return trashed

    def reap(self, path: Path) -> None:
        """Trash ``path`` and reap it at once."""
        self.trash(path)
        reconciler = self.reconciler(grace_days=0)
        reconciler.sweep()
        reconciler.reap()

    def schema_claimant_tables(self) -> set[str]:
        """Every regular table with a ``claimant_id`` column, read from
        the schema so a new per-message table is covered without editing
        this test."""
        tables = [
            r[0]
            for r in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
            )
        ]
        return {
            t
            for t in tables
            if "claimant_id" in {c[1] for c in self.db._conn.execute(f'PRAGMA table_info("{t}")')}
        }

    def claimant_tables(self) -> list[str]:
        """``schema_claimant_tables`` but ``reaped_messages``, the reap
        record, which is checked on its own."""
        return sorted(self.schema_claimant_tables() - {"reaped_messages"})

    def rows_of(self, claimant: str) -> dict[str, list[tuple]]:
        """Every row, all columns, keyed by ``claimant``, per table."""
        return {
            table: sorted(
                tuple(r)
                for r in self.db._conn.execute(
                    f'SELECT * FROM "{table}" WHERE claimant_id = ?',  # nosec B608
                    (claimant,),
                )
            )
            for table in self.claimant_tables()
        }

    def chunks_of(self, claimant: str) -> dict[str, str]:
        """``chunk_id`` to text for ``claimant``'s chunks."""
        return {
            r["chunk_id"]: r["text"]
            for r in self.db._conn.execute(
                "SELECT chunk_id, text FROM message_chunks WHERE claimant_id = ?", (claimant,)
            )
        }

    def tables_holding(self, needles: tuple[str, ...]) -> set[str]:
        """Every regular table with a text or blob value containing one
        of ``needles``, in any case."""
        found: set[str] = set()
        tables = [
            r[0]
            for r in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
            )
        ]
        for table in tables:
            for row in self.db._conn.execute(f'SELECT * FROM "{table}"'):  # nosec B608
                for value in row:
                    if isinstance(value, bytes):
                        value = value.decode("latin-1")
                    if isinstance(value, str) and _leaks(value, needles):
                        found.add(table)
        return found

    def identity_rows(self) -> dict[str, list[tuple[str, str, str | None]]]:
        """Per claimant, the ``(name, raw_value, decoded_value)`` of
        every ``message_headers`` row holding an identity marker, in
        source order."""
        rows: dict[str, list[tuple[str, str, str | None]]] = {}
        for r in self.db._conn.execute(
            "SELECT claimant_id, name, raw_value, decoded_value FROM message_headers "
            "ORDER BY claimant_id, ordinal"
        ):
            values = f"{r['raw_value']}\0{r['decoded_value']}"
            if _leaks(values):
                rows.setdefault(r["claimant_id"], []).append(
                    (r["name"], r["raw_value"], r["decoded_value"])
                )
        return rows

    def assert_identity_stored_only_as_header_evidence(
        self, expected: dict[str, tuple[tuple[str, str], ...]]
    ) -> None:
        """The identity markers are stored nowhere but ``message_headers``,
        and there exactly as the fields ``expected`` lists per claimant
        (a claimant without identity fields omitted). These fields are
        plain text, so the decoded value equals the raw one."""
        holding = self.tables_holding(_ID_MARKERS)
        assert holding == ({"message_headers"} if any(expected.values()) else set())
        assert self.identity_rows() == {
            c: [(name, value, value) for name, value in f] for c, f in expected.items() if f
        }


def _claimant(path: Path) -> str:
    msg = parser.parse_email(path)
    assert msg is not None
    return msg.claimant_id


# Every ``Message`` field, none of them a provider identity (#1316, T1).
_MESSAGE_FIELDS = [
    "message_id",
    "in_reply_to",
    "references",
    "subject",
    "from_addr",
    "to_addrs",
    "cc_addrs",
    "date",
    "body_text",
    "folder",
    "filepath",
    "attachments",
    "has_attachments",
    "from_addrs",
    "bcc_addrs",
    "size",
    "mtime_ns",
    "content_hash",
    "date_status",
    "first_indexed_at",
    "occurred_at",
    "sender_ambiguous",
    "participant_names",
    "participant_names_complete",
    "headers",
    "headers_complete",
    "subject_complete",
    "from_addresses_complete",
    "to_addresses_complete",
    "cc_addresses_complete",
    "bcc_addresses_complete",
    "attachments_manifest_complete",
    "body_complete",
    "parse_caps",
]

_SOURCE = parser.SourceMetadata(
    folder="INBOX", flags=frozenset("S"), size=0, mtime_ns=None, path="synthetic://1"
)


def _top_level_fields(raw: bytes) -> int:
    """The header fields of ``raw``'s top-level header block."""
    return len(email.message_from_bytes(raw).keys())


def _written_fields(raw: bytes) -> list[tuple[str, str]]:
    """The ``(name, value)`` fields ``_eml`` wrote into ``raw``'s
    top-level header block, in order (it writes one unfolded line per
    field), read without the parser under test."""
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    return [(name, value) for name, value in (line.split(": ", 1) for line in head.split("\r\n"))]


def _filler(count: int) -> tuple[tuple[str, str], ...]:
    """``count`` distinct ``X-Pm-Internal-Id`` fields."""
    return tuple((_INTERNAL, f"SYNTHPMID1316{i:06d}==") for i in range(count))


def _kept_fields(written: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The leading fields of ``written`` within the documented header
    budget (#1079): at most ``MAX_STORED_HEADER_FIELDS`` fields, none
    over ``MAX_STORED_HEADER_VALUE_BYTES`` (name plus value) and at most
    ``MAX_STORED_HEADER_BYTES`` in all; capture stops at the first field
    over any of them."""
    kept: list[tuple[str, str]] = []
    spent = 0
    for name, value in written:
        size = len(name.encode()) + len(value.encode())
        if (
            len(kept) >= parser.MAX_STORED_HEADER_FIELDS
            or size > parser.MAX_STORED_HEADER_VALUE_BYTES
            or spent + size > parser.MAX_STORED_HEADER_BYTES
        ):
            break
        spent += size
        kept.append((name, value))
    return kept


def _assert_only_bytes_and_headers_differ(
    tagged: parser.Message, tagged_raw: bytes, plain: parser.Message, plain_raw: bytes
) -> None:
    """``tagged``'s ``size`` and ``content_hash`` are those of all of
    ``tagged_raw``, so its claimant ID differs from ``plain``'s exactly
    when the bytes differ. Its ``headers`` are the fields written into
    ``tagged_raw``, identity fields included, each as an ordinary
    ``MessageHeader`` at its source ordinal, within the header budget
    (``_kept_fields``); past it the capture stops, says so
    (``headers_complete`` false, ``headers_budget`` counted) and keeps
    the earlier fields. Every other compared field equals ``plain``'s
    (``first_indexed_at``, the clock at parse time, is not compared)."""
    assert (tagged.size, tagged.content_hash) == (
        len(tagged_raw),
        hashlib.sha256(tagged_raw).hexdigest(),
    )
    assert (tagged.claimant_id == plain.claimant_id) == (tagged_raw == plain_raw)
    written = _written_fields(tagged_raw)
    kept = _kept_fields(written)
    assert [(h.ordinal, h.name, h.raw_value, h.decoded_value) for h in tagged.headers] == [
        (i, name, value, value) for i, (name, value) in enumerate(kept)
    ]
    assert all(h.name_normalized == h.name.lower() for h in tagged.headers)
    capped = len(kept) < len(written)
    assert tagged.headers_complete is (not capped)
    assert tagged.parse_caps.get("headers_budget", 0) == int(capped)
    # The plain message is under every cap, so its headers are complete.
    assert plain.headers_complete is True and "headers_budget" not in plain.parse_caps
    assert (
        dataclasses.replace(
            tagged,
            size=plain.size,
            content_hash=plain.content_hash,
            headers=plain.headers,
            headers_complete=plain.headers_complete,
            parse_caps={k: v for k, v in tagged.parse_caps.items() if k != "headers_budget"},
        )
        == plain
    )


def _cap_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "parser work caps" in r.getMessage()
    ]


class TestParserIgnoresIdentityHeaders:
    @pytest.mark.parametrize("identity_first", [False, True], ids=["after-ids", "top"])
    @pytest.mark.parametrize("shape", sorted(_SECOND_IDENTITY))
    def test_identity_headers_change_only_the_bytes_and_the_header_list(
        self, shape, identity_first, caplog
    ):
        """Parsed from the same source metadata, a message with identity
        headers (after its other headers, or at the top above ``From``)
        and one without differ only in the fields taken from the bytes
        themselves (``size`` and ``content_hash``, so the claimant ID) and
        in ``headers``, which keeps the identity fields as ordinary header
        evidence at their source position; every other compared
        ``Message`` field is equal, ``Message`` has no provider-identity
        field, and no identity value is held outside ``headers`` or
        logged."""
        caplog.set_level(logging.DEBUG)
        plain_raw = _eml("evidence@mail.example", "Same body.")
        tagged_raw = _eml(
            "evidence@mail.example",
            "Same body.",
            identity=_SECOND_IDENTITY[shape],
            identity_first=identity_first,
        )

        plain = parser.parse_email_bytes(plain_raw, _SOURCE)
        tagged = parser.parse_email_bytes(tagged_raw, _SOURCE)

        assert plain is not None and tagged is not None
        _assert_only_bytes_and_headers_differ(tagged, tagged_raw, plain, plain_raw)
        if shape == "absent":
            assert tagged.claimant_id == plain.claimant_id
        else:
            assert tagged.claimant_id != plain.claimant_id
        identity = [
            (h.name, h.raw_value)
            for h in tagged.headers
            if _leaks(f"{h.raw_value}\0{h.decoded_value}")
        ]
        assert identity == list(_SECOND_IDENTITY[shape])
        assert not _leaks(repr(dataclasses.replace(tagged, headers=[])))
        # Pinned, so a new ``Message`` field (a provider identity among
        # them, even one equal on both messages) fails here until it is
        # classified and this claim rechecked.
        assert [f.name for f in dataclasses.fields(parser.Message)] == _MESSAGE_FIELDS
        assert not _leaks(caplog.text)

    @pytest.mark.parametrize("identity_first", [False, True], ids=["after-ids", "top"])
    def test_identity_headers_up_to_the_stored_header_cap_are_all_kept(self, identity_first):
        """With exactly ``MAX_STORED_HEADER_FIELDS`` top-level fields in
        all, every field is kept, identity headers included, and header
        evidence is complete, wherever the identity headers sit."""
        plain_raw = _eml("evidence@mail.example", "Same body.")
        count = parser.MAX_STORED_HEADER_FIELDS - _top_level_fields(plain_raw)
        tagged_raw = _eml(
            "evidence@mail.example",
            "Same body.",
            identity=_filler(count),
            identity_first=identity_first,
        )
        assert _top_level_fields(tagged_raw) == parser.MAX_STORED_HEADER_FIELDS

        plain = parser.parse_email_bytes(plain_raw, _SOURCE)
        tagged = parser.parse_email_bytes(tagged_raw, _SOURCE)

        assert plain is not None and tagged is not None
        _assert_only_bytes_and_headers_differ(tagged, tagged_raw, plain, plain_raw)
        assert tagged.headers_complete is True and tagged.parse_caps == {}

    @pytest.mark.parametrize("identity_first", [False, True], ids=["after-ids", "top"])
    @pytest.mark.parametrize(
        "budget",
        ["fields-one-over", "fields-at-address-scan-cap", "field-bytes", "total-bytes"],
    )
    def test_identity_headers_past_the_stored_header_budget_are_counted(
        self, caplog, identity_first, budget
    ):
        """Past the header budget (more than ``MAX_STORED_HEADER_FIELDS``
        top-level fields, one field over ``MAX_STORED_HEADER_VALUE_BYTES``
        or more than ``MAX_STORED_HEADER_BYTES`` in all), header capture
        stops at the first field over it: the earlier fields are kept, the
        rest (the From, To and Date fields too, when identity headers fill
        the capture above them) are not stored, ``headers_complete`` is
        false and the cap is counted as ``headers_budget`` in
        ``parse_caps`` and a WARNING. Up to ``MAX_ADDRESS_HEADER_FIELDS``
        fields in all, the address scan, subject, dates, body and
        attachments are those of the message without identity headers."""
        caplog.set_level(logging.DEBUG)
        plain_raw = _eml("evidence@mail.example", "Same body.")
        base = _top_level_fields(plain_raw)
        identity = {
            "fields-one-over": _filler(parser.MAX_STORED_HEADER_FIELDS + 1 - base),
            "fields-at-address-scan-cap": _filler(parser.MAX_ADDRESS_HEADER_FIELDS - base),
            "field-bytes": (
                (_INTERNAL, "SYNTHPMID1316" + "a" * parser.MAX_STORED_HEADER_VALUE_BYTES),
            ),
            # Each field within its own limit, below the field count, over
            # the total.
            "total-bytes": tuple(
                (_INTERNAL, f"SYNTHPMID1316{i:06d}" + "a" * 15_000) for i in range(80)
            ),
        }[budget]
        tagged_raw = _eml(
            "evidence@mail.example",
            "Same body.",
            identity=identity,
            identity_first=identity_first,
        )
        written = _written_fields(tagged_raw)
        kept = _kept_fields(written)
        # Each layout trips the dimension it names, and only that one.
        assert {
            "fields-one-over": len(written) > parser.MAX_STORED_HEADER_FIELDS == len(kept),
            "fields-at-address-scan-cap": len(written) == parser.MAX_ADDRESS_HEADER_FIELDS,
            "field-bytes": len(written) < parser.MAX_STORED_HEADER_FIELDS
            and max(len(n) + len(v) for n, v in written) > parser.MAX_STORED_HEADER_VALUE_BYTES,
            "total-bytes": len(written) < parser.MAX_STORED_HEADER_FIELDS
            and max(len(n) + len(v) for n, v in written) < parser.MAX_STORED_HEADER_VALUE_BYTES
            and sum(len(n) + len(v) for n, v in written) > parser.MAX_STORED_HEADER_BYTES,
        }[budget]

        plain = parser.parse_email_bytes(plain_raw, _SOURCE)
        tagged = parser.parse_email_bytes(tagged_raw, _SOURCE)

        assert plain is not None and tagged is not None
        _assert_only_bytes_and_headers_differ(tagged, tagged_raw, plain, plain_raw)
        assert len(tagged.headers) == len(kept) < len(written)
        assert tagged.headers_complete is False
        assert tagged.parse_caps == {"headers_budget": 1}
        stored = {h.name_normalized for h in tagged.headers}
        from_index = [name for name, _ in written].index("From")
        assert ("from" in stored) is (from_index < len(kept))
        assert tagged.from_addr == plain.from_addr and tagged.from_addresses_complete
        warnings = _cap_warnings(caplog)
        assert len(warnings) == 1 and warnings[0].endswith(": headers_budget=1")
        assert not _leaks(caplog.text)

    @pytest.mark.parametrize(
        ("layout", "addresses_kept"),
        [("one-over-after-ids", True), ("one-over-top", True), ("fill-scan-top", False)],
    )
    def test_identity_headers_past_the_address_scan_cap_are_counted(
        self, caplog, layout, addresses_kept
    ):
        """Past ``MAX_ADDRESS_HEADER_FIELDS`` top-level fields, the From /
        To / Cc scan stops (#1144), wherever the identity headers sit. The
        cap is counted (``parse_caps``, a WARNING with its count), the
        sender is flagged ambiguous and all three address roles
        incomplete. Address headers before the cut-off are kept; identity
        headers that fill the scan above ``From`` lose every address.
        Header capture stopped long before (``headers_budget``). The
        body, attachments, subject and dates are unchanged."""
        caplog.set_level(logging.DEBUG)
        plain_raw = _eml("evidence@mail.example", "Same body.")
        if layout == "fill-scan-top":
            count = parser.MAX_ADDRESS_HEADER_FIELDS
        else:
            count = parser.MAX_ADDRESS_HEADER_FIELDS - _top_level_fields(plain_raw) + 1
        tagged_raw = _eml(
            "evidence@mail.example",
            "Same body.",
            identity=_filler(count),
            identity_first=layout != "one-over-after-ids",
        )

        plain = parser.parse_email_bytes(plain_raw, _SOURCE)
        tagged = parser.parse_email_bytes(tagged_raw, _SOURCE)

        assert plain is not None and tagged is not None
        assert tagged.parse_caps == {"address_fields": 1, "headers_budget": 1}
        assert tagged.sender_ambiguous and not plain.sender_ambiguous
        assert not (
            tagged.from_addresses_complete
            or tagged.to_addresses_complete
            or tagged.cc_addresses_complete
        )
        if addresses_kept:
            kept = (
                plain.from_addr,
                plain.from_addrs,
                plain.to_addrs,
                plain.cc_addrs,
                plain.participant_names,
            )
        else:
            kept = ("", [], [], [], [])
        assert plain.cc_addrs and plain.to_addrs and plain.from_addrs
        assert (
            tagged.from_addr,
            tagged.from_addrs,
            tagged.to_addrs,
            tagged.cc_addrs,
            tagged.participant_names,
        ) == kept
        # Nothing else the parser returns changes.
        _assert_only_bytes_and_headers_differ(
            dataclasses.replace(
                tagged,
                from_addr=plain.from_addr,
                from_addrs=plain.from_addrs,
                to_addrs=plain.to_addrs,
                cc_addrs=plain.cc_addrs,
                participant_names=plain.participant_names,
                sender_ambiguous=plain.sender_ambiguous,
                from_addresses_complete=plain.from_addresses_complete,
                to_addresses_complete=plain.to_addresses_complete,
                cc_addresses_complete=plain.cc_addresses_complete,
                bcc_addresses_complete=plain.bcc_addresses_complete,
                parse_caps={k: v for k, v in tagged.parse_caps.items() if k != "address_fields"},
            ),
            tagged_raw,
            plain,
            plain_raw,
        )
        warnings = _cap_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].endswith(": address_fields=1,headers_budget=1")
        assert not _leaks(caplog.text)

    @pytest.mark.parametrize(
        ("identity_first", "past_the_read", "with_received"),
        [(False, True, False), (True, False, False), (True, True, False), (True, True, True)],
        ids=["after-ids", "top-within-read", "top-past-read", "top-past-read-received"],
    )
    def test_identity_headers_above_the_date_past_the_sort_read_hide_it(
        self, tmp_path, monkeypatch, identity_first, past_the_read, with_received
    ):
        """``message_sort_time``, which orders a first full index, reads
        only the first ``SORT_HEADER_MAX_BYTES`` of the header block and
        takes the topmost ``Received:`` date before ``Date``. Identity
        headers below the ``Date`` leave it alone; identity headers above
        it that push it past that read, in a file with no ``Received:``
        within the read, leave the sort time unknown, and the oldest-first
        walk then queues the file due at the walk's start time instead of
        its date. A ``Received:`` above them still gives the sort time."""
        idx = _Index(tmp_path, monkeypatch)
        plain_raw = _eml("plain@mail.example", "Same body.")
        field = len(f"{_INTERNAL}: SYNTHPMID1316000000==\r\n")
        count = (parser.SORT_HEADER_MAX_BYTES // field + 1) if past_the_read else 10
        tagged_raw = _eml(
            "tagged@mail.example",
            "Same body.",
            identity=_filler(count),
            identity_first=identity_first,
            received="Tue, 02 Jan 2024 08:00:00 +0000" if with_received else None,
        )
        plain = idx.write("1700000000.M1.host", plain_raw)
        tagged = idx.write("1700000001.M2.host", tagged_raw)
        date_past_read = identity_first and past_the_read
        assert (tagged_raw.index(b"\r\nDate:") > parser.SORT_HEADER_MAX_BYTES) == date_past_read
        hidden = date_past_read and not with_received

        before = datetime.now(UTC)
        main._enqueue_unindexed_messages(
            idx.db, idx.queue, idx.maildir, REASON_INITIAL_SCAN, oldest_first=True
        )
        after = datetime.now(UTC)

        dated = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        received = datetime(2024, 1, 2, 8, 0, tzinfo=UTC)
        tagged_time = None if hidden else (received if with_received else dated)
        assert parser.message_sort_time(plain) == dated
        assert parser.message_sort_time(tagged) == tagged_time
        due = {
            r["filepath"]: datetime.fromisoformat(r["next_attempt_at"])
            for r in idx.db._conn.execute("SELECT filepath, next_attempt_at FROM indexing_jobs")
        }
        assert due[str(plain)] == dated
        if hidden:
            assert before <= due[str(tagged)] <= after
        else:
            assert due[str(tagged)] == tagged_time


class TestClaimantsStayIndependent:
    @pytest.mark.parametrize("order", ["first-then-second", "second-then-first"])
    @pytest.mark.parametrize("same_message_id", [True, False], ids=["same-mid", "other-mid"])
    @pytest.mark.parametrize("identity_first", [False, True], ids=["after-ids", "top"])
    @pytest.mark.parametrize("shape", sorted(_SECOND_IDENTITY))
    def test_identity_headers_never_merge_overwrite_or_delete(
        self, tmp_path, monkeypatch, caplog, shape, identity_first, same_message_id, order
    ):
        """Two files with different bytes, whatever identity headers they carry
        (equal, forged, duplicated, conflicting, absent; after the other
        headers or at the top) and whatever their Message-IDs, are two
        claimants, each with its own rows in every table keyed by
        claimant ID. Trashing the first tombstones only its claimant ID
        and, within the grace window, changes no row of either; reaping
        it then removes all of its rows but the ``reaped_messages``
        record it adds and leaves every row of the
        second exactly as it was. Each file's identity fields are stored
        only as its own ``message_headers`` rows (ordinary header
        evidence, complete here), so reaping the first removes its copies
        and leaves the second's. Every table with a ``claimant_id``
        column in the schema holds rows at some step."""
        caplog.set_level(logging.DEBUG)
        idx = _Index(tmp_path, monkeypatch)
        first = idx.write(
            "1700000000.M1.host",
            _eml(
                "first@mail.example",
                "First wording alphaword.",
                identity=_FIRST_IDENTITY,
                subject="First subject",
            ),
        )
        second = idx.write(
            "1700000001.M2.host",
            _eml(
                "first@mail.example" if same_message_id else "second@mail.example",
                "Second wording betaword.",
                identity=_SECOND_IDENTITY[shape],
                identity_first=identity_first,
                subject="Second subject",
            ),
        )
        idx.index(*((first, second) if order == "first-then-second" else (second, first)))

        a, b = _claimant(first), _claimant(second)
        assert a != b
        rows_a, rows_b = idx.rows_of(a), idx.rows_of(b)
        # Every per-message table AGENTS.md lists has rows for both.
        populated = {
            "messages",
            "message_thread_map",
            "message_participants",
            "message_participant_names",
            "message_headers",
            "message_chunks",
            "attachments",
        }
        assert populated <= set(idx.claimant_tables())
        for table in populated:
            assert rows_a[table] and rows_b[table], table
        paths = {
            r["claimant_id"]: r["filepath"]
            for r in idx.db._conn.execute("SELECT claimant_id, filepath FROM messages")
        }
        assert paths == {a: str(first), b: str(second)}
        text_a = " ".join(idx.chunks_of(a).values())
        text_b = " ".join(idx.chunks_of(b).values())
        assert "alphaword" in text_a and "betaword" not in text_a
        assert "betaword" in text_b and "alphaword" not in text_b
        observed = {t for rows in (rows_a, rows_b) for t, r in rows.items() if r}
        both = {a: _FIRST_IDENTITY, b: _SECOND_IDENTITY[shape]}
        idx.assert_identity_stored_only_as_header_evidence(both)
        complete = idx.db._conn.execute("SELECT headers_complete FROM messages").fetchall()
        assert [r[0] for r in complete] == [1, 1]

        # Trash the first and hold it in the grace window: the tombstone
        # names its claimant ID, never the bare Message-ID or the other
        # claimant, and no row of either claimant is removed.
        trashed = idx.trash(first)
        held = idx.reconciler(grace_days=1)
        held.sweep()
        held.reap()
        tombstones = [
            (r["filepath"], r["claimant_id"], r["thread_id"])
            for r in idx.db._conn.execute(
                "SELECT filepath, claimant_id, thread_id FROM pending_deletions"
            )
        ]
        (thread_a,) = idx.db._conn.execute(
            "SELECT thread_id FROM messages WHERE claimant_id = ?", (a,)
        ).fetchone()
        assert tombstones == [(str(trashed), a, thread_a)]
        held_a = idx.rows_of(a)
        assert held_a.pop("pending_deletions") == [(str(trashed), a, thread_a, mock.ANY)]
        # Every other row of the first claimant is as it was, apart from
        # the sweep following the flag rename to the trashed path. The
        # mailbox occurrence stays where it was: a sweep infers the rename
        # from the uniq alone, a missed move left to reconciliation
        # (#1566, #1567).

        def follow(table: str, row: tuple) -> tuple:
            if table == "message_occurrences":
                return row
            return tuple(str(trashed) if v == str(first) else v for v in row)

        renamed = {
            table: sorted(follow(table, row) for row in rows)
            for table, rows in rows_a.items()
            if table != "pending_deletions"
        }
        assert held_a == renamed
        assert idx.rows_of(b) == rows_b
        assert not idx.db._conn.execute("SELECT 1 FROM reaped_messages").fetchall()
        idx.assert_identity_stored_only_as_header_evidence(both)
        observed |= {t for t, r in held_a.items() if r} | {"pending_deletions"}

        idx.reconciler(grace_days=0).reap()

        assert not any(idx.rows_of(a).values())
        # Every row of the survivor, in every claimant-keyed table, is
        # exactly as it was.
        assert idx.rows_of(b) == rows_b
        assert idx.db.is_indexed(str(second))
        reaped = idx.db._conn.execute("SELECT claimant_id FROM reaped_messages").fetchall()
        assert [r["claimant_id"] for r in reaped] == [a]
        observed.add("reaped_messages")
        # Each claimant-keyed table in the schema was seen holding rows,
        # so none of the checks above passed on an empty table. A new
        # table needs rows in this test, or an exclusion with its reason.
        assert observed == idx.schema_claimant_tables()
        # No identity value is logged, and the only stored copies left
        # are the survivor's own header rows.
        assert not _leaks(caplog.text)
        idx.assert_identity_stored_only_as_header_evidence({b: _SECOND_IDENTITY[shape]})

    def test_identity_headers_past_the_address_scan_cap_index_as_their_own_claimant(
        self, tmp_path, monkeypatch, caplog
    ):
        """A file whose identity headers fill the address scan above
        ``From`` (``MAX_ADDRESS_HEADER_FIELDS``, #1144) is still its own
        claimant: indexed with the sender flagged ambiguous, all three
        address roles incomplete, both caps in ``caps_json``, no
        participant rows, header evidence incomplete with only the first
        ``MAX_STORED_HEADER_FIELDS`` fields stored, while the other
        file's rows are unchanged; and reaping it removes those header
        rows and leaves the other file's rows unchanged."""
        caplog.set_level(logging.DEBUG)
        idx = _Index(tmp_path, monkeypatch)
        first = idx.write(
            "1700000000.M1.host",
            _eml("first@mail.example", "First wording alphaword.", identity=_FIRST_IDENTITY),
        )
        idx.index(first)
        a = _claimant(first)
        rows_a = idx.rows_of(a)
        capped = idx.write(
            "1700000001.M2.host",
            _eml(
                "first@mail.example",
                "Capped wording betaword.",
                identity=_filler(parser.MAX_ADDRESS_HEADER_FIELDS),
                identity_first=True,
            ),
        )
        idx.index(capped)

        b = _claimant(capped)
        assert b != a
        (row,) = idx.db._conn.execute(
            "SELECT sender_ambiguous, from_addresses_complete, to_addresses_complete, "
            "cc_addresses_complete, headers_complete, caps_json "
            "FROM messages WHERE claimant_id = ?",
            (b,),
        ).fetchall()
        assert tuple(row)[:5] == (1, 0, 0, 0, 0)
        assert json.loads(row["caps_json"]) == {"address_fields": 1, "headers_budget": 1}
        rows_b = idx.rows_of(b)
        assert not rows_b["message_participants"] and not rows_b["message_participant_names"]
        assert "betaword" in " ".join(idx.chunks_of(b).values())
        assert idx.rows_of(a) == rows_a
        kept = _filler(parser.MAX_ADDRESS_HEADER_FIELDS)[: parser.MAX_STORED_HEADER_FIELDS]
        idx.assert_identity_stored_only_as_header_evidence({a: _FIRST_IDENTITY, b: kept})
        assert len(rows_b["message_headers"]) == parser.MAX_STORED_HEADER_FIELDS

        idx.reap(capped)

        assert not any(idx.rows_of(b).values())
        assert idx.rows_of(a) == rows_a
        assert not _leaks(caplog.text)
        idx.assert_identity_stored_only_as_header_evidence({a: _FIRST_IDENTITY})


class TestEncodedIdentityHeaderThroughTheWriter:
    def test_encoded_word_identity_is_stored_raw_and_decoded(self, tmp_path, monkeypatch):
        """An identity header written as an RFC 2047 encoded word goes
        through the queue and writer into ``message_headers`` with its
        wire text as ``raw_value`` and the decoded ID as
        ``decoded_value``, which the header predicates match first."""
        idx = _Index(tmp_path, monkeypatch)
        encoded = "=?utf-8?q?SYNTHPMID1316eeee=3D=3D?="
        path = idx.write(
            "1700000000.M1.host",
            _eml("encoded@mail.example", "Encoded wording.", identity=((_INTERNAL, encoded),)),
        )
        idx.index(path)

        claimant = _claimant(path)
        assert idx.identity_rows() == {claimant: [(_INTERNAL, encoded, "SYNTHPMID1316eeee==")]}
        assert idx.tables_holding(_ID_MARKERS) == {"message_headers"}


class TestByteVersionsOfOneLogicalMessage:
    def test_rebuilt_representation_is_a_separate_source(self, tmp_path, monkeypatch):
        """Bridge builds multipart messages with random boundaries, so one
        Proton message can reach the Maildir as different bytes. Two such
        versions (same Message-ID, same ``X-Pm-Internal-Id``, same text,
        new boundary) are two claimants with disjoint chunk IDs. Reaping
        the first removes its chunks and records its claimant as reaped;
        the second's chunk IDs are untouched, so a citation of the first
        is never moved to the second's bytes. Each version keeps its own
        copy of the identity headers as header evidence, so a header
        predicate counts two messages, not one logical message."""
        idx = _Index(tmp_path, monkeypatch)
        body = "Unchanged wording gammaword."
        older = idx.write(
            "1700000000.M1.host",
            _eml("version@mail.example", body, identity=_FIRST_IDENTITY, boundary="b1316one"),
        )
        newer = idx.write(
            "1700000001.M2.host",
            _eml("version@mail.example", body, identity=_FIRST_IDENTITY, boundary="b1316two"),
        )
        idx.index(older, newer)

        a, b = _claimant(older), _claimant(newer)
        assert a != b
        chunks_a = idx.chunks_of(a)
        chunks_b = idx.chunks_of(b)
        assert chunks_a and chunks_b
        assert not set(chunks_a) & set(chunks_b)
        assert sorted(chunks_a.values()) == sorted(chunks_b.values())
        (thread,) = idx.db._conn.execute("SELECT message_ids FROM threads").fetchall()
        assert sorted(json.loads(thread["message_ids"])) == sorted([a, b])
        idx.assert_identity_stored_only_as_header_evidence({a: _FIRST_IDENTITY, b: _FIRST_IDENTITY})

        idx.reap(older)

        remaining = {
            r["chunk_id"] for r in idx.db._conn.execute("SELECT chunk_id FROM message_chunks")
        }
        assert remaining == set(chunks_b)
        assert not remaining & set(chunks_a)
        reaped = idx.db._conn.execute("SELECT claimant_id, message_id FROM reaped_messages")
        assert [tuple(r) for r in reaped] == [(a, "version@mail.example")]
        idx.assert_identity_stored_only_as_header_evidence({b: _FIRST_IDENTITY})


class TestIdLessRecoveryStyleInput:
    @pytest.mark.parametrize("identity_first", [False, True], ids=["after-ids", "top"])
    @pytest.mark.parametrize("shape", sorted(_SECOND_IDENTITY))
    def test_no_message_id_is_dead_lettered_whatever_identity_it_carries(
        self, tmp_path, monkeypatch, caplog, shape, identity_first
    ):
        """Gluon keeps a failed APPEND's literal unchanged (when it
        parses, is within the size limit and is not a recovery-hash
        duplicate, S7), so a ``Recovered Messages`` file need not have the Message-ID Bridge
        synthesizes for messages it builds. Today the parser returns
        ``None`` for it and the worker dead-letters it with fixed text;
        an identity header does not make it indexable, in any of the
        identity-header shapes, above or below the other headers.
        Recorded apart from the #1082 decision on content-hash
        identity."""
        caplog.set_level(logging.DEBUG)
        idx = _Index(tmp_path, monkeypatch)
        path = idx.write(
            "1700000000.M1.host",
            _eml(
                None,
                "Recovered deltaword.",
                identity=_SECOND_IDENTITY[shape],
                identity_first=identity_first,
            ),
        )

        assert parser.parse_email(path) is None
        idx.index(path)

        row = idx.db._conn.execute(
            "SELECT status, last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
            (str(path),),
        ).fetchone()
        assert tuple(row) == (
            "dead",
            "parse",
            "unindexable: no Message-ID or one over 998 characters",
        )
        assert idx.db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert not idx.db.is_indexed(str(path))
        assert not _leaks(caplog.text)
        assert "deltaword" not in caplog.text
        assert idx.tables_holding((*_ID_MARKERS, "deltaword")) == set()


class TestGenericSourceIndexes:
    @pytest.mark.parametrize(
        ("fixture", "sent_at_status"),
        [("ordinary-imap-received.eml", "parsed"), ("ordinary-imap-no-optional.eml", "missing")],
    )
    def test_source_without_proton_headers_is_indexed(
        self, tmp_path, monkeypatch, fixture, sent_at_status
    ):
        """#1483's ordinary-IMAP fixtures (no ``X-Pm-*`` header), filed in
        a folder outside Proton's layout, go through the queue and writer
        like any message: one ``messages`` row in that folder, its
        participants and its body chunks; a missing send date stays
        unknown."""
        idx = _Index(tmp_path, monkeypatch)
        raw = (Path(__file__).parent / "fixtures" / "source" / fixture).read_bytes()
        assert b"X-Pm-" not in raw
        path = idx.maildir / "Sent Items" / "cur" / "1700000000.M1.host:2,S"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)

        idx.index(path)

        claimant = _claimant(path)
        (row,) = idx.db._conn.execute(
            "SELECT claimant_id, folder, sent_at, sent_at_status FROM messages"
        ).fetchall()
        assert (row["claimant_id"], row["folder"]) == (claimant, "Sent Items")
        assert row["sent_at_status"] == sent_at_status
        assert (row["sent_at"] is None) == (sent_at_status == "missing")
        assert idx.rows_of(claimant)["message_participants"]
        assert "SRCFIXTURE-1483" in " ".join(idx.chunks_of(claimant).values())
        assert idx.db.is_indexed(str(path))

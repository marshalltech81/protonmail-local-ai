"""#1316: Proton identity headers do not change today's evidence identity.

The Proton Mail Bridge writes ``X-Pm-Internal-Id`` (Proton's message
ID) into the messages it builds, and Gluon writes ``X-Pm-Gluon-Id``
(its own internal ID). A file can also carry them forged, duplicated
or not at all: Gluon keeps a failed APPEND's literal unchanged in
``Recovered Messages``, and Bridge keeps existing ``X-Pm-Internal-Id``
fields below one that matches (see the "Proton identity evidence"
section of ``docs/architecture.md`` for the upstream source review). These tests pin what the index does with such files today,
before any proposed use of the headers (#1313):

- The headers are ordinary bytes. Two files that differ in anything
  get two claimant IDs (#217), each with its own rows in every table
  keyed by claimant ID, and
  neither header ever merges, overwrites or deletes another claimant's
  rows, in either arrival order.
- Two byte versions of one logical message (a rebuild with a new MIME
  boundary) are two claimants with disjoint chunk IDs; reaping one
  leaves the other's chunks as they were and records the reaped
  claimant, so a citation is never moved to the other bytes.
- A recovery-style file with no Message-ID is dead-lettered with fixed
  text whatever identity headers it carries (the #1082 decision may
  change this; this test records today's behaviour only).
- No identity value reaches the log, ``last_error`` or any table.
- A source with no Proton headers, in a folder outside Proton's layout,
  is indexed like any other message.

Every message here is written by the test, with ``.example`` domains
and synthetic identity values; nothing is derived from real mail.
"""

import dataclasses
import hashlib
import json
import logging
from pathlib import Path

import pytest
from src import main, parser
from src.database import Database
from src.queue import REASON_INITIAL_SCAN
from src.reconciler import Reconciler, ReconcilerConfig
from src.threader import Threader

from tests.conftest import make_mock_embedder
from tests.test_main import _UNIT_VECTOR, _drain, _make_queue

# Synthetic identity values. They must never appear in a log line, a
# persisted error or a stored row.
_PM_ID = "SYNTHPMID1316aaaa=="
_PM_OTHER = "SYNTHPMID1316bbbb=="
_GLUON_ID = "SYNTHGLUON1316-0000-0000-0000-000000000001"
_GLUON_OTHER = "SYNTHGLUON1316-0000-0000-0000-000000000002"
_ID_MARKERS = ("SYNTHPMID1316", "SYNTHGLUON1316")


def _eml(
    message_id: str | None,
    body: str,
    *,
    identity: tuple[tuple[str, str], ...] = (),
    boundary: str = "b1316one",
    subject: str = "Identity evidence",
) -> bytes:
    """A small multipart message: a text body and a text attachment,
    with display names on the addresses so every claimant-keyed table
    gets rows. ``identity`` is a list of extra ``(name, value)`` header
    fields, written in order (so a name can repeat); ``boundary``
    changes only the MIME representation."""
    headers = [
        "From: Sender One <sender@mail.example>",
        "To: Recipient Two <recipient@mail.example>",
        f"Subject: {subject}",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
    ]
    if message_id is not None:
        headers.append(f"Message-ID: <{message_id}>")
    headers += [f"{name}: {value}" for name, value in identity]
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
        self.db = Database(tmp_path / "mail.db")
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

    def reap(self, path: Path) -> None:
        """Trash ``path`` (Maildir ``T`` flag) and reap it at once."""
        path.rename(path.with_name(path.name + "T"))
        reconciler = Reconciler(
            self.db,
            self.embedder,
            ReconcilerConfig(
                enabled=True, grace_days=0, sweep_interval_secs=60, max_batch_pct=1.0, force=False
            ),
            maildir_root=self.maildir,
        )
        reconciler.sweep()
        reconciler.reap()

    def claimant_tables(self) -> list[str]:
        """Every regular table with a ``claimant_id`` column, read from
        the schema so a new per-message table is covered without editing
        this test. ``reaped_messages`` is the reap record, checked on its
        own."""
        tables = [
            r[0]
            for r in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
            )
        ]
        return sorted(
            t
            for t in tables
            if t != "reaped_messages"
            and "claimant_id" in {c[1] for c in self.db._conn.execute(f'PRAGMA table_info("{t}")')}
        )

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
        of ``needles``."""
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
                    if isinstance(value, str) and any(n in value for n in needles):
                        found.add(table)
        return found


def _claimant(path: Path) -> str:
    msg = parser.parse_email(path)
    assert msg is not None
    return msg.claimant_id


class TestParserIgnoresIdentityHeaders:
    @pytest.mark.parametrize("shape", sorted(_SECOND_IDENTITY))
    def test_identity_headers_only_change_the_bytes(self, shape, caplog):
        """Parsed from the same source metadata, a message with identity
        headers and one without differ only in the fields taken from the
        bytes themselves (``size`` and ``content_hash``, so the claimant
        ID); every other compared ``Message`` field is equal (only
        ``first_indexed_at``, the clock at parse time, is not compared),
        and ``Message`` has no provider-identity field."""
        caplog.set_level(logging.DEBUG)
        plain_raw = _eml("evidence@mail.example", "Same body.")
        tagged_raw = _eml("evidence@mail.example", "Same body.", identity=_SECOND_IDENTITY[shape])
        source = parser.SourceMetadata(
            folder="INBOX", flags=frozenset("S"), size=0, mtime_ns=None, path="synthetic://1"
        )

        plain = parser.parse_email_bytes(plain_raw, source)
        tagged = parser.parse_email_bytes(tagged_raw, source)

        assert plain is not None and tagged is not None
        assert (tagged.size, tagged.content_hash) == (
            len(tagged_raw),
            hashlib.sha256(tagged_raw).hexdigest(),
        )
        assert (
            dataclasses.replace(tagged, size=plain.size, content_hash=plain.content_hash) == plain
        )
        if shape == "absent":
            assert tagged.claimant_id == plain.claimant_id
        else:
            assert tagged.claimant_id != plain.claimant_id
        assert all(marker not in repr(tagged) for marker in _ID_MARKERS)
        assert all(marker not in caplog.text for marker in _ID_MARKERS)


class TestClaimantsStayIndependent:
    @pytest.mark.parametrize("order", ["first-then-second", "second-then-first"])
    @pytest.mark.parametrize("same_message_id", [True, False], ids=["same-mid", "other-mid"])
    @pytest.mark.parametrize("shape", sorted(_SECOND_IDENTITY))
    def test_identity_headers_never_merge_overwrite_or_delete(
        self, tmp_path, monkeypatch, caplog, shape, same_message_id, order
    ):
        """Two different files, whatever identity headers they carry
        (equal, forged, duplicated, conflicting, absent) and whatever
        their Message-IDs, are two claimants, each with its own rows in
        every table keyed by claimant ID. Reaping the first removes all
        of its rows and leaves every row of the second exactly as it
        was."""
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

        idx.reap(first)

        assert not any(idx.rows_of(a).values())
        # Every row of the survivor, in every claimant-keyed table, is
        # exactly as it was.
        assert idx.rows_of(b) == rows_b
        assert idx.db.is_indexed(str(second))
        reaped = idx.db._conn.execute("SELECT claimant_id FROM reaped_messages").fetchall()
        assert [r["claimant_id"] for r in reaped] == [a]
        # No identity value is logged or stored anywhere (no table keeps
        # raw headers today; #1079's header store will revisit this).
        assert all(marker not in caplog.text for marker in _ID_MARKERS)
        assert idx.tables_holding(_ID_MARKERS) == set()


class TestByteVersionsOfOneLogicalMessage:
    def test_rebuilt_representation_is_a_separate_source(self, tmp_path, monkeypatch):
        """Bridge builds multipart messages with random boundaries, so one
        Proton message can reach the Maildir as different bytes. Two such
        versions (same Message-ID, same ``X-Pm-Internal-Id``, same text,
        new boundary) are two claimants with disjoint chunk IDs. Reaping
        the first removes its chunks and records its claimant as reaped;
        the second's chunk IDs are untouched, so a citation of the first
        is never moved to the second's bytes."""
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

        idx.reap(older)

        remaining = {
            r["chunk_id"] for r in idx.db._conn.execute("SELECT chunk_id FROM message_chunks")
        }
        assert remaining == set(chunks_b)
        assert not remaining & set(chunks_a)
        reaped = idx.db._conn.execute("SELECT claimant_id, message_id FROM reaped_messages")
        assert [tuple(r) for r in reaped] == [(a, "version@mail.example")]


class TestIdLessRecoveryStyleInput:
    @pytest.mark.parametrize(
        "identity",
        [
            (),
            ((_INTERNAL, _PM_ID),),
            ((_INTERNAL, _PM_ID), (_GLUON, _GLUON_ID), (_INTERNAL, _PM_OTHER)),
        ],
        ids=["no-identity", "internal-id", "duplicate-identity"],
    )
    def test_no_message_id_is_dead_lettered_whatever_identity_it_carries(
        self, tmp_path, monkeypatch, caplog, identity
    ):
        """Gluon keeps a failed APPEND's literal unchanged, so a
        ``Recovered Messages`` file need not have the Message-ID Bridge
        synthesizes for messages it builds. Today the parser returns
        ``None`` for it and the worker dead-letters it with fixed text;
        an identity header does not make it indexable. Recorded apart
        from the #1082 decision on content-hash identity."""
        caplog.set_level(logging.DEBUG)
        idx = _Index(tmp_path, monkeypatch)
        path = idx.write(
            "1700000000.M1.host", _eml(None, "Recovered deltaword.", identity=identity)
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
        assert all(marker not in caplog.text for marker in _ID_MARKERS)
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

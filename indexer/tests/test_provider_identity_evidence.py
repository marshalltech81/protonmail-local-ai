"""#1316: Proton identity headers do not change today's evidence identity.

The Proton Mail Bridge writes ``X-Pm-Internal-Id`` (Proton's message
ID) into the messages it builds, and Gluon writes ``X-Pm-Gluon-Id``
(its own internal ID). Either header can also arrive forged, duplicated
or missing: a sender can write it, and Gluon keeps a failed APPEND's
literal unchanged in ``Recovered Messages`` (see the "Proton identity
evidence" section of ``docs/architecture.md`` for the upstream source
review). These tests pin what the index does with such files today,
before any proposed use of the headers (#1313):

- The headers are ordinary bytes. Two files that differ in anything
  get two claimant IDs (#217), each with its own per-message rows, and
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
    """A small multipart message. ``identity`` is a list of extra
    ``(name, value)`` header fields, written in order (so a name can
    repeat); ``boundary`` changes only the MIME representation."""
    headers = [
        "From: sender@mail.example",
        "To: recipient@mail.example",
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

    def rows_of(self, claimant: str) -> dict[str, list[tuple]]:
        """Every per-message row keyed by ``claimant``."""
        queries = {
            "messages": "SELECT claimant_id, message_id, filepath, content_hash FROM messages",
            "map": "SELECT claimant_id, message_id, filepath FROM message_thread_map",
            "participants": "SELECT claimant_id, role, address FROM message_participants",
            "chunks": "SELECT claimant_id, chunk_id, text FROM message_chunks",
        }
        return {
            table: sorted(
                tuple(r) for r in self.db._conn.execute(sql) if r["claimant_id"] == claimant
            )
            for table, sql in queries.items()
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
        their Message-IDs, are two claimants with their own message,
        thread-map, participant and chunk rows. Reaping the first leaves
        the second's rows exactly as they were."""
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
        for table in ("messages", "map", "participants", "chunks"):
            assert rows_a[table] and rows_b[table], table
        assert rows_a["messages"][0][2] == str(first)
        assert rows_b["messages"][0][2] == str(second)
        text_a = " ".join(r[2] for r in rows_a["chunks"])
        text_b = " ".join(r[2] for r in rows_b["chunks"])
        assert "alphaword" in text_a and "betaword" not in text_a
        assert "betaword" in text_b and "alphaword" not in text_b

        idx.reap(first)

        assert not any(idx.rows_of(a).values())
        # The survivor's message, thread-map, participant and chunk rows
        # are exactly as they were.
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
        chunks_a = {r[1]: r[2] for r in idx.rows_of(a)["chunks"]}
        chunks_b = {r[1]: r[2] for r in idx.rows_of(b)["chunks"]}
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

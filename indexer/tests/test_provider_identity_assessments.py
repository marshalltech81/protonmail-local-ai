"""#1598 (owner decision 2026-10-23, option A): provider-identity
candidates, versioned assessments and dated label observations are
stored, and nothing is ever accepted.

The tests pin:

- schema v21: the tables, the policy selector, the guards, and a
  migration from v20 that equals a fresh install and moves no claimant,
  chunk, thread ID or vector;
- ``proton-bridge-v1``: a candidate comes from stored header evidence
  only (one ``X-Pm-Internal-Id`` field, complete capture), ``Recovered
  Messages`` is rejected, ``X-Pm-Gluon-Id`` is never an identity, and no
  shape yields ``accepted`` (so the effective accepted relation is
  empty);
- assessments are per occurrence and per account, invalidated in the
  transaction that changes their evidence, refused when stale, and
  removed with their occurrence;
- label observations carry no foreign key to any identity table, outlive
  their sources, are retired only by a complete generation, and keep
  "no reconciled scan" apart from "no label";
- no provider ID or label name reaches a log line.

Synthetic bytes only.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from src import main, parser, provider_identity
from src.database import EMBEDDING_DIM, SCHEMA_VERSION, Database
from src.provider_identity import Assessment, Evidence, assess

from tests.conftest import TEST_ACCOUNT, make_thread
from tests.test_database import _schema_shape, _v20_from_fresh

_PM_ID = "SYNTHPMID1598aaaa=="
_PM_OTHER = "SYNTHPMID1598bbbb=="
_GLUON_ID = "SYNTHGLUON1598-0000-0000-0000-000000000001"
_LABEL = "SYNTHLABEL1598 private"
_MARKERS = ("SYNTHPMID1598", "SYNTHGLUON1598", "SYNTHLABEL1598")
_INTERNAL = "X-Pm-Internal-Id"
_GLUON = "X-Pm-Gluon-Id"


@pytest.fixture
def db(db):
    """Autocommit, so a direct write is visible to the methods that open
    their own transaction."""
    db._conn.isolation_level = None
    return db


@pytest.fixture(autouse=True)
def _reset_identity_tick(monkeypatch):
    monkeypatch.setattr(main, "_identity_next_due", 0.0)
    monkeypatch.setattr(main, "_identity_cursor", 0)


def _eml(message_id: str, fields: tuple[tuple[str, str], ...] = (), body: str = "body") -> bytes:
    headers = [
        "From: Sender One <sender@mail.example>",
        "To: Recipient Two <recipient@mail.example>",
        "Subject: Identity storage",
        "Date: Mon, 01 Jan 2024 12:00:00 +0000",
        f"Message-ID: <{message_id}>",
        *(f"{name}: {value}" for name, value in fields),
    ]
    return ("\r\n".join(headers) + f"\r\n\r\n{body}\r\n").encode()


def _index(
    db: Database,
    name: str,
    fields: tuple[tuple[str, str], ...] = (),
    *,
    folder: str = "INBOX",
    raw: bytes | None = None,
) -> parser.Message:
    """Index one synthetic message in ``folder`` of the database's account."""
    path = f"/{folder}/cur/{name}:2,S"
    data = raw if raw is not None else _eml(f"{name}@mail.example", fields)
    msg = parser.parse_email_bytes(
        data,
        parser.SourceMetadata(
            folder=folder, flags=frozenset("S"), size=len(data), mtime_ns=None, path=path
        ),
    )
    assert msg is not None
    db.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
    return msg


def _occurrence(db: Database, msg: parser.Message) -> int:
    return db._conn.execute(
        "SELECT id FROM message_occurrences WHERE claimant_id = ? AND state = 'active'",
        (msg.claimant_id,),
    ).fetchone()[0]


def _assessment(db: Database, occurrence_id: int):
    return db._conn.execute(
        "SELECT outcome, reason, candidate, state, account_id, policy_version, "
        "identity_header_count, headers_complete "
        "FROM provider_identity_assessments WHERE occurrence_id = ?",
        (occurrence_id,),
    ).fetchone()


def _tuples(cursor) -> list[tuple]:
    return [tuple(r) for r in cursor.fetchall()]


def _run(db: Database, limit: int = 100, after: int = 0):
    return provider_identity.reassess_batch(db, limit=limit, after=after)


def _leaks(text: str) -> bool:
    folded = text.casefold()
    return any(m.casefold() in folded for m in _MARKERS)


# ---------------------------------------------------------------------------
# the policy: a pure function of stored evidence
# ---------------------------------------------------------------------------


class TestPolicy:
    @pytest.mark.parametrize(
        ("evidence", "expected"),
        [
            # One complete, well-formed field: recorded as a candidate,
            # and still not accepted (no acceptance rule exists).
            (
                Evidence("INBOX", True, 1, _PM_ID),
                Assessment("unresolved", "no_acceptance_rule", _PM_ID),
            ),
            (
                Evidence("Folders/Work", True, 1, _PM_ID),
                Assessment("unresolved", "no_acceptance_rule", _PM_ID),
            ),
            # Recovered Messages is its own untrusted domain: rejected,
            # and the value is not kept as a candidate.
            (
                Evidence("Recovered Messages", True, 1, _PM_ID),
                Assessment("rejected", "recovered_folder", None),
            ),
            (
                Evidence("Recovered Messages", False, 0, None),
                Assessment("rejected", "recovered_folder", None),
            ),
            (
                Evidence("INBOX", None, 1, _PM_ID),
                Assessment("unresolved", "headers_unknown", None),
            ),
            (
                Evidence("INBOX", False, 1, _PM_ID),
                Assessment("unresolved", "headers_incomplete", None),
            ),
            (
                Evidence("INBOX", True, 0, None),
                Assessment("unresolved", "no_identity_header", None),
            ),
            (
                Evidence("INBOX", True, 2, _PM_ID),
                Assessment("unresolved", "duplicate_identity_header", None),
            ),
            (
                Evidence("INBOX", True, 1, ""),
                Assessment("unresolved", "malformed_identity", None),
            ),
            (
                Evidence("INBOX", True, 1, f" {_PM_ID}"),
                Assessment("unresolved", "malformed_identity", None),
            ),
            (
                Evidence("INBOX", True, 1, "has space"),
                Assessment("unresolved", "malformed_identity", None),
            ),
            (
                Evidence("INBOX", True, 1, "x" * 257),
                Assessment("unresolved", "malformed_identity", None),
            ),
            (
                Evidence("INBOX", True, 1, "café"),
                Assessment("unresolved", "malformed_identity", None),
            ),
        ],
    )
    def test_each_shape(self, evidence, expected):
        assert assess(evidence) == expected

    def test_no_shape_is_accepted(self):
        for folder in ("INBOX", "Recovered Messages", "Folders/x", "Spam", "Trash"):
            for complete in (True, False, None):
                for count in (0, 1, 2):
                    for value in (None, _PM_ID, "", "a b"):
                        outcome = assess(Evidence(folder, complete, count, value)).outcome
                        assert outcome in {"unresolved", "rejected"}

    def test_the_gluon_id_is_never_a_candidate(self):
        # Gluon IDs are not read at all: the evidence carries only the
        # ``X-Pm-Internal-Id`` fields.
        assert not hasattr(Evidence("INBOX", True, 0, None), "gluon_id")


# ---------------------------------------------------------------------------
# schema v21
# ---------------------------------------------------------------------------


def _new_objects(conn: sqlite3.Connection) -> dict[str, str]:
    """The DDL of v21's tables, view and triggers, whitespace-normalized."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name IN ("
        "'provider_identity_assessments', 'identity_policy_state', "
        "'label_scan_generations', 'label_membership_observations', "
        "'effective_accepted_occurrences', 'idx_message_occurrences_id_account') "
        "OR name LIKE '%invalidate_assessments%' OR name LIKE '%label_%guard%' "
        "OR name LIKE '%no_acceptance%'"
    ).fetchall()
    return {name: " ".join(sql.split()) for name, sql in rows}


class TestSchema:
    def test_fresh_install_has_the_current_version_and_selector(self, db):
        assert SCHEMA_VERSION == 22
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        )
        assert _tuples(db._conn.execute("SELECT policy_version FROM identity_policy_state")) == [
            (provider_identity.POLICY_VERSION,)
        ]

    def test_v20_migrates_to_the_fresh_shape_without_moving_ids_or_vectors(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "v20.db", account=TEST_ACCOUNT)
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        before = (
            _tuples(db._conn.execute("SELECT claimant_id, thread_id FROM messages")),
            _tuples(db._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY 1")),
            db._conn.execute("SELECT COUNT(*) FROM threads_vec").fetchone()[0],
            db._conn.execute("SELECT COUNT(*) FROM message_chunks_vec").fetchone()[0],
        )
        _v20_from_fresh(db)
        db.close()
        migrated = Database(tmp_path / "v20.db", account=TEST_ACCOUNT)
        fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
        try:
            assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
            assert _new_objects(migrated._conn) == _new_objects(fresh._conn)
            assert len(_new_objects(fresh._conn)) >= 12
            after = (
                _tuples(migrated._conn.execute("SELECT claimant_id, thread_id FROM messages")),
                _tuples(migrated._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY 1")),
                migrated._conn.execute("SELECT COUNT(*) FROM threads_vec").fetchone()[0],
                migrated._conn.execute("SELECT COUNT(*) FROM message_chunks_vec").fetchone()[0],
            )
            assert after == before
            assert after[0][0][0] == msg.claimant_id
            # No reparse is queued; no assessment exists until a batch runs.
            assert migrated._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
            assert (
                migrated._conn.execute(
                    "SELECT COUNT(*) FROM provider_identity_assessments"
                ).fetchone()[0]
                == 0
            )
            assert _tuples(
                migrated._conn.execute("SELECT policy_version FROM identity_policy_state")
            ) == [(provider_identity.POLICY_VERSION,)]
        finally:
            migrated.close()
            fresh.close()
        assert f"applied migrations: {list(range(21, SCHEMA_VERSION + 1))}" in caplog.text

    def test_an_interrupted_migration_leaves_the_version(self, tmp_path):
        from pathlib import Path

        from src import migrations
        from src.migrations import runner

        db = Database(tmp_path / "v20.db", account=TEST_ACCOUNT)
        _v20_from_fresh(db)
        db.close()
        shipped = Path(migrations.__file__).parent
        broken = tmp_path / "migrations"
        broken.mkdir()
        for path in shipped.glob("00[0-9][0-9]_*.sql"):
            sql = path.read_text(encoding="utf-8")
            # Fail the last statement of v21, after its tables exist.
            broken.joinpath(path.name).write_text(
                sql + "\nINSERT INTO no_such_table VALUES (1);\n"
                if path.name.startswith("0021")
                else sql,
                encoding="utf-8",
            )
        conn = sqlite3.connect(tmp_path / "v20.db")
        conn.row_factory = sqlite3.Row
        try:
            with pytest.raises(sqlite3.OperationalError):
                runner.apply_pending(
                    conn, current_version=20, target_version=21, migration_dir=broken
                )
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 20
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                    "('identity_policy_state', 'provider_identity_assessments')"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

    def test_an_assessment_names_a_registered_account_matching_its_occurrence(self, db):
        msg = _index(db, "one")
        occ = _occurrence(db, msg)
        base = (
            "INSERT INTO provider_identity_assessments (occurrence_id, account_id, "
            "policy_version, outcome, reason, identity_header_count, state, assessed_at) "
            "VALUES (?, ?, 'p', 'unresolved', 'no_identity_header', 0, 'current', 'x')"
        )
        with pytest.raises(sqlite3.IntegrityError):  # an unregistered account
            db._conn.execute(base, (occ, 99))
        db._conn.execute(
            "INSERT INTO accounts (id, scope_uuid, provider, created_at) VALUES "
            "(2, '00000000-0000-4000-8000-000000000002', 'proton-bridge', 'x')"
        )
        with pytest.raises(
            sqlite3.IntegrityError
        ):  # a registered account that is not the occurrence's
            db._conn.execute(base, (occ, 2))
        db._conn.execute(base, (occ, db.account_id))

    def test_the_provider_id_check_is_shared_by_both_tables(self, db):
        msg = _index(db, "one")
        occ = _occurrence(db, msg)
        db._conn.execute(
            "INSERT INTO label_scan_generations (account_id, generation, status, started_at) "
            "VALUES (?, 1, 'in_progress', 'x')",
            (db.account_id,),
        )
        for bad in ("", "has space", "x" * 257, "café", "tab\there"):
            with pytest.raises(sqlite3.IntegrityError):
                db._conn.execute(
                    "INSERT INTO provider_identity_assessments (occurrence_id, account_id, "
                    "policy_version, outcome, reason, candidate, identity_header_count, state, "
                    "assessed_at) VALUES (?, ?, 'p', 'unresolved', 'no_acceptance_rule', ?, 1, "
                    "'current', 'x')",
                    (occ, db.account_id, bad),
                )
            with pytest.raises(sqlite3.IntegrityError):
                db._conn.execute(
                    "INSERT INTO label_membership_observations (account_id, provider_id, label, "
                    "first_generation, last_generation, observed_at) VALUES (?, ?, 'l', 1, 1, 'x')",
                    (db.account_id, bad),
                )

    def test_no_table_names_an_identity_table_from_the_observations(self, db):
        refs = db._conn.execute("PRAGMA foreign_key_list(label_membership_observations)").fetchall()
        assert {r["table"] for r in refs} == {"label_scan_generations"}
        tables = {
            r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # No logical-identity table (decision 52): logical messages are a view.
        assert not [t for t in tables if "logical" in t]

    def test_acceptance_is_refused_by_the_database(self, db):
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        occ = _occurrence(db, msg)
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO provider_identity_assessments (occurrence_id, account_id, "
                "policy_version, outcome, reason, candidate, identity_header_count, state, "
                "assessed_at) VALUES (?, ?, ?, 'accepted', 'no_acceptance_rule', ?, 1, "
                "'current', 'x')",
                (occ, db.account_id, provider_identity.POLICY_VERSION, _PM_ID),
            )


# ---------------------------------------------------------------------------
# reassessment batches
# ---------------------------------------------------------------------------


class TestReassessment:
    def test_each_rejected_shape_is_stored_unresolved_or_rejected_never_accepted(self, db):
        shapes = {
            "ok": (((_INTERNAL, _PM_ID),), "INBOX", "unresolved", "no_acceptance_rule"),
            "none": ((), "INBOX", "unresolved", "no_identity_header"),
            "gluon": (((_GLUON, _GLUON_ID),), "INBOX", "unresolved", "no_identity_header"),
            "dup": (
                ((_INTERNAL, _PM_ID), (_INTERNAL, _PM_OTHER)),
                "INBOX",
                "unresolved",
                "duplicate_identity_header",
            ),
            "empty": (((_INTERNAL, ""),), "INBOX", "unresolved", "malformed_identity"),
            "recovered": (
                ((_INTERNAL, _PM_ID), (_GLUON, _GLUON_ID)),
                "Recovered Messages",
                "rejected",
                "recovered_folder",
            ),
        }
        occurrences = {
            name: _occurrence(db, _index(db, name, fields, folder=folder))
            for name, (fields, folder, _o, _r) in shapes.items()
        }
        result = _run(db)
        assert result.assessed == len(shapes)
        for name, (_f, _folder, outcome, reason) in shapes.items():
            row = _assessment(db, occurrences[name])
            assert (row["outcome"], row["reason"]) == (outcome, reason), name
            assert row["state"] == "current"
            assert row["policy_version"] == provider_identity.POLICY_VERSION
            assert row["account_id"] == db.account_id
        # Only the clean single field is kept as a candidate; a Gluon ID,
        # a duplicate, a malformed value and recovery mail never are.
        candidates = {name: _assessment(db, occ)["candidate"] for name, occ in occurrences.items()}
        assert candidates == {
            "ok": _PM_ID,
            "none": None,
            "gluon": None,
            "dup": None,
            "empty": None,
            "recovered": None,
        }
        assert (
            db._conn.execute("SELECT COUNT(*) FROM effective_accepted_occurrences").fetchone()[0]
            == 0
        )
        assert not db._conn.execute(
            "SELECT 1 FROM provider_identity_assessments WHERE outcome = 'accepted'"
        ).fetchone()

    def test_identity_headers_past_a_stopped_capture_read_unresolved(self, db, monkeypatch):
        monkeypatch.setattr(parser, "MAX_STORED_HEADER_FIELDS", 3)
        msg = _index(db, "cut", ((_INTERNAL, _PM_ID),))
        assert msg.headers_complete is False
        _run(db)
        row = _assessment(db, _occurrence(db, msg))
        assert (row["outcome"], row["reason"], row["candidate"]) == (
            "unresolved",
            "headers_incomplete",
            None,
        )
        assert row["headers_complete"] == 0

    def test_a_message_not_assessed_yet_has_no_row_and_is_not_accepted(self, db):
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        assert _assessment(db, _occurrence(db, msg)) is None
        assert (
            db._conn.execute("SELECT COUNT(*) FROM effective_accepted_occurrences").fetchone()[0]
            == 0
        )

    def test_equal_bytes_in_two_folders_are_assessed_per_occurrence(self, db):
        data = _eml("same@mail.example", ((_INTERNAL, _PM_ID),))
        inbox = _index(db, "a", raw=data, folder="INBOX")
        recovered = _index(db, "b", raw=data, folder="Recovered Messages")
        assert inbox.claimant_id == recovered.claimant_id
        _run(db)
        one = _assessment(db, _occurrence_in(db, inbox, "INBOX"))
        two = _assessment(db, _occurrence_in(db, recovered, "Recovered Messages"))
        # One claimant, two occurrences, two assessments: equal bytes pass
        # on neither acceptance nor rejection.
        assert (one["outcome"], one["candidate"]) == ("unresolved", _PM_ID)
        assert (two["outcome"], two["candidate"]) == ("rejected", None)
        assert (
            db._conn.execute("SELECT COUNT(*) FROM effective_accepted_occurrences").fetchone()[0]
            == 0
        )

    def test_batches_are_bounded_and_resumable(self, db):
        for i in range(5):
            _index(db, f"m{i}", ((_INTERNAL, f"SYNTH{i}"),))
        first = _run(db, limit=2)
        assert (first.assessed, first.exhausted) == (2, False)
        second = _run(db, limit=2, after=first.last_id)
        assert (second.assessed, second.exhausted) == (2, False)
        third = _run(db, limit=2, after=second.last_id)
        assert (third.assessed, third.exhausted) == (1, True)
        assert _run(db, limit=2).assessed == 0
        assert (
            db._conn.execute("SELECT COUNT(*) FROM provider_identity_assessments").fetchone()[0]
            == 5
        )

    def test_a_pass_reads_each_row_once(self, db):
        """Work gate (#1670): the cost of one batch must not grow with
        the rows already assessed or still pending. Measured in SQLite VM
        steps (a plain count, not a profiler): the dearest batch of a
        corpus four times larger costs the same as the dearest of the small
        one (1.0x); the known-bad shape (a scan from the first occurrence
        and a whole-account recount per batch) measured 2.1x at these
        sizes (24,579 against 51,924 steps), and worsens with size."""

        def dearest_batch(count: int) -> int:
            for i in range(count):
                _index(db, f"w{count}-{i}", ((_INTERNAL, f"SYNTH{count}x{i}"),))
            steps: list[int] = []
            after = 0
            while True:
                n = 0

                def tick() -> int:
                    nonlocal n
                    n += 1
                    return 0

                db._conn.set_progress_handler(tick, 1)
                result = _run(db, limit=50, after=after)
                db._conn.set_progress_handler(None, 1)
                steps.append(n)
                after = result.last_id
                if result.exhausted:
                    assert (
                        db._conn.execute(
                            "SELECT COUNT(*) FROM provider_identity_assessments"
                        ).fetchone()[0]
                        == db._conn.execute("SELECT COUNT(*) FROM message_occurrences").fetchone()[
                            0
                        ]
                    )
                    # The first batch also seeks past rows assessed by an
                    # earlier pass, once per pass.
                    return max(steps[1:])

        small = dearest_batch(300)
        large = dearest_batch(1200)
        assert large < 1.3 * small, (small, large)

    def test_a_retired_occurrence_is_not_assessed(self, db):
        msg = _index(db, "gone", ((_INTERNAL, _PM_ID),))
        db._conn.execute("UPDATE message_occurrences SET state = 'retired', retired_at = 'x'")
        assert _run(db).assessed == 0
        assert _assessment(db, _occurrence_any(db, msg)) is None

    def test_another_provider_is_not_assessed(self, tmp_path):
        from dataclasses import replace

        db = Database(tmp_path / "o.db", account=TEST_ACCOUNT)
        try:
            _index(db, "one", ((_INTERNAL, _PM_ID),))
            db.account = replace(db.account, provider="generic-imap")
            assert _run(db).assessed == 0
        finally:
            db.close()

    def test_counts_only_in_the_result_and_no_marker_in_logs(self, db, caplog):
        caplog.set_level(logging.DEBUG)
        _index(db, "ok", ((_INTERNAL, _PM_ID),))
        _index(
            db, "rec", ((_INTERNAL, _PM_OTHER), (_GLUON, _GLUON_ID)), folder="Recovered Messages"
        )
        result = _run(db)
        assert result.counts == {"unresolved": 1, "rejected": 1}
        assert not _leaks(caplog.text)
        assert not _leaks(repr(result))


def _occurrence_in(db: Database, msg: parser.Message, folder: str) -> int:
    return db._conn.execute(
        "SELECT id FROM message_occurrences WHERE claimant_id = ? AND folder = ? "
        "AND state = 'active'",
        (msg.claimant_id, folder),
    ).fetchone()[0]


def _occurrence_any(db: Database, msg: parser.Message) -> int:
    return db._conn.execute(
        "SELECT id FROM message_occurrences WHERE claimant_id = ?", (msg.claimant_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# invalidation, staleness, cascade, policy selector
# ---------------------------------------------------------------------------


class TestInvalidation:
    def _assessed(self, db, fields=((_INTERNAL, _PM_ID),), folder="INBOX"):
        msg = _index(db, "one", fields, folder=folder)
        occ = _occurrence(db, msg)
        _run(db)
        assert _assessment(db, occ)["state"] == "current"
        return msg, occ

    def test_a_reparse_that_changes_the_identity_field_invalidates_in_the_same_write(self, db):
        msg, occ = self._assessed(db)
        # Rewrite the claimant's header rows the way a reparse does.
        db._conn.execute("DELETE FROM message_headers WHERE claimant_id = ?", (msg.claimant_id,))
        assert _assessment(db, occ)["state"] == "invalidated"

    def test_inserting_updating_or_deleting_an_identity_row_invalidates(self, db):
        msg, occ = self._assessed(db)
        for statement in (
            "INSERT INTO message_headers (claimant_id, ordinal, name, name_normalized, raw_value, "
            "decoded_value) VALUES (?, 99, 'X-Pm-Internal-Id', 'x-pm-internal-id', 'q', 'q')",
            "UPDATE message_headers SET raw_value = 'changed' "
            "WHERE claimant_id = ? AND name_normalized = 'x-pm-internal-id'",
            "DELETE FROM message_headers WHERE claimant_id = ? "
            "AND name_normalized = 'x-pm-internal-id'",
        ):
            db._conn.execute("UPDATE provider_identity_assessments SET state = 'current'")
            db._conn.execute(statement, (msg.claimant_id,))
            assert _assessment(db, occ)["state"] == "invalidated", statement

    def test_other_header_rows_do_not_invalidate(self, db):
        msg, occ = self._assessed(db)
        db._conn.execute(
            "UPDATE message_headers SET raw_value = 'x' WHERE claimant_id = ? "
            "AND name_normalized = 'subject'",
            (msg.claimant_id,),
        )
        assert _assessment(db, occ)["state"] == "current"

    def test_capture_completeness_folder_and_state_changes_invalidate(self, db):
        msg, occ = self._assessed(db)
        for statement in (
            "UPDATE messages SET headers_complete = 0 WHERE claimant_id = ?",
            "UPDATE message_occurrences SET folder = 'Recovered Messages' WHERE claimant_id = ?",
            "UPDATE message_occurrences SET state = 'retired', retired_at = 'x' "
            "WHERE claimant_id = ?",
        ):
            db._conn.execute("UPDATE provider_identity_assessments SET state = 'current'")
            db._conn.execute(statement, (msg.claimant_id,))
            assert _assessment(db, occ)["state"] == "invalidated", statement

    def test_a_flag_rename_does_not_invalidate(self, db):
        msg, occ = self._assessed(db)
        db._conn.execute("UPDATE message_occurrences SET seen = 0 WHERE id = ?", (occ,))
        assert _assessment(db, occ)["state"] == "current"

    def test_an_invalidated_assessment_is_reassessed_and_is_current_again(self, db):
        msg, occ = self._assessed(db)
        db._conn.execute("DELETE FROM message_headers WHERE claimant_id = ?", (msg.claimant_id,))
        assert _run(db).assessed == 1
        row = _assessment(db, occ)
        assert (row["state"], row["reason"], row["candidate"]) == (
            "current",
            "no_identity_header",
            None,
        )

    def test_reaping_a_source_removes_its_assessments(self, db):
        msg, occ = self._assessed(db)
        with db.transaction():
            db._conn.execute(
                "DELETE FROM message_thread_map WHERE claimant_id = ?", (msg.claimant_id,)
            )
        assert _assessment(db, occ) is None

    def test_a_stale_worker_is_refused(self, db):
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        occ = _occurrence(db, msg)
        (pending,) = db.pending_identity_evidence(limit=10)
        # The evidence changes between the worker's read and its write.
        db._conn.execute("DELETE FROM message_headers WHERE claimant_id = ?", (msg.claimant_id,))
        assessment = assess(pending.evidence)
        assert assessment.reason == "no_acceptance_rule"
        assert db.record_identity_assessment(pending, assessment) is False
        assert _assessment(db, occ) is None

    def test_a_stale_write_is_counted_and_left_pending(self, db, monkeypatch):
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        real = db.record_identity_assessment

        def changed_first(pending, assessment):
            db._conn.execute(
                "DELETE FROM message_headers WHERE claimant_id = ?", (msg.claimant_id,)
            )
            return real(pending, assessment)

        monkeypatch.setattr(db, "record_identity_assessment", changed_first)
        result = _run(db)
        assert (result.assessed, result.stale, result.exhausted) == (0, 1, True)
        monkeypatch.undo()
        # The next pass starts at 0 and picks the row up.
        assert _run(db).assessed == 1

    def test_a_worker_of_an_earlier_policy_is_refused(self, db):
        _index(db, "one", ((_INTERNAL, _PM_ID),))
        (pending,) = db.pending_identity_evidence(limit=10)
        db._conn.execute("UPDATE identity_policy_state SET policy_version = 'proton-bridge-v2'")
        assert db.record_identity_assessment(pending, assess(pending.evidence)) is False

    def test_acceptance_is_refused_by_the_write_path(self, db):
        _index(db, "one", ((_INTERNAL, _PM_ID),))
        (pending,) = db.pending_identity_evidence(limit=10)
        with pytest.raises(ValueError, match="accepted"):
            db.record_identity_assessment(
                pending, Assessment("accepted", "no_acceptance_rule", _PM_ID)
            )

    def test_a_later_policy_version_revokes_acceptance(self, db):
        # The database refuses 'accepted' (above); lift that guard only to
        # prove the effective relation follows the selector.
        db._conn.execute("DROP TRIGGER provider_identity_no_acceptance_insert")
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        occ = _occurrence(db, msg)
        db._conn.execute(
            "INSERT INTO provider_identity_assessments (occurrence_id, account_id, policy_version, "
            "outcome, reason, candidate, identity_header_count, state, assessed_at) VALUES "
            "(?, ?, ?, 'accepted', 'no_acceptance_rule', ?, 1, 'current', 'x')",
            (occ, db.account_id, provider_identity.POLICY_VERSION, _PM_ID),
        )
        view = "SELECT occurrence_id, account_id, candidate FROM effective_accepted_occurrences"
        assert _tuples(db._conn.execute(view)) == [(occ, db.account_id, _PM_ID)]
        db._conn.execute("UPDATE provider_identity_assessments SET state = 'invalidated'")
        assert _tuples(db._conn.execute(view)) == []
        db._conn.execute("UPDATE provider_identity_assessments SET state = 'current'")
        db._conn.execute("UPDATE identity_policy_state SET policy_version = 'proton-bridge-v2'")
        assert _tuples(db._conn.execute(view)) == []

    def test_opening_with_a_new_policy_version_moves_the_selector_once(
        self, tmp_path, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "p.db", account=TEST_ACCOUNT)
        _index(db, "one", ((_INTERNAL, _PM_ID),))
        _run(db)
        db.close()
        monkeypatch.setattr(provider_identity, "POLICY_VERSION", "proton-bridge-v2")
        db = Database(tmp_path / "p.db", account=TEST_ACCOUNT)
        try:
            assert _tuples(
                db._conn.execute("SELECT policy_version FROM identity_policy_state")
            ) == [("proton-bridge-v2",)]
            # The earlier version's rows stay as history but are pending again.
            assert db.pending_identity_evidence(limit=10)
            assert _run(db).assessed == 1
        finally:
            db.close()
        assert "identity policy selector moved" in caplog.text
        assert not _leaks(caplog.text)


# ---------------------------------------------------------------------------
# two accounts
# ---------------------------------------------------------------------------


def test_one_provider_id_under_two_accounts_is_two_candidates(db):
    msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
    occ1 = _occurrence(db, msg)
    db._conn.execute(
        "INSERT INTO accounts (id, scope_uuid, provider, created_at) VALUES "
        "(2, '00000000-0000-4000-8000-000000000002', 'proton-bridge', 'x')"
    )
    db._conn.execute(
        "INSERT INTO message_occurrences (account_id, folder, uniq, claimant_id, relative_path, "
        "seen, flagged, replied, trashed, state, provenance) VALUES "
        "(2, 'INBOX', 'u2', ?, 'INBOX/cur/u2', 0, 0, 0, 0, 'active', 'parse')",
        (msg.claimant_id,),
    )
    occ2 = db._conn.execute("SELECT MAX(id) FROM message_occurrences").fetchone()[0]
    for occ, account in ((occ1, db.account_id), (occ2, 2)):
        db._conn.execute(
            "INSERT INTO provider_identity_assessments (occurrence_id, account_id, policy_version, "
            "outcome, reason, candidate, identity_header_count, state, assessed_at) VALUES "
            "(?, ?, ?, 'unresolved', 'no_acceptance_rule', ?, 1, 'current', 'x')",
            (occ, account, provider_identity.POLICY_VERSION, _PM_ID),
        )
    pairs = _tuples(
        db._conn.execute("SELECT DISTINCT account_id, candidate FROM provider_identity_assessments")
    )
    assert len(pairs) == 2


# ---------------------------------------------------------------------------
# label observations
# ---------------------------------------------------------------------------


class TestLabelObservations:
    def _members(self, db):
        return sorted(
            (r["provider_id"], r["label"], r["retired_generation"])
            for r in db._conn.execute("SELECT * FROM label_membership_observations")
        )

    def test_no_reconciled_scan_is_unavailable_not_no_label(self, db):
        assert db.label_scan_status() == "unavailable"
        gen = db.begin_label_generation()
        assert db.label_scan_status() == "unavailable"  # open generation
        db.finish_label_generation(gen, "partial")
        assert db.label_scan_status() == "unavailable"
        gen = db.begin_label_generation()
        db.finish_label_generation(gen, "complete")
        # A complete scan with no memberships: available, and empty.
        assert db.label_scan_status() == "available"
        assert self._members(db) == []

    def test_a_complete_generation_retires_what_it_did_not_see(self, db):
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [("A1", _LABEL), ("A2", _LABEL)])
        db.finish_label_generation(g1, "complete")
        g2 = db.begin_label_generation()
        db.record_label_memberships(g2, [("A1", _LABEL)])
        db.finish_label_generation(g2, "complete")
        assert self._members(db) == [("A1", _LABEL, None), ("A2", _LABEL, g2)]
        # A membership that returns is live again.
        g3 = db.begin_label_generation()
        db.record_label_memberships(g3, [("A1", _LABEL), ("A2", _LABEL)])
        db.finish_label_generation(g3, "complete")
        assert self._members(db) == [("A1", _LABEL, None), ("A2", _LABEL, None)]

    @pytest.mark.parametrize("status", ["partial", "failed", "interrupted"])
    def test_an_unfinished_generation_adds_and_retires_nothing(self, db, status):
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [("A1", _LABEL), ("A2", _LABEL)])
        db.finish_label_generation(g1, "complete")
        g2 = db.begin_label_generation()
        db.record_label_memberships(g2, [("A3", _LABEL)])
        if status == "interrupted":
            assert db.interrupt_open_label_generations() == 1
        else:
            db.finish_label_generation(g2, status)
        assert self._members(db) == [
            ("A1", _LABEL, None),
            ("A2", _LABEL, None),
            ("A3", _LABEL, None),
        ]

    def test_the_database_refuses_to_retire_on_a_generation_that_is_not_complete(self, db):
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [("A1", _LABEL)])
        db.finish_label_generation(g1, "complete")
        g2 = db.begin_label_generation()
        db.finish_label_generation(g2, "partial")
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "UPDATE label_membership_observations SET retired_generation = ?, retired_at = 'x'",
                (g2,),
            )

    def test_only_an_open_generation_takes_observations(self, db):
        g1 = db.begin_label_generation()
        db.finish_label_generation(g1, "partial")
        with pytest.raises(ValueError):
            db.record_label_memberships(g1, [("A1", _LABEL)])
        with pytest.raises(ValueError):
            db.finish_label_generation(g1, "complete")
        with pytest.raises(ValueError):
            db.finish_label_generation(db.begin_label_generation(), "in_progress")

    def test_a_failed_batch_write_leaves_no_partial_rows(self, db):
        g1 = db.begin_label_generation()
        with pytest.raises(sqlite3.IntegrityError):
            db.record_label_memberships(g1, [("A1", _LABEL), ("bad id", _LABEL)])
        assert self._members(db) == []

    def test_observations_outlive_their_sources_and_name_no_identity_row(self, db):
        msg = _index(db, "one", ((_INTERNAL, _PM_ID),))
        _run(db)
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [(_PM_ID, _LABEL)])
        db.finish_label_generation(g1, "complete")
        with db.transaction():
            db._conn.execute(
                "DELETE FROM message_thread_map WHERE claimant_id = ?", (msg.claimant_id,)
            )
        assert self._members(db) == [(_PM_ID, _LABEL, None)]
        assert (
            db._conn.execute("SELECT COUNT(*) FROM provider_identity_assessments").fetchone()[0]
            == 0
        )

    def test_a_label_never_joins_through_the_claimant_or_the_candidate_alone(self, db):
        # With nothing accepted, the effective relation is empty, so a
        # label joined to it is empty whatever was observed.
        _index(db, "one", ((_INTERNAL, _PM_ID),))
        _run(db)
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [(_PM_ID, _LABEL)])
        db.finish_label_generation(g1, "complete")
        joined = _tuples(
            db._conn.execute(
                "SELECT 1 FROM label_membership_observations l JOIN effective_accepted_occurrences e "
                "ON e.account_id = l.account_id AND e.candidate = l.provider_id"
            )
        )
        assert joined == []

    def test_generations_are_numbered_per_account_in_order(self, db):
        numbers = []
        for _ in range(3):
            numbers.append(db.begin_label_generation())
            db.finish_label_generation(numbers[-1], "partial")
        assert numbers == [1, 2, 3]

    def test_a_second_open_generation_is_refused(self, db):
        # An older scan's late write must never undo what a newer complete
        # scan retired, so two generations are never open at once.
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [("A1", _LABEL)])
        with pytest.raises(ValueError, match="already open"):
            db.begin_label_generation()
        db.finish_label_generation(g1, "complete")
        assert db.begin_label_generation() == g1 + 1

    def test_the_database_refuses_two_open_generations_for_one_account(self, db):
        db.begin_label_generation()
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO label_scan_generations (account_id, generation, status, started_at) "
                "VALUES (?, 2, 'in_progress', 't')",
                (db.account_id,),
            )

    def test_a_generation_open_at_startup_is_interrupted(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        first = Database(tmp_path / "g.db", account=TEST_ACCOUNT)
        first.begin_label_generation()
        first.close()
        second = Database(tmp_path / "g.db", account=TEST_ACCOUNT)
        try:
            assert (
                second._conn.execute("SELECT status FROM label_scan_generations").fetchone()[0]
                == "interrupted"
            )
            assert second.label_scan_status() == "unavailable"
        finally:
            second.close()
        assert "label scan generations interrupted by a restart: 1" in caplog.text

    def test_no_marker_in_logs(self, db, caplog):
        caplog.set_level(logging.DEBUG)
        g1 = db.begin_label_generation()
        db.record_label_memberships(g1, [(_PM_ID, _LABEL)])
        db.finish_label_generation(g1, "complete")
        db.interrupt_open_label_generations()
        assert not _leaks(caplog.text)


# ---------------------------------------------------------------------------
# main-loop wiring: counts only, failures by type
# ---------------------------------------------------------------------------


class TestMainLoop:
    def test_a_tick_logs_counts_and_no_value(self, db, caplog):
        caplog.set_level(logging.INFO)
        _index(db, "ok", ((_INTERNAL, _PM_ID),))
        _index(db, "rec", ((_INTERNAL, _PM_OTHER),), folder="Recovered Messages")
        main._run_identity_assessment(db)
        text = caplog.text
        assert "identity assessment: assessed=2 unresolved=1 rejected=1 pass_done=1" in text
        assert not _leaks(text)

    def test_ticks_carry_the_cursor_and_reset_it_at_the_end_of_a_pass(self, db, monkeypatch):
        monkeypatch.setattr(main, "IDENTITY_ASSESSMENT_BATCH", 2)
        for i in range(3):
            _index(db, f"c{i}", ((_INTERNAL, f"SYNTH{i}"),))
        main._run_identity_assessment(db)
        assert main._identity_cursor > 0 and main._identity_next_due == 0.0
        main._run_identity_assessment(db)
        assert main._identity_cursor == 0 and main._identity_next_due > 0.0

    def test_an_idle_tick_logs_nothing(self, db, caplog):
        caplog.set_level(logging.INFO)
        main._run_identity_assessment(db)
        assert "identity assessment" not in caplog.text

    def test_a_failure_is_logged_by_type_alone(self, db, caplog, monkeypatch):
        def boom(*_a, **_k):
            raise sqlite3.OperationalError(f"{_PM_ID} {_LABEL}")

        monkeypatch.setattr(provider_identity, "reassess_batch", boom)
        main._run_identity_assessment(db)
        assert "identity assessment failed: OperationalError" in caplog.text
        assert not _leaks(caplog.text)

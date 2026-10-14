"""#1592: mbsync's collection status file, as the indexer reads and
records it, and the per-account ``account_collection_state`` table (v18).

The file is synthetic throughout; a folder-name marker stands in for
mailbox content and must never reach a log line or the stored row.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from src import collection_status, main
from src.collection_status import (
    COLLECTION_STATUS_NAME,
    CollectionStatus,
    Success,
    Terminal,
    Unknown,
    parse_collection_status,
    read_collection_status,
)
from src.database import SCHEMA_VERSION, Database
from src.maildir import SyncStamp

from tests.conftest import TEST_ACCOUNT
from tests.test_database import _schema_shape, _v17_from_fresh

SCOPE = TEST_ACCOUNT.scope_uuid
OTHER_SCOPE = "00000000-0000-4000-8000-000000000002"
MARKER = "Folders/MarkerZq9-1592"
ENTRYPOINT = Path(__file__).resolve().parents[2] / "mbsync" / "entrypoint.sh"


def _terminal(n: int, outcome: str, at: str, withheld: int | None = 0, cls: str | None = None):
    return {
        "attempt_id": f"0123456789ab-{n}",
        "completed_at": at,
        "outcome": outcome,
        "withheld_far_side_folders": withheld,
        "failure_class": cls,
    }


def _success(n: int, at: str):
    return {"attempt_id": f"0123456789ab-{n}", "completed_at": at}


def _status(attempt: int, outcome: str, terminal=None, success=None, scope: str = SCOPE) -> dict:
    return {
        "format": 1,
        "scope_uuid": scope,
        "run_deadline_secs": 86400,
        "sync_interval_secs": 60,
        "attempt": {
            "id": f"0123456789ab-{attempt}",
            "started_at": "2026-10-12T09:59:00Z",
            "outcome": outcome,
        },
        "last_terminal": terminal,
        "last_success": success,
    }


def _succeeded(n: int, at: str = "2026-10-12T10:00:00Z", withheld: int = 0) -> dict:
    outcome = "succeeded_with_withheld_folders" if withheld else "succeeded"
    return _status(n, outcome, _terminal(n, outcome, at, withheld), _success(n, at))


def _failed(n: int, cls: str = "error", success=None, at: str = "2026-10-12T10:05:00Z") -> dict:
    return _status(n, "failed", _terminal(n, "failed", at, 0, cls), success)


def _write(root: Path, data: dict | str | bytes) -> None:
    """Publish like mbsync: a temporary file renamed into place."""
    tmp = root / f"{COLLECTION_STATUS_NAME}.tmp"
    if isinstance(data, dict):
        data = json.dumps(data)
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, root / COLLECTION_STATUS_NAME)


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).isoformat()


class TestParse:
    def test_each_outcome_is_read(self):
        status = parse_collection_status(json.dumps(_succeeded(3, withheld=2)).encode(), SCOPE)
        assert status == CollectionStatus(
            run_deadline_secs=86400,
            sync_interval_secs=60,
            attempt_id="0123456789ab-3",
            attempt_started_at=_utc("2026-10-12T09:59:00Z"),
            attempt_outcome="succeeded_with_withheld_folders",
            last_terminal=Terminal(
                "0123456789ab-3",
                _utc("2026-10-12T10:00:00Z"),
                "succeeded_with_withheld_folders",
                2,
                None,
            ),
            last_success=Success("0123456789ab-3", _utc("2026-10-12T10:00:00Z")),
        )
        for cls in ("deadline", "killed", "error", "permission_repair", "interrupted"):
            failed = parse_collection_status(json.dumps(_failed(4, cls)).encode(), SCOPE)
            assert isinstance(failed, CollectionStatus)
            assert failed.last_terminal is not None
            assert failed.last_terminal.failure_class == cls
        first = parse_collection_status(json.dumps(_status(1, "in_progress")).encode(), SCOPE)
        assert isinstance(first, CollectionStatus)
        assert (first.attempt_outcome, first.last_terminal, first.last_success) == (
            "in_progress",
            None,
            None,
        )
        # A success whose error-filter counts never arrived: count unknown.
        unknown_count = _succeeded(5)
        unknown_count["last_terminal"]["withheld_far_side_folders"] = None
        parsed = parse_collection_status(json.dumps(unknown_count).encode(), SCOPE)
        assert isinstance(parsed, CollectionStatus)
        assert parsed.last_terminal is not None
        assert parsed.last_terminal.withheld_far_side_folders is None

    def test_every_number_mbsync_can_write_is_read(self):
        """mbsync bounds no interval, count or attempt number below Bash's
        64-bit integers, so neither does the reader (review round 2): a
        long SYNC_INTERVAL must not make every publication unknown. Past
        64 bits, which SQLite cannot store, is malformed."""
        top = 2**63 - 1
        data = _failed(2)
        data["sync_interval_secs"] = 1_000_000_000
        data["last_terminal"]["withheld_far_side_folders"] = 10**10
        data["attempt"]["id"] = data["last_terminal"]["attempt_id"] = "0123456789ab-12345678901"
        status = parse_collection_status(json.dumps(data).encode(), SCOPE)
        assert isinstance(status, CollectionStatus)
        assert status.sync_interval_secs == 1_000_000_000
        assert status.last_terminal is not None
        assert status.last_terminal.withheld_far_side_folders == 10**10
        for field in ("sync_interval_secs", "run_deadline_secs"):
            assert isinstance(
                parse_collection_status(json.dumps(data | {field: top}).encode(), SCOPE),
                CollectionStatus,
            )
            assert parse_collection_status(
                json.dumps(data | {field: top + 1}).encode(), SCOPE
            ) == Unknown("malformed")
        data["last_terminal"]["withheld_far_side_folders"] = top + 1
        assert parse_collection_status(json.dumps(data).encode(), SCOPE) == Unknown("malformed")

    def test_a_stamp_format_file_is_legacy(self):
        legacy = {"completed_at": "2026-10-12T10:00:00Z", "sync_interval_secs": 60}
        assert parse_collection_status(json.dumps(legacy).encode(), SCOPE) == Unknown("legacy")

    def test_another_format_is_unsupported(self):
        data = _succeeded(1)
        data["format"] = 2
        assert parse_collection_status(json.dumps(data).encode(), SCOPE) == Unknown("unsupported")

    def test_another_account_is_a_mismatch(self):
        data = _succeeded(1)
        assert parse_collection_status(json.dumps(data).encode(), OTHER_SCOPE) == Unknown(
            "account_mismatch"
        )

    @staticmethod
    def _variants():
        def edit(change):
            data = _succeeded(2)
            change(data)
            return json.dumps(data).encode()

        def failed_edit(change):
            data = _failed(2)
            change(data)
            return json.dumps(data).encode()

        yield "not json", b"{"
        yield "not utf-8", b'{"format": 1, "x": "\xff"}'
        yield "array", b"[1]"
        yield "bool format", json.dumps({**_succeeded(1), "format": True}).encode()
        yield "extra key", edit(lambda d: d.update(x=1))
        yield "missing key", edit(lambda d: d.pop("last_success"))
        yield (
            "folder in a count",
            edit(lambda d: d["last_terminal"].update(withheld_far_side_folders=MARKER)),
        )
        yield "folder in an outcome", edit(lambda d: d["attempt"].update(outcome=MARKER))
        yield (
            "folder in a terminal outcome",
            edit(lambda d: d["last_terminal"].update(outcome=MARKER)),
        )
        yield "extra key in the attempt", edit(lambda d: d["attempt"].update(x=1))
        yield "terminal record not an object", edit(lambda d: d.update(last_terminal=[]))
        yield (
            "folder in a class",
            failed_edit(lambda d: d["last_terminal"].update(failure_class=MARKER)),
        )
        yield "folder in a time", edit(lambda d: d["attempt"].update(started_at=MARKER))
        yield "bad attempt id", edit(lambda d: d["attempt"].update(id="1"))
        yield (
            "negative count",
            edit(lambda d: d["last_terminal"].update(withheld_far_side_folders=-1)),
        )
        yield (
            "bool count",
            edit(lambda d: d["last_terminal"].update(withheld_far_side_folders=True)),
        )
        yield "float interval", edit(lambda d: d.update(sync_interval_secs=60.0))
        yield "zero deadline", edit(lambda d: d.update(run_deadline_secs=0))
        yield "class on a success", edit(lambda d: d["last_terminal"].update(failure_class="error"))
        yield (
            "no class on a failure",
            failed_edit(lambda d: d["last_terminal"].update(failure_class=None)),
        )
        yield (
            "clean success with a count",
            edit(lambda d: d["last_terminal"].update(withheld_far_side_folders=3)),
        )
        yield (
            "degraded success without a count",
            json.dumps(
                {
                    **_succeeded(2, withheld=1),
                    "last_terminal": _terminal(
                        2, "succeeded_with_withheld_folders", "2026-10-12T10:00:00Z", 0
                    ),
                }
            ).encode(),
        )
        yield (
            "finished attempt not the terminal one",
            edit(lambda d: d["attempt"].update(id="0123456789ab-9")),
        )
        yield (
            "finished attempt with another outcome",
            failed_edit(lambda d: d["attempt"].update(outcome="succeeded")),
        )
        yield "success not the last success", edit(lambda d: d.update(last_success=None))
        yield (
            "success at another time",
            edit(lambda d: d["last_success"].update(completed_at="2026-10-12T11:00:00Z")),
        )
        yield "invalid date", edit(lambda d: d["attempt"].update(started_at="2026-13-12T10:00:00Z"))
        yield "oversized", json.dumps({**_succeeded(1), "pad": "x" * 5000}).encode()

    def test_every_malformed_shape_is_unknown(self, caplog):
        caplog.set_level(logging.DEBUG)
        for name, raw in self._variants():
            assert parse_collection_status(raw, SCOPE) == Unknown("malformed"), name
        assert MARKER not in caplog.text


class TestRead:
    def test_missing_file(self, tmp_path):
        assert read_collection_status(tmp_path, SCOPE) == Unknown("missing")

    def test_a_directory_fifo_or_link_is_unreadable_without_blocking(self, tmp_path):
        path = tmp_path / COLLECTION_STATUS_NAME
        path.mkdir()
        assert read_collection_status(tmp_path, SCOPE) == Unknown("unreadable")
        path.rmdir()
        os.mkfifo(path)
        assert read_collection_status(tmp_path, SCOPE) == Unknown("unreadable")
        path.unlink()
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps(_succeeded(1)))
        path.symlink_to(target)
        assert read_collection_status(tmp_path, SCOPE) == Unknown("unreadable")

    def test_a_read_error_is_unreadable(self, tmp_path, monkeypatch):
        _write(tmp_path, _succeeded(1))

        def failing_read(fd, n):
            raise OSError(5, "synthetic", MARKER)

        monkeypatch.setattr(collection_status.os, "read", failing_read)
        assert read_collection_status(tmp_path, SCOPE) == Unknown("unreadable")

    def test_reads_at_most_the_bound(self, tmp_path, monkeypatch):
        _write(tmp_path, b" " * 100_000)
        read_sizes = []
        real_read = os.read

        def counting_read(fd, n):
            read_sizes.append(n)
            return real_read(fd, n)

        monkeypatch.setattr(collection_status.os, "read", counting_read)
        assert read_collection_status(tmp_path, SCOPE) == Unknown("malformed")
        assert sum(read_sizes) <= collection_status.MAX_STATUS_BYTES + 1

    def test_a_replacement_during_the_read_yields_one_whole_publication(
        self, tmp_path, monkeypatch
    ):
        """mbsync renames a new publication over the file while the
        indexer reads it: the open descriptor reads the old one whole."""
        _write(tmp_path, _failed(1))
        real_read = os.read
        replaced = []

        def replacing_read(fd, n):
            if not replaced:
                replaced.append(True)
                _write(tmp_path, _succeeded(2))
            return real_read(fd, n)

        monkeypatch.setattr(collection_status.os, "read", replacing_read)
        first = read_collection_status(tmp_path, SCOPE)
        assert isinstance(first, CollectionStatus) and first.attempt_id == "0123456789ab-1"
        second = read_collection_status(tmp_path, SCOPE)
        assert isinstance(second, CollectionStatus) and second.attempt_id == "0123456789ab-2"


def _bash_functions(*names: str) -> str:
    """The entrypoint's function definitions, from each ``name() {`` line
    through the first lone ``}``, as mbsync/tests/entrypoint_test.sh loads
    them."""
    lines = ENTRYPOINT.read_text().splitlines()
    out = []
    for name in names:
        start = lines.index(f"{name}() {{")
        end = lines.index("}", start)
        out.extend(lines[start : end + 1])
    return "\n".join(out)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
class TestEntrypointAgreement:
    """What mbsync's own functions publish is what the indexer reads: one
    format, written and read by two implementations, cannot drift."""

    def _publish(
        self, tmp_path: Path, steps: str, interval: int = 60
    ) -> CollectionStatus | Unknown:
        constants = "\n".join(
            line.removeprefix("readonly ")
            for line in ENTRYPOINT.read_text().splitlines()
            if line.startswith("readonly COLLECTION_")
            and not line.startswith("readonly COLLECTION_STATUS_FILE")
        )
        script = "\n".join(
            [
                "set -Eeuo pipefail",
                constants,
                f'COLLECTION_STATUS_FILE="{tmp_path}/{COLLECTION_STATUS_NAME}"',
                f'ACCOUNT_SCOPE="{SCOPE}"',
                "SYNC_DEADLINE_SECONDS=86400",
                f"SYNC_INTERVAL={interval}",
                'collection_count=0 collection_attempt_id="" collection_attempt_started_at=""',
                "collection_last_terminal=null collection_last_success=null",
                _bash_functions(
                    "collection_now",
                    "load_collection_records",
                    "publish_collection_status",
                    "begin_collection_attempt",
                    "finish_collection_attempt",
                ),
                "load_collection_records",
                steps,
            ]
        )
        run = subprocess.run(
            ["bash", "-c", script], check=True, capture_output=True, text=True, timeout=30
        )
        assert "WARNING" not in run.stderr
        return read_collection_status(tmp_path, SCOPE)

    def test_long_intervals_and_large_counts_are_read_and_carried(self, tmp_path):
        """Review round 2: every value mbsync accepts is read back, by the
        indexer and by mbsync's own restart (no warning, records kept)."""
        steps = 'begin_collection_attempt\nfinish_collection_attempt failed 10000000000 error "$(collection_now)"'
        status = self._publish(tmp_path, steps, interval=1_000_000_000)
        assert isinstance(status, CollectionStatus)
        assert status.sync_interval_secs == 1_000_000_000
        assert status.last_terminal is not None
        assert status.last_terminal.withheld_far_side_folders == 10**10
        restarted = self._publish(tmp_path, "begin_collection_attempt", interval=1_000_000_000)
        assert isinstance(restarted, CollectionStatus)
        assert restarted.last_terminal == status.last_terminal

    def test_each_publication_is_read(self, tmp_path):
        status = self._publish(tmp_path, "begin_collection_attempt")
        assert isinstance(status, CollectionStatus)
        assert (status.attempt_outcome, status.last_terminal, status.last_success) == (
            "in_progress",
            None,
            None,
        )
        cases = [
            ("succeeded 0 null", ("succeeded", 0, None)),
            ("succeeded null null", ("succeeded", None, None)),
            (
                "succeeded_with_withheld_folders 2 null",
                ("succeeded_with_withheld_folders", 2, None),
            ),
            ("failed 1 error", ("failed", 1, "error")),
            ("failed null interrupted", ("failed", None, "interrupted")),
            ("failed 0 permission_repair", ("failed", 0, "permission_repair")),
        ]
        for args, (outcome, withheld, cls) in cases:
            status = self._publish(
                tmp_path,
                "begin_collection_attempt\n"
                f'set -- {args}\nfinish_collection_attempt "$1" "$2" "$3" "$(collection_now)"',
            )
            assert isinstance(status, CollectionStatus), args
            assert status.attempt_outcome == outcome
            assert status.last_terminal is not None
            assert (
                status.last_terminal.outcome,
                status.last_terminal.withheld_far_side_folders,
                status.last_terminal.failure_class,
            ) == (outcome, withheld, cls)
        # A restart read the earlier records back: the next attempt is in
        # progress beside the last terminal and last success.
        status = self._publish(tmp_path, "begin_collection_attempt")
        assert isinstance(status, CollectionStatus)
        assert status.attempt_outcome == "in_progress"
        assert status.last_terminal is not None and status.last_terminal.failure_class == (
            "permission_repair"
        )
        assert status.last_success is not None


# --- the recorder (main._IngestionStateRecorder) ------------------------------


def _row(db: Database) -> dict | None:
    return db.get_collection_state()


def _stamp(at: str, attempt_id: str, generation: int) -> SyncStamp:
    """An acknowledged success stamp naming the attempt it delivered
    (2026-10-13, #1621 thread): ``attempt_id`` must match the matching
    ``_success(n, ...)``'s for ``_success_delivered`` to prove delivery."""
    return SyncStamp(_utc(at), 60, attempt_id, generation)


class TestRecorder:
    def test_records_the_status_with_the_ingestion_state(self, tmp_path, db):
        _write(tmp_path, _succeeded(1, withheld=2))
        recorder = main._IngestionStateRecorder(db, tmp_path)
        recorder.acknowledge(_stamp("2026-10-12T10:00:00Z", "0123456789ab-1", 1))
        recorder.maybe_record(now=100.0)
        row = _row(db)
        assert row is not None
        assert {k: row[k] for k in row if k != "observed_at"} == {
            "account_id": db.account_id,
            "status": "known",
            "unknown_reason": None,
            "run_deadline_secs": 86400,
            "sync_interval_secs": 60,
            "attempt_id": "0123456789ab-1",
            "attempt_started_at": _utc("2026-10-12T09:59:00Z"),
            "attempt_outcome": "succeeded_with_withheld_folders",
            "terminal_attempt_id": "0123456789ab-1",
            "terminal_completed_at": _utc("2026-10-12T10:00:00Z"),
            "terminal_outcome": "succeeded_with_withheld_folders",
            "terminal_withheld_folders": 2,
            "terminal_failure_class": None,
            "success_attempt_id": "0123456789ab-1",
            "success_completed_at": _utc("2026-10-12T10:00:00Z"),
            "success_delivered": 1,
            "unrecovered_failure_attempt_id": None,
        }
        seen = db._conn.execute("SELECT indexer_seen_at FROM ingestion_state").fetchone()[0]
        assert row["observed_at"] == seen

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (None, "missing"),
            ({"completed_at": "2026-10-12T10:00:00Z", "sync_interval_secs": 60}, "legacy"),
            ("{" + MARKER, "malformed"),
            ({**_succeeded(1), "format": 2}, "unsupported"),
            (_succeeded(1, withheld=1) | {"scope_uuid": OTHER_SCOPE}, "account_mismatch"),
        ],
    )
    def test_an_unusable_file_reads_as_unknown_with_one_warning(
        self, tmp_path, db, caplog, content, reason
    ):
        caplog.set_level(logging.INFO)
        if content is not None:
            _write(tmp_path, content)
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=0)
        recorder.maybe_record(now=1.0)
        recorder.maybe_record(now=2.0)
        row = _row(db)
        assert row is not None
        assert (row["status"], row["unknown_reason"]) == ("unknown", reason)
        assert row["attempt_outcome"] is None and row["terminal_outcome"] is None
        warnings = [
            r for r in caplog.records if "mbsync collection status unavailable" in r.getMessage()
        ]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert f"reason={reason}" in warnings[0].getMessage()
        assert MARKER not in caplog.text
        # Readable again: an INFO line, and the row is known.
        _write(tmp_path, _succeeded(1))
        recorder.maybe_record(now=3.0)
        assert _row(db)["status"] == "known"  # type: ignore[index]
        assert "mbsync collection status is readable again" in caplog.text

    def test_a_failure_holds_through_retries_until_a_success_is_delivered(
        self, tmp_path, db, caplog
    ):
        caplog.set_level(logging.INFO)
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=0)
        _write(tmp_path, _succeeded(1))
        recorder.acknowledge(_stamp("2026-10-12T10:00:00Z", "0123456789ab-1", 1))
        recorder.maybe_record(now=1.0)
        assert _row(db)["unrecovered_failure_attempt_id"] is None  # type: ignore[index]

        failed = _failed(2, "deadline", _success(1, "2026-10-12T10:00:00Z"))
        _write(tmp_path, failed)
        recorder.maybe_record(now=2.0)
        assert _row(db)["unrecovered_failure_attempt_id"] == "0123456789ab-2"  # type: ignore[index]
        warnings = [r for r in caplog.records if "collection attempt failed" in r.getMessage()]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert "failure_class=deadline" in warnings[0].getMessage()

        # A retry in progress keeps the earlier failure as last_terminal.
        retry = {
            **failed,
            "attempt": {**failed["attempt"], "id": "0123456789ab-3", "outcome": "in_progress"},
        }
        _write(tmp_path, retry)
        recorder.maybe_record(now=3.0)
        assert _row(db)["unrecovered_failure_attempt_id"] == "0123456789ab-2"  # type: ignore[index]

        # The retry succeeds, but its stamp is not acknowledged yet.
        _write(tmp_path, _succeeded(3, at="2026-10-12T10:10:00Z"))
        recorder.maybe_record(now=4.0)
        row = _row(db)
        assert row["unrecovered_failure_attempt_id"] == "0123456789ab-2"  # type: ignore[index]
        assert row["success_delivered"] == 0  # type: ignore[index]
        assert "collection recovered" not in caplog.text

        # Its stamp is acknowledged: recovered at the next read.
        recorder.acknowledge_watched(_stamp("2026-10-12T10:10:00Z", "0123456789ab-3", 2))
        recorder.maybe_record(now=5.0)
        row = _row(db)
        assert row["unrecovered_failure_attempt_id"] is None  # type: ignore[index]
        assert row["success_delivered"] == 1  # type: ignore[index]
        recoveries = [r for r in caplog.records if "collection recovered" in r.getMessage()]
        assert len(recoveries) == 1 and recoveries[0].levelno == logging.INFO
        assert len([r for r in caplog.records if "attempt failed" in r.getMessage()]) == 1

    def test_a_clock_rollback_does_not_reorder_the_records(self, tmp_path, db):
        """After a rollback the success's time sorts before the failure's;
        publication order still decides, and delivery is still proven by
        attempt ID equality, never by comparing times (#1621 thread,
        2026-10-13)."""
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=0)
        future = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
        _write(tmp_path, _failed(1, at=future.strftime("%Y-%m-%dT%H:%M:%SZ")))
        recorder.maybe_record(now=1.0)
        assert _row(db)["unrecovered_failure_attempt_id"] == "0123456789ab-1"  # type: ignore[index]
        earlier = "2026-10-12T08:00:00Z"
        _write(tmp_path, _succeeded(2, at=earlier))
        recorder.acknowledge_watched(_stamp(earlier, "0123456789ab-2", 1))
        recorder.maybe_record(now=2.0)
        row = _row(db)
        assert row["terminal_outcome"] == "succeeded"  # type: ignore[index]
        assert row["unrecovered_failure_attempt_id"] is None  # type: ignore[index]

    def test_an_unrecovered_failure_survives_a_restart(self, tmp_path, db, caplog):
        caplog.set_level(logging.INFO)
        _write(tmp_path, _failed(1))
        main._IngestionStateRecorder(db, tmp_path).maybe_record(now=1.0)
        # The next start sees the retry's success, not yet delivered.
        _write(tmp_path, _succeeded(2))
        restarted = main._IngestionStateRecorder(db, tmp_path)
        restarted.maybe_record(now=1.0)
        assert _row(db)["unrecovered_failure_attempt_id"] == "0123456789ab-1"  # type: ignore[index]
        assert len([r for r in caplog.records if "attempt failed" in r.getMessage()]) == 1

    def test_a_rebuilt_index_recovers_the_state_from_the_file(self, tmp_path, db):
        _write(tmp_path, _failed(4, "permission_repair"))
        assert _row(db) is None
        recorder = main._IngestionStateRecorder(db, tmp_path)
        recorder.read_collection()
        recorder.maybe_record(now=1.0)
        row = _row(db)
        assert row["terminal_failure_class"] == "permission_repair"  # type: ignore[index]
        assert row["unrecovered_failure_attempt_id"] == "0123456789ab-4"  # type: ignore[index]

    def test_a_stored_state_read_error_waits_for_the_next_read(self, tmp_path, db, caplog):
        _write(tmp_path, _failed(1))
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=0)

        def locked():
            raise sqlite3.OperationalError(MARKER)

        db.get_collection_state = locked  # type: ignore[method-assign]
        recorder.read_collection()
        assert recorder._collection is None
        assert "reading the stored collection state failed: OperationalError" in caplog.text
        assert MARKER not in caplog.text
        # The heartbeat still records liveness, without a collection row.
        recorder.maybe_record(now=1.0)
        assert db._conn.execute("SELECT COUNT(*) FROM ingestion_state").fetchone()[0] == 1
        del db.get_collection_state
        assert _row(db) is None
        recorder.maybe_record(now=2.0)
        assert _row(db)["status"] == "known"  # type: ignore[index]

    def test_the_two_rows_commit_together(self, db):
        """A collection row the table refuses rolls the ingestion state
        back with it: mcp-server never reads half a snapshot."""
        with pytest.raises(sqlite3.IntegrityError):
            db.record_ingestion_state(
                sync_completed_at=None,
                sync_interval_secs=None,
                seen_at="2026-10-12T10:00:00+00:00",
                collection={"status": "known", "attempt_outcome": MARKER},
            )
        assert db._conn.execute("SELECT COUNT(*) FROM ingestion_state").fetchone()[0] == 0
        assert _row(db) is None

    def test_the_rename_onto_the_file_reads_it_and_acknowledges_nothing(
        self, tmp_path, db, monkeypatch
    ):
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        recorder = main._IngestionStateRecorder(db, tmp_path)
        queue = SimpleNamespace(enqueue=lambda *a: pytest.fail("enqueued"))
        handler = main.MaildirHandler(db, queue, ingestion_state=recorder)  # type: ignore[arg-type]
        _write(tmp_path, _succeeded(1))
        handler.on_moved(
            SimpleNamespace(
                is_directory=False,
                src_path=str(tmp_path / f"{COLLECTION_STATUS_NAME}.abcdef"),
                dest_path=str(tmp_path / COLLECTION_STATUS_NAME),
            )
        )
        assert isinstance(recorder._collection, CollectionStatus)
        assert recorder._acked is None
        # Without a recorder (as some callers build the handler) it is
        # ignored, never treated as mail.
        main.MaildirHandler(db, queue).on_moved(  # type: ignore[arg-type]
            SimpleNamespace(
                is_directory=False,
                src_path=str(tmp_path / f"{COLLECTION_STATUS_NAME}.abcdef"),
                dest_path=str(tmp_path / COLLECTION_STATUS_NAME),
            )
        )


class TestMigrationV18:
    def test_v17_migrates_to_the_fresh_shape_with_rows_kept(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "v17.db", account=TEST_ACCOUNT)
        db.record_ingestion_state(
            sync_completed_at="2026-10-12T10:00:00+00:00",
            sync_interval_secs=60,
            seen_at="2026-10-12T10:00:30+00:00",
        )
        _v17_from_fresh(db)
        db.close()
        migrated = Database(tmp_path / "v17.db", account=TEST_ACCOUNT)
        fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
        try:
            assert SCHEMA_VERSION == 18
            assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
            assert migrated.get_collection_state() is None
            state = migrated._conn.execute(
                "SELECT sync_completed_at FROM ingestion_state"
            ).fetchone()[0]
            assert state == "2026-10-12T10:00:00+00:00"
            assert migrated._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
            assert migrated._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
        finally:
            migrated.close()
            fresh.close()
        assert "applied migrations: [18]" in caplog.text

    def test_a_row_must_name_a_registered_account(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO account_collection_state (account_id, status, observed_at) "
                "VALUES (99, 'known', 'x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO account_collection_state (account_id, status, observed_at) "
                "VALUES (?, 'unknown', 'x')",
                (db.account_id,),
            )

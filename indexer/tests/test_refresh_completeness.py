"""A missing source keeps its refresh obligation across repeated retries."""

from pathlib import Path

import pytest
from src import main
from src.occurrence_scan import file_identity
from src.queue import REASON_REEXTRACT, IndexingQueue
from src.threader import Threader

from tests.conftest import make_mock_embedder
from tests.test_occurrence_reconciliation import Box, _eml
from tests.test_rename_wait import (
    _SHAPES,
    _VECTOR,
    _drain,
    _FakeEvent,
    _jobs,
    _make_due,
)


@pytest.mark.parametrize("reason", sorted(_SHAPES))
def test_missing_source_refresh_survives_multiple_waits_then_completes(
    tmp_path, monkeypatch, reason
):
    db, queue, path, repaired = _SHAPES[reason](tmp_path, monkeypatch)
    try:
        original = _jobs(db)[path]
        old = Path(path)
        dest = old.with_name(old.name + "R")
        old.rename(dest)
        embedder = make_mock_embedder(_VECTOR)
        for _ in range(3):
            _make_due(db)
            _drain(db, queue, embedder)
            waiting = _jobs(db)[path]
            assert waiting["attempts"] == original["attempts"]
            assert waiting["reason"] == reason
            assert waiting["last_error"] == main.RENAME_DEFERRED_ERROR
        main.MaildirHandler(db, queue).on_moved(_FakeEvent(str(old), str(dest)))
        _make_due(db)
        _drain(db, queue, embedder)
        assert _jobs(db) == {}
        repaired(embedder)
    finally:
        db.close()


@pytest.mark.parametrize("destination", ["absent", "queued", "dead"])
@pytest.mark.parametrize("scan_bound", [False, True])
def test_missing_refresh_transfers_without_resetting_destination_state(
    tmp_path, destination, scan_bound
):
    box = Box(tmp_path)
    try:
        raw = _eml("refresh@example.test")
        old = box.put("INBOX/cur/one:2,S", raw)
        box.index(old)
        other = box.put("Archive/cur/two:2,S", raw)
        if scan_bound:
            claimant = box.db.find_active_occurrence(str(old))["claimant_id"]
            box.db.bind_found_copy(claimant, str(other), identity=file_identity(other))
        else:
            box.index(other)
        old.unlink()
        box.reconciler.sweep()
        queue = IndexingQueue(box.db, max_attempts=8, base_backoff_seconds=0)
        queue.enqueue(str(old), REASON_REEXTRACT)
        box.db._conn.execute(
            "UPDATE indexing_jobs SET attempts = 2 WHERE filepath = ?", (str(old),)
        )
        if destination != "absent":
            queue.enqueue(str(other), "existing-intent")
            box.db._conn.execute(
                "UPDATE indexing_jobs SET attempts = 4, status = ? WHERE filepath = ?",
                (destination, str(other)),
            )
        box.db._conn.commit()
        before = dict(_jobs(box.db)[str(other)]) if destination != "absent" else None
        row = _jobs(box.db)[str(old)]
        assert queue.begin_attempt(str(old))
        main._phase1_commit_thread(row, box.db, Threader(box.db), queue)
        queue.end_attempt(str(old))
        jobs = _jobs(box.db)
        assert str(old) not in jobs
        assert not box.db.is_indexed(str(old))
        if before is not None:
            assert dict(jobs[str(other)]) == before
        else:
            assert jobs[str(other)]["attempts"] == 2
            assert jobs[str(other)]["reason"] == REASON_REEXTRACT
    finally:
        box.close()


def test_scan_bound_missing_source_keeps_its_refresh_without_an_indexed_marker(tmp_path):
    box = Box(tmp_path)
    try:
        old = box.put("INBOX/cur/one:2,S", _eml("refresh@example.test"))
        box.index(old)
        box.db._conn.execute("DELETE FROM indexed_files")
        box.db._conn.commit()
        old.unlink()
        queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
        queue.enqueue(str(old), REASON_REEXTRACT)
        row = _jobs(box.db)[str(old)]
        main._phase1_commit_thread(row, box.db, Threader(box.db), queue)
        assert _jobs(box.db)[str(old)]["last_error"] == main.RENAME_DEFERRED_ERROR
    finally:
        box.close()


@pytest.mark.parametrize("failure", ["mismatch", "unreadable"])
def test_missing_refresh_hashes_are_bounded_and_make_progress(
    tmp_path, monkeypatch, failure, caplog
):
    import hashlib
    import logging
    import time

    from src import file_evidence

    box = Box(tmp_path)
    try:
        raw = _eml("refresh@example.test")
        old = box.put("INBOX/cur/old:2,S", raw)
        box.index(old)
        copies = [box.put(f"Archive/cur/copy-{i}:2,S", raw) for i in range(6)]
        box.index(*copies)
        old.unlink()
        box.reconciler.sweep()
        queue = IndexingQueue(box.db, max_attempts=8, base_backoff_seconds=0)
        queue.enqueue(str(old), REASON_REEXTRACT)
        monkeypatch.setattr(file_evidence, "MAX_HASH_FILES", 2)
        monkeypatch.setattr(file_evidence, "MAX_HASH_BYTES", 2 * len(raw))
        reads = []

        def digest(path, identity):
            reads.append(path)
            if path == copies[-1]:
                return hashlib.sha256(raw).hexdigest()
            if failure == "unreadable":
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return "0" * 64

        monkeypatch.setattr(file_evidence, "read_digest", digest)
        monkeypatch.setattr("src.database.read_digest", digest)
        caplog.set_level(logging.INFO)
        started = time.monotonic()
        for _ in range(3):
            before = len(reads)
            assert box.db.retain_or_retarget_missing_job(str(old))
            assert len(reads) - before <= 2
        assert reads == copies
        assert str(old) not in _jobs(box.db)
        assert str(copies[-1]) in _jobs(box.db)
        assert "hash verification deferred" in caplog.text
        assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
        assert time.monotonic() - started < 5
    finally:
        box.close()


def test_missing_refresh_cleanup_does_not_scan_unrelated_queue_rows(tmp_path):
    box = Box(tmp_path)
    try:
        path = box.put("INBOX/cur/old:2,S", _eml("refresh@example.test"))
        box.index(path)
        path.unlink()
        queue = IndexingQueue(box.db, max_attempts=8, base_backoff_seconds=0)
        queue.enqueue(str(path), REASON_REEXTRACT)
        for i in range(100):
            queue.enqueue(str(box.root / f"INBOX/cur/unrelated-{i}:2,S"), REASON_REEXTRACT)
        queries = []
        box.db._conn.set_trace_callback(queries.append)
        assert box.db.retain_or_retarget_missing_job(str(path))
        box.db._conn.set_trace_callback(None)
        assert "SELECT filepath FROM indexing_jobs" not in queries
        assert str(path) in box.db._missing_job_checks
        queue.mark_skipped(str(path), reason="file_missing")
        assert str(path) not in box.db._missing_job_checks
    finally:
        box.close()


def test_unknown_claimant_hash_cannot_transfer_unverified_refresh(tmp_path, monkeypatch):
    from src.file_evidence import FolderInventory

    box = Box(tmp_path)
    try:
        raw = _eml("refresh@example.test")
        old = box.put("INBOX/cur/old:2,S", raw)
        other = box.put("Archive/cur/copy:2,S", raw)
        box.index(old, other)
        old.unlink()
        box.reconciler.sweep()
        box.db._conn.execute("UPDATE messages SET content_hash = NULL")
        box.db._conn.execute("DELETE FROM indexed_files WHERE filepath = ?", (str(old),))
        box.db._conn.commit()
        queue = IndexingQueue(box.db, max_attempts=8, base_backoff_seconds=0)
        queue.enqueue(str(old), REASON_REEXTRACT)
        monkeypatch.setattr(FolderInventory, "digest", lambda *_args: None)
        assert box.db.retain_or_retarget_missing_job(str(old))
        assert str(old) in _jobs(box.db)
        assert str(other) not in _jobs(box.db)
    finally:
        box.close()

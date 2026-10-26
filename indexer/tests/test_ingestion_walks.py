"""Durable, counts-only evidence for completed Maildir enumeration (#1363)."""

import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from src import main
from src.queue import IndexingQueue


def _stamp(root: Path, generation=1):
    (root / main.SYNC_STAMP_NAME).write_text(
        json.dumps(
            {
                "completed_at": "2026-10-25T10:00:00Z",
                "sync_interval_secs": 60,
                "attempt_id": f"0123456789ab-{generation}",
                "generation": generation,
            }
        )
    )


def _walks(db):
    return [dict(r) for r in db._conn.execute("SELECT * FROM ingestion_walks ORDER BY id")]


def test_missed_events_are_queued_before_the_walk_is_recorded(db, tmp_path):
    root = tmp_path / "maildir"
    cur = root / "INBOX" / "cur"
    cur.mkdir(parents=True)
    (cur / "synthetic").write_bytes(b"synthetic mail")
    _stamp(root)
    queue = IndexingQueue(db)
    assert main._enqueue_unindexed_messages(db, queue, root, main.REASON_RESCAN) == 1
    (row,) = _walks(db)
    assert row["root"] == str(root)
    assert row["account_id"] == db.account_id
    assert row["sync_generation"] == 1
    assert row["sync_attempt_id"] == "0123456789ab-1"
    assert row["files_seen"] == row["enqueued"] == 1
    assert row["unreadable_directories"] == 0
    assert row["started_at"] <= row["completed_at"]
    assert queue.has_pending_row(str(cur / "synthetic"))
    with closing(sqlite3.connect(db.path)) as reader:
        assert reader.execute("SELECT files_seen FROM ingestion_walks").fetchone()[0] == 1
    main._enqueue_unindexed_messages(db, queue, root, main.REASON_RESCAN)
    assert len(_walks(db)) == 2
    assert _walks(db)[1]["enqueued"] == 0


def test_sync_published_during_walk_is_not_claimed(db, tmp_path, monkeypatch):
    _stamp(tmp_path)
    original = main._iter_maildir_messages

    def walk(root, **kwargs):
        _stamp(root, 2)
        yield from original(root, **kwargs)

    monkeypatch.setattr(main, "_iter_maildir_messages", walk)
    main._enqueue_unindexed_messages(db, IndexingQueue(db), tmp_path, main.REASON_RESCAN)
    assert _walks(db)[0]["sync_generation"] == 1


def test_unreadable_counts_are_persisted_without_names(db, tmp_path, monkeypatch, caplog):
    marker = "PRIVATE-FOLDER-MARKER"
    original = os.walk

    def walk(root, *, onerror):
        onerror(PermissionError(13, marker, str(tmp_path / marker)))
        yield from original(root, onerror=onerror)

    monkeypatch.setattr(os, "walk", walk)
    caplog.set_level(logging.INFO)
    main._enqueue_unindexed_messages(db, IndexingQueue(db), tmp_path, main.REASON_RESCAN)
    (row,) = _walks(db)
    assert row["unreadable_directories"] == 1
    assert marker not in json.dumps(row)
    assert marker not in caplog.text
    assert "skipped 1 director" in caplog.text


def test_failed_enqueue_does_not_record_completed_walk(db, tmp_path, monkeypatch):
    cur = tmp_path / "cur"
    cur.mkdir()
    (cur / "synthetic").write_bytes(b"synthetic")
    queue = IndexingQueue(db)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(queue, "enqueue", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        main._enqueue_unindexed_messages(db, queue, tmp_path, main.REASON_RESCAN)
    assert _walks(db) == []


def test_missing_stamp_does_not_invent_sync(db, tmp_path):
    main._enqueue_unindexed_messages(db, IndexingQueue(db), tmp_path, main.REASON_RESCAN)
    (row,) = _walks(db)
    assert row["sync_generation"] is None
    assert row["sync_attempt_id"] is None


def test_missing_root_is_not_a_successfully_empty_walk(db, tmp_path):
    root = tmp_path / "missing"
    main._enqueue_unindexed_messages(db, IndexingQueue(db), root, main.REASON_RESCAN)
    (row,) = _walks(db)
    assert row["files_seen"] == 0
    assert row["unreadable_directories"] == 1


def test_v27_upgrade_starts_unknown_and_preserves_existing_data(tmp_path):
    from src.database import SCHEMA_VERSION, Database

    from tests.conftest import TEST_ACCOUNT
    from tests.test_database import _schema_shape

    path = tmp_path / "old.db"
    old = Database(path, account=TEST_ACCOUNT)
    IndexingQueue(old).enqueue("/maildir/cur/synthetic", main.REASON_RESCAN)
    before = [tuple(r) for r in old._conn.execute("SELECT * FROM indexing_jobs")]
    old._conn.execute("DROP TABLE ingestion_walks")
    old._conn.execute("UPDATE schema_version SET version=27")
    old._conn.commit()
    old.close()
    upgraded = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert (
            upgraded._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert _walks(upgraded) == []
        assert _schema_shape(upgraded._conn) == _schema_shape(fresh._conn)
        assert [tuple(r) for r in upgraded._conn.execute("SELECT * FROM indexing_jobs")] == before
    finally:
        upgraded.close()
        fresh.close()


def test_walk_record_failure_rolls_back_and_can_retry(db, tmp_path):
    db._conn.execute(
        "CREATE TRIGGER fail_walk BEFORE INSERT ON ingestion_walks "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
    )
    db._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        main._enqueue_unindexed_messages(db, IndexingQueue(db), tmp_path, main.REASON_RESCAN)
    assert not db._conn.in_transaction
    assert _walks(db) == []
    db._conn.execute("DROP TRIGGER fail_walk")
    db._conn.commit()
    main._enqueue_unindexed_messages(db, IndexingQueue(db), tmp_path, main.REASON_RESCAN)
    assert len(_walks(db)) == 1

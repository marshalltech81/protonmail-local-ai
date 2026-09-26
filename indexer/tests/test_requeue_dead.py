"""Tests for src/requeue_dead.py — the operator rescue command."""

import pytest
from src import requeue_dead
from src.database import Database
from src.queue import REASON_INITIAL_SCAN, IndexingQueue


def _seed(db_path):
    db = Database(db_path)
    q = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
    q.enqueue("/m/exhausted", REASON_INITIAL_SCAN)
    q.mark_failed("/m/exhausted", stage="embed", error="boom")
    q.enqueue("/m/oversized", REASON_INITIAL_SCAN)
    q.mark_dead_terminal("/m/oversized", stage="parse", error="oversized")
    db.close()


def _status(db_path, filepath):
    db = Database(db_path)
    try:
        return IndexingQueue(db).is_dead(filepath)
    finally:
        db.close()


def test_requeues_all_dead_rows(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "mail.db"
    _seed(db_path)
    monkeypatch.setenv("SQLITE_PATH", str(db_path))

    assert requeue_dead.main([]) == 0

    assert not _status(db_path, "/m/exhausted")
    assert not _status(db_path, "/m/oversized")
    assert "Requeued 2 dead-lettered" in capsys.readouterr().out


def test_class_filter(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "mail.db"
    _seed(db_path)
    monkeypatch.setenv("SQLITE_PATH", str(db_path))

    assert requeue_dead.main(["--class", "retryable"]) == 0

    assert not _status(db_path, "/m/exhausted")
    assert _status(db_path, "/m/oversized")
    assert "Requeued 1 dead-lettered" in capsys.readouterr().out


def test_rejects_unknown_class(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mail.db"))
    with pytest.raises(SystemExit):
        requeue_dead.main(["--class", "bogus"])


def test_missing_database_fails_without_creating_one(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "absent.db"
    monkeypatch.setenv("SQLITE_PATH", str(db_path))

    assert requeue_dead.main([]) == 1

    assert not db_path.exists()
    assert "No index database" in capsys.readouterr().err

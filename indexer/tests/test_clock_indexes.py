"""Selected-clock indexes migrate atomically without changing indexed evidence."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from src.database import SCHEMA_VERSION, Database
from src.migrations.runner import apply_pending

from tests.conftest import TEST_ACCOUNT, make_message, make_thread
from tests.test_database import _schema_shape, _v15_from_fresh


def _indexes(conn):
    return {
        name: (
            [r[2] for r in conn.execute(f"PRAGMA index_info({name})")],
            " ".join(sql.split("WHERE", 1)[1].split()),
        )
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_messages_sent', 'idx_messages_occurred')"
        )
    }


def test_fresh_clock_indexes(db):
    assert _indexes(db._conn) == {
        "idx_messages_sent": (["sent_at", "claimant_id"], "sent_at_status = 'parsed'"),
        "idx_messages_occurred": (["occurred_at", "claimant_id"], "(occurred_at IS NOT NULL) = 1"),
    }


def test_v15_migration_preserves_rows_and_queue_and_matches_fresh(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path, account=TEST_ACCOUNT)
    message = make_message(message_id="synthetic-clock@test.example")
    db.upsert_thread(make_thread(messages=[message]), [0.1] * 4096)
    _v15_from_fresh(db)
    before = {
        table: [tuple(r) for r in db._conn.execute(f"SELECT * FROM {table}")]
        for table in ("messages", "message_thread_map", "threads", "threads_vec", "indexing_jobs")
    }
    assert apply_pending(
        db._conn,
        current_version=15,
        target_version=16,
        migration_dir=Path(__file__).parents[1] / "src/migrations",
    ) == [16]
    for table, rows in before.items():
        assert [tuple(r) for r in db._conn.execute(f"SELECT * FROM {table}")] == rows
    db.close()
    migrated = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert (
            migrated._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
        assert _indexes(migrated._conn) == _indexes(fresh._conn)
    finally:
        migrated.close()
        fresh.close()
    reopened = Database(path, account=TEST_ACCOUNT)
    try:
        assert len(_indexes(reopened._conn)) == 2
    finally:
        reopened.close()


def test_second_index_failure_rolls_back_first_and_version(tmp_path):
    path = tmp_path / "failure.db"
    db = Database(path, account=TEST_ACCOUNT)
    _v15_from_fresh(db)
    db._conn.execute("CREATE INDEX idx_messages_occurred ON messages(occurred_at)")
    db._conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError, match="idx_messages_occurred"):
            db._migrate()
    finally:
        db.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 15
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='idx_messages_sent'").fetchone()
            is None
        )


def test_effective_clock_index_migrates_without_changing_evidence(tmp_path):
    path = tmp_path / "effective.db"
    db = Database(path, account=TEST_ACCOUNT)
    message = make_message(message_id="synthetic-effective@test.example")
    db.upsert_thread(make_thread(messages=[message]), [0.1] * 4096)
    db._conn.execute("DROP INDEX idx_messages_effective")
    db._conn.execute("CREATE INDEX idx_messages_effective ON messages(effective_at)")
    db._conn.execute("DROP TABLE IF EXISTS authority_rule_state")
    db._conn.execute("UPDATE schema_version SET version = 21")
    db._conn.commit()
    tables = ("messages", "message_thread_map", "threads", "threads_vec", "indexing_jobs")
    before = {
        table: [tuple(r) for r in db._conn.execute(f"SELECT * FROM {table}")] for table in tables
    }
    db.close()
    migrated = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh-effective.db", account=TEST_ACCOUNT)
    try:
        assert (
            migrated._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        for table, rows in before.items():
            assert [tuple(r) for r in migrated._conn.execute(f"SELECT * FROM {table}")] == rows
        for conn in (migrated._conn, fresh._conn):
            assert [r[2] for r in conn.execute("PRAGMA index_info(idx_messages_effective)")] == [
                "effective_at",
                "claimant_id",
            ]
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
    finally:
        migrated.close()
        fresh.close()


def test_effective_clock_index_migration_failure_is_atomic(tmp_path):
    db = Database(tmp_path / "effective-failure.db", account=TEST_ACCOUNT)
    db._conn.execute("DROP INDEX idx_messages_effective")
    db._conn.execute("CREATE INDEX idx_messages_effective ON messages(effective_at)")
    db._conn.execute("DROP TABLE IF EXISTS authority_rule_state")
    db._conn.execute("UPDATE schema_version SET version = 21")
    db._conn.commit()
    db._conn.set_authorizer(
        lambda action, *args: (
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK
        )
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db._migrate()
    finally:
        db._conn.set_authorizer(None)
    try:
        assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 21
        assert [r[2] for r in db._conn.execute("PRAGMA index_info(idx_messages_effective)")] == [
            "effective_at"
        ]
    finally:
        db.close()

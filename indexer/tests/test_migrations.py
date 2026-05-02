"""
Tests for src/migrations/runner.py.

Covers discovery, ordered apply, idempotency, atomicity per migration,
and rejection of malformed migration sets (bad filenames, gaps,
duplicates).
"""

import sqlite3
from pathlib import Path

import pytest
from src.migrations import runner


def _make_versioned_db(tmp_path: Path, version: int) -> sqlite3.Connection:
    """Build a fresh on-disk SQLite DB with a stamped schema_version row."""
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
    conn.commit()
    return conn


def _stored_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT version FROM schema_version").fetchone()["version"]


def _write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# discover_migrations
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_empty_directory_returns_empty_list(self, tmp_path):
        assert runner.discover_migrations(tmp_path) == []

    def test_returns_sorted_by_version(self, tmp_path):
        _write(tmp_path, "0014_b.sql", "")
        _write(tmp_path, "0013_a.sql", "")
        _write(tmp_path, "0015_c.sql", "")
        result = runner.discover_migrations(tmp_path)
        assert [v for v, _ in result] == [13, 14, 15]

    def test_rejects_invalid_filename(self, tmp_path):
        _write(tmp_path, "0013_ok.sql", "")
        _write(tmp_path, "not_a_migration.sql", "")
        with pytest.raises(RuntimeError, match="invalid migration filename"):
            runner.discover_migrations(tmp_path)

    def test_rejects_duplicate_version(self, tmp_path):
        _write(tmp_path, "0013_a.sql", "")
        _write(tmp_path, "0013_b.sql", "")
        with pytest.raises(RuntimeError, match="duplicate migration version"):
            runner.discover_migrations(tmp_path)

    def test_rejects_zero_version(self, tmp_path):
        _write(tmp_path, "0000_x.sql", "")
        with pytest.raises(RuntimeError, match="invalid migration version"):
            runner.discover_migrations(tmp_path)


# ---------------------------------------------------------------------------
# apply_pending
# ---------------------------------------------------------------------------


class TestApplyPending:
    def test_no_op_when_current_equals_target(self, tmp_path):
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=12, migration_dir=tmp_path
        )
        assert applied == []
        assert _stored_version(conn) == 12

    def test_no_op_when_current_above_target(self, tmp_path):
        # Defense in depth — caller (Database._migrate) is expected to
        # reject downgrades, but the runner stays lenient: no migration
        # files apply, so nothing happens. The stored version is the
        # caller's problem, not ours.
        conn = _make_versioned_db(tmp_path, 13)
        applied = runner.apply_pending(
            conn, current_version=13, target_version=12, migration_dir=tmp_path
        )
        assert applied == []
        assert _stored_version(conn) == 13

    def test_single_migration_advances_version(self, tmp_path):
        _write(tmp_path, "0013_add_widget.sql", "CREATE TABLE widget (id INTEGER);")
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=13, migration_dir=tmp_path
        )
        assert applied == [13]
        assert _stored_version(conn) == 13
        # The DDL really ran.
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='widget'").fetchone()
            is not None
        )

    def test_multiple_migrations_apply_in_order(self, tmp_path):
        _write(tmp_path, "0013_a.sql", "CREATE TABLE a (x INTEGER);")
        _write(tmp_path, "0014_b.sql", "CREATE TABLE b (x INTEGER);")
        _write(tmp_path, "0015_c.sql", "CREATE TABLE c (x INTEGER);")
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=15, migration_dir=tmp_path
        )
        assert applied == [13, 14, 15]
        assert _stored_version(conn) == 15

    def test_skips_already_applied_versions(self, tmp_path):
        # Files exist for 13, 14, 15; only 15 should apply if stored is 14.
        _write(tmp_path, "0013_a.sql", "CREATE TABLE a (x INTEGER);")
        _write(tmp_path, "0014_b.sql", "CREATE TABLE b (x INTEGER);")
        _write(tmp_path, "0015_c.sql", "CREATE TABLE c (x INTEGER);")
        conn = _make_versioned_db(tmp_path, 14)
        applied = runner.apply_pending(
            conn, current_version=14, target_version=15, migration_dir=tmp_path
        )
        assert applied == [15]
        assert _stored_version(conn) == 15
        # 13 and 14 must NOT have been re-run on top.
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='b'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='c'").fetchone() is not None

    def test_rejects_gap_in_pending_sequence(self, tmp_path):
        # current=12, target=15, but only 13 and 15 exist — 14 missing.
        _write(tmp_path, "0013_a.sql", "CREATE TABLE a (x INTEGER);")
        _write(tmp_path, "0015_c.sql", "CREATE TABLE c (x INTEGER);")
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(RuntimeError, match="migration sequence"):
            runner.apply_pending(
                conn, current_version=12, target_version=15, migration_dir=tmp_path
            )
        # Nothing should have been applied — the check runs before the loop.
        assert _stored_version(conn) == 12
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is None

    def test_failing_migration_rolls_back_that_migration_only(self, tmp_path):
        # 13 succeeds, 14 fails. After the call, version should be 13 and
        # table_a should exist (committed by 13's transaction); table_b
        # should NOT exist (rolled back by 14's transaction).
        _write(tmp_path, "0013_a.sql", "CREATE TABLE a (x INTEGER);")
        _write(
            tmp_path,
            "0014_broken.sql",
            "CREATE TABLE b (x INTEGER); SELECT * FROM nonexistent_table;",
        )
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(sqlite3.Error):
            runner.apply_pending(
                conn, current_version=12, target_version=14, migration_dir=tmp_path
            )
        assert _stored_version(conn) == 13
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone() is not None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='b'").fetchone() is None

    def test_target_below_available_files_only_applies_through_target(self, tmp_path):
        # Files exist for 13, 14, 15; target is 14, so 15 must be left
        # alone even though its file is present.
        _write(tmp_path, "0013_a.sql", "CREATE TABLE a (x INTEGER);")
        _write(tmp_path, "0014_b.sql", "CREATE TABLE b (x INTEGER);")
        _write(tmp_path, "0015_c.sql", "CREATE TABLE c (x INTEGER);")
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=14, migration_dir=tmp_path
        )
        assert applied == [13, 14]
        assert _stored_version(conn) == 14
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='c'").fetchone() is None

    def test_target_above_available_files_raises_with_clear_message(self, tmp_path):
        # current=12, target=15, but no files at all — caller bumped the
        # constant without shipping a migration.
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(RuntimeError, match="migration sequence"):
            runner.apply_pending(
                conn, current_version=12, target_version=15, migration_dir=tmp_path
            )


# ---------------------------------------------------------------------------
# _split_statements — the SQL splitter is load-bearing for atomicity, so
# it gets its own coverage. The interesting cases are the ones where a
# naive ``sql.split(';')`` would do the wrong thing.
# ---------------------------------------------------------------------------


class TestSplitStatements:
    def test_empty_input_returns_empty(self):
        assert runner._split_statements("") == []
        assert runner._split_statements("   \n\n  ") == []

    def test_single_statement_no_terminator(self):
        assert runner._split_statements("CREATE TABLE a (x)") == ["CREATE TABLE a (x)"]

    def test_multiple_statements_split_on_semicolon(self):
        sql = "CREATE TABLE a (x); CREATE TABLE b (y);"
        assert runner._split_statements(sql) == ["CREATE TABLE a (x)", "CREATE TABLE b (y)"]

    def test_line_comment_skipped(self):
        sql = "-- this is a comment\nCREATE TABLE a (x);"
        assert runner._split_statements(sql) == ["CREATE TABLE a (x)"]

    def test_inline_line_comment_after_statement(self):
        sql = "CREATE TABLE a (x); -- trailing comment\nCREATE TABLE b (y);"
        assert runner._split_statements(sql) == ["CREATE TABLE a (x)", "CREATE TABLE b (y)"]

    def test_string_literal_with_semicolon_kept_intact(self):
        # A naive ``split(';')`` would shred this into two broken halves.
        sql = "INSERT INTO t VALUES ('hello;world'); INSERT INTO t VALUES ('next');"
        assert runner._split_statements(sql) == [
            "INSERT INTO t VALUES ('hello;world')",
            "INSERT INTO t VALUES ('next')",
        ]

    def test_string_literal_with_escaped_quote(self):
        # SQLite's standard ``''`` escape inside a single-quoted string.
        sql = "INSERT INTO t VALUES ('it''s fine'); CREATE TABLE x (y);"
        assert runner._split_statements(sql) == [
            "INSERT INTO t VALUES ('it''s fine')",
            "CREATE TABLE x (y)",
        ]

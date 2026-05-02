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

    def test_empty_migration_file_rejected_before_version_bump(self, tmp_path):
        # A whitespace-only migration produces no statements at all.
        # Without this guard the runner would still UPDATE
        # schema_version, leaving the DB stamped at the new version
        # with none of the expected DDL applied.
        _write(tmp_path, "0013_empty.sql", "   \n\n  ")
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(RuntimeError, match="no executable SQL"):
            runner.apply_pending(
                conn, current_version=12, target_version=13, migration_dir=tmp_path
            )
        assert _stored_version(conn) == 12

    def test_line_comment_only_migration_rejected(self, tmp_path):
        # sqlite3 accepts a comment-only statement as a no-op, so
        # without ``_has_executable_sql`` the runner would happily
        # execute it and advance schema_version. The guard catches it.
        _write(tmp_path, "0013_note.sql", "-- just a note about the upcoming change\n")
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(RuntimeError, match="no executable SQL"):
            runner.apply_pending(
                conn, current_version=12, target_version=13, migration_dir=tmp_path
            )
        assert _stored_version(conn) == 12

    def test_block_comment_only_migration_rejected(self, tmp_path):
        # Same risk for ``/* … */`` block comments.
        _write(tmp_path, "0013_block.sql", "/* TODO: real migration goes here */\n")
        conn = _make_versioned_db(tmp_path, 12)
        with pytest.raises(RuntimeError, match="no executable SQL"):
            runner.apply_pending(
                conn, current_version=12, target_version=13, migration_dir=tmp_path
            )
        assert _stored_version(conn) == 12

    def test_mixed_comments_and_ddl_applies(self, tmp_path):
        # A comment header followed by real DDL is the normal case and
        # must still apply cleanly. Confirms the guard discriminates
        # comment-only from comment-prefixed.
        _write(
            tmp_path,
            "0013_with_header.sql",
            "-- Add a widget table for retrieval testing.\n"
            "/* Schema reviewed 2026-05-02. */\n"
            "CREATE TABLE widget (id INTEGER);\n",
        )
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=13, migration_dir=tmp_path
        )
        assert applied == [13]
        assert _stored_version(conn) == 13
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'widget'").fetchone()
            is not None
        )

    def test_compound_trigger_migration_applies_atomically(self, tmp_path):
        # End-to-end coverage for a CREATE TRIGGER … BEGIN … END;
        # compound. A naive split-on-semicolon would shred the trigger
        # body into invalid fragments and the migration would fail.
        # With ``sqlite3.complete_statement`` the trigger and the
        # follow-on CREATE TABLE both apply cleanly inside the same
        # per-migration transaction.
        _write(
            tmp_path,
            "0013_trigger.sql",
            (
                "CREATE TABLE log (id INTEGER);\n"
                "CREATE TABLE t (id INTEGER);\n"
                "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
                "  INSERT INTO log (id) VALUES (NEW.id);\n"
                "END;\n"
            ),
        )
        conn = _make_versioned_db(tmp_path, 12)
        applied = runner.apply_pending(
            conn, current_version=12, target_version=13, migration_dir=tmp_path
        )
        assert applied == [13]
        assert _stored_version(conn) == 13
        # All three objects exist; the trigger fires on insert.
        for name in ("log", "t", "trg"):
            assert (
                conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (name,)).fetchone()
                is not None
            )
        conn.execute("INSERT INTO t (id) VALUES (42)")
        conn.commit()
        assert conn.execute("SELECT id FROM log").fetchone()["id"] == 42


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
        # ``sqlite3.complete_statement`` keeps the trailing ``;`` as
        # part of each statement boundary; sqlite3 accepts statements
        # with or without it on ``execute``, so the test asserts the
        # boundary count and content rather than exact stripping.
        sql = "CREATE TABLE a (x); CREATE TABLE b (y);"
        statements = runner._split_statements(sql)
        assert len(statements) == 2
        assert statements[0].rstrip(";").strip() == "CREATE TABLE a (x)"
        assert statements[1].rstrip(";").strip() == "CREATE TABLE b (y)"

    def test_line_comment_kept_with_following_statement(self):
        # ``sqlite3.complete_statement`` treats the leading comment as
        # part of the statement that follows it; sqlite3 is happy to
        # execute that as a single statement (the comment is a no-op).
        # We do not strip comments — they may carry useful migration
        # provenance to anyone reading the journal.
        sql = "-- this is a comment\nCREATE TABLE a (x);"
        statements = runner._split_statements(sql)
        assert len(statements) == 1
        assert "CREATE TABLE a (x)" in statements[0]
        assert "-- this is a comment" in statements[0]

    def test_inline_line_comment_after_statement(self):
        # The trailing comment between two statements stays attached to
        # whichever statement it is closer to per sqlite3's parser.
        sql = "CREATE TABLE a (x); -- trailing comment\nCREATE TABLE b (y);"
        statements = runner._split_statements(sql)
        assert len(statements) == 2
        assert "CREATE TABLE a (x)" in statements[0]
        assert "CREATE TABLE b (y)" in statements[1]

    def test_compound_create_trigger_stays_one_statement(self):
        # A naive ``split(';')`` shreds a CREATE TRIGGER body — the
        # ``BEGIN`` block contains its own ``;`` terminators that are
        # part of the compound statement, not boundaries between
        # statements. ``sqlite3.complete_statement`` only returns True
        # at the final ``END;``, so the splitter emits the whole
        # trigger as a single statement.
        sql = (
            "CREATE TRIGGER trg AFTER INSERT ON t BEGIN\n"
            "  INSERT INTO log (id) VALUES (NEW.id);\n"
            "  UPDATE counters SET n = n + 1;\n"
            "END;\n"
            "CREATE TABLE u (x);"
        )
        statements = runner._split_statements(sql)
        assert len(statements) == 2
        assert statements[0].startswith("CREATE TRIGGER trg")
        assert statements[0].rstrip(";").endswith("END")
        assert "CREATE TABLE u (x)" in statements[1]

    def test_block_comment_kept_with_following_statement(self):
        # ``/* … */`` block comments are recognized by
        # ``sqlite3.complete_statement`` — the parser treats them as
        # whitespace, so a block comment never falsely closes a
        # statement boundary even when it contains a ``;``.
        sql = "/* setup;\n   note */\nCREATE TABLE a (x);"
        statements = runner._split_statements(sql)
        assert len(statements) == 1
        assert "CREATE TABLE a (x)" in statements[0]


# ---------------------------------------------------------------------------
# _has_executable_sql — drives the "comment-only migration is not enough to
# advance schema_version" guard. Pin the qualitative behavior so a future
# refactor of comment-stripping can't silently re-open the bug.
# ---------------------------------------------------------------------------


class TestHasExecutableSql:
    def test_pure_ddl_is_executable(self):
        assert runner._has_executable_sql("CREATE TABLE a (x)") is True
        assert runner._has_executable_sql("CREATE TABLE a (x);") is True

    def test_comment_prefixed_ddl_is_executable(self):
        assert runner._has_executable_sql("-- header\nCREATE TABLE a (x);") is True

    def test_line_comment_only_is_not_executable(self):
        assert runner._has_executable_sql("-- just a note") is False
        assert runner._has_executable_sql("-- a;\n") is False

    def test_block_comment_only_is_not_executable(self):
        assert runner._has_executable_sql("/* TODO */") is False
        assert runner._has_executable_sql("/* multi\n line\n note */") is False

    def test_mixed_comment_only_is_not_executable(self):
        # Both styles, no DDL — still not executable.
        assert runner._has_executable_sql("-- one\n/* two */\n-- three\n") is False

    def test_whitespace_and_semicolons_are_not_executable(self):
        assert runner._has_executable_sql("") is False
        assert runner._has_executable_sql("   \n\n  ") is False
        assert runner._has_executable_sql(";") is False
        assert runner._has_executable_sql(" ; ; ") is False

    def test_string_literal_with_semicolon_kept_intact(self):
        # A naive ``split(';')`` would shred this into two broken halves.
        sql = "INSERT INTO t VALUES ('hello;world'); INSERT INTO t VALUES ('next');"
        statements = runner._split_statements(sql)
        assert len(statements) == 2
        assert "'hello;world'" in statements[0]
        assert "'next'" in statements[1]

    def test_string_literal_with_escaped_quote(self):
        # SQLite's standard ``''`` escape inside a single-quoted string.
        sql = "INSERT INTO t VALUES ('it''s fine'); CREATE TABLE x (y);"
        statements = runner._split_statements(sql)
        assert len(statements) == 2
        assert "'it''s fine'" in statements[0]
        assert "CREATE TABLE x (y)" in statements[1]

"""#887: the indexer logs one INFO line at startup naming the source
commit, a random boot ID, the code's and the stored schema version and a
hash of the non-secret settings, so log lines from before and after a
rebuild or restart can be told apart.

The line is built only from inputs that cannot fail (raw environment
strings and a read-only, never-raising schema read) and is logged when
``src.main`` is imported, before any setting is parsed, so a malformed
setting, a refused index or a failed migration still leaves it in the
log (Codex review rounds 3 and 4 on #893)."""

import logging
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from src import main
from src.database import SCHEMA_APPLICATION_ID, SCHEMA_VERSION, Database

_LINE = re.compile(
    r"Startup identity: service=indexer commit=(?P<commit>\S+) "
    r"boot=(?P<boot>[0-9a-f]{12}) schema_code=(?P<code>\d+) "
    r"schema_stored=(?P<stored>\d+|none|unreadable) config=(?P<config>[0-9a-f]{12})"
)
_SECRET_MARKER = "synthetic-secret-marker-887"  # pragma: allowlist secret
_SERVICE_DIR = Path(__file__).resolve().parents[1]
_CONFIG_PREFIXES = ("EMBED_", "INDEXER_", "INITIAL_INDEX_", "GIT_COMMIT")


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(caplog) -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main._log_startup_identity()
    [record] = _identity_records(caplog)
    assert record.levelno == logging.INFO
    match = _LINE.fullmatch(record.getMessage())
    assert match, record.getMessage()
    return match


def _stamp(path, version: int, application_id: int = SCHEMA_APPLICATION_ID) -> None:
    """A database file carrying only the schema stamp."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (?)", (version,))
        conn.execute(f"PRAGMA application_id = {application_id}")
        conn.commit()


def test_line_format_before_and_after_the_index_exists(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GIT_COMMIT", "abc1234-dirty")
    path = tmp_path / "mail.db"
    monkeypatch.setenv("SQLITE_PATH", str(path))
    fresh = _log(caplog)
    assert fresh["commit"] == "abc1234-dirty"
    assert fresh["code"] == str(SCHEMA_VERSION)
    # No index yet: nothing is stored.
    assert fresh["stored"] == "none"
    Database(path).close()
    reopened = _log(caplog)
    assert reopened["stored"] == str(SCHEMA_VERSION)
    # Each start gets its own boot ID; the settings did not change.
    assert reopened["boot"] != fresh["boot"]
    assert reopened["config"] == fresh["config"]


@pytest.mark.parametrize("value", [None, "", "  ", "abc 123", "abc\nforged line", "x" * 65])
def test_missing_or_unusable_commit_is_logged_as_unknown(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("GIT_COMMIT", value)
    assert _log(caplog)["commit"] == "unknown"


class TestReadStoredSchemaVersion:
    """The stored version is read read-only and never raises: the line
    is logged before ``Database`` opens and migrates the file."""

    def test_missing_file_is_none_and_is_not_created(self, tmp_path):
        path = tmp_path / "mail.db"
        assert main._read_stored_schema_version(path) == "none"
        assert not path.exists()

    def test_file_without_the_stamp_is_none(self, tmp_path):
        path = tmp_path / "mail.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE other (x INTEGER)")
        assert main._read_stored_schema_version(path) == "none"

    def test_a_newer_version_is_read_and_left_alone(self, tmp_path):
        path = tmp_path / "mail.db"
        _stamp(path, SCHEMA_VERSION + 1)
        before = path.read_bytes()
        assert main._read_stored_schema_version(path) == str(SCHEMA_VERSION + 1)
        assert path.read_bytes() == before

    def test_a_file_sqlite_cannot_read_is_unreadable(self, tmp_path):
        path = tmp_path / "mail.db"
        path.write_bytes(b"not a database" * 100)
        assert main._read_stored_schema_version(path) == "unreadable"

    def test_a_directory_is_unreadable(self, tmp_path):
        assert main._read_stored_schema_version(tmp_path) == "unreadable"


def test_config_hash_covers_only_the_named_non_secret_settings():
    names = set(main._identity_settings())
    assert names == {
        "MAILDIR_PATH",
        "SQLITE_PATH",
        "EMBED_MODE",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
        "EMBED_BATCH_SIZE",
        "EMBED_CONCURRENCY",
        "EMBED_WARMUP_TIMEOUT_SECS",
        "INDEXER_PARSE_MAX_BYTES",
        "INDEXER_CHUNK_TARGET_TOKENS",
        "INDEXER_CHUNK_MAX_TOKENS",
        "INDEXER_CHUNK_OVERLAP_TOKENS",
        "INITIAL_INDEX_BATCH_SIZE",
        "INDEXER_STEADY_STATE_BATCH_SIZE",
        "INDEXER_WAL_CHECKPOINT_INTERVAL_SECS",
        "INDEXER_RECOVERY_SWEEP_INTERVAL_SECS",
        "INDEXER_MESSAGE_TIMEOUT_SECONDS",
        "INDEXER_ATTACHMENT_EXTRACTION_ENABLED",
        "INDEXER_OCR_ENABLED",
        "INDEXER_ATTACHMENT_MAX_BYTES",
        "INDEXER_OCR_MAX_PAGES",
        "INDEXER_OCR_TIMEOUT_SECONDS",
        "INDEXER_PDF_MAX_DIGITAL_PAGES",
        "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS",
        "INDEXER_MAX_ATTEMPTS",
        "INDEXER_RETRY_BASE_SECONDS",
        "INDEXER_DELETION_ENABLED",
        "INDEXER_DELETION_GRACE_DAYS",
        "INDEXER_DELETION_SWEEP_INTERVAL_SECS",
        "INDEXER_DELETION_MAX_BATCH_PCT",
        "INDEXER_DELETION_FORCE",
    }
    assert not [n for n in names if re.search(r"KEY|TOKEN\b|TOKEN$|PASS|SECRET", n)]


def test_changing_the_api_key_does_not_change_the_hash(monkeypatch, caplog):
    monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-a")
    before = _log(caplog)["config"]
    monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-b")
    after = _log(caplog)["config"]
    assert _SECRET_MARKER not in caplog.text
    # A named setting does change it, so the hash is not a constant.
    monkeypatch.setenv("EMBED_MODEL", "synthetic-other-model")
    changed = _log(caplog)["config"]
    assert before == after
    assert changed != before


@pytest.mark.parametrize(
    ("name", "value"),
    [
        # 0 disables the per-message parse cap.
        ("INDEXER_PARSE_MAX_BYTES", "0"),
        ("EMBED_WARMUP_TIMEOUT_SECS", "30"),
        ("MAILDIR_PATH", "/synthetic/maildir"),
        ("SQLITE_PATH", "/synthetic/mail.db"),
        # A malformed value is hashed as given, never parsed.
        ("INDEXER_MAX_ATTEMPTS", "oops"),
    ],
)
def test_each_raw_value_changes_the_hash(monkeypatch, name, value):
    monkeypatch.delenv(name, raising=False)
    before = main._identity_settings()
    monkeypatch.setenv(name, value)
    after = main._identity_settings()
    assert after[name] == value
    assert main._config_hash(before) != main._config_hash(after)


def _run_indexer(tmp_path, args: list[str], env: dict[str, str]):
    """The indexer in a child process with only the given settings, so
    a malformed one fails it the way it fails the container."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(_CONFIG_PREFIXES)}
    base.update(
        {
            "GIT_COMMIT": "abc1234",
            "SQLITE_PATH": str(tmp_path / "mail.db"),
            "MAILDIR_PATH": str(tmp_path / "maildir"),
            "INDEXER_HEALTH_FILE": str(tmp_path / "health"),
            "EMBED_BASE_URL": "http://127.0.0.1:9/v1",
            "EMBED_MODEL": "synthetic-embed",
            "EMBED_API_KEY": _SECRET_MARKER,
            **env,
        }
    )
    return subprocess.run(
        [sys.executable, *args],
        cwd=_SERVICE_DIR,
        env=base,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_identity_logged_before_failure(result) -> re.Match[str]:
    assert result.returncode != 0
    lines = [line for line in result.stderr.splitlines() if "Startup identity:" in line]
    assert len(lines) == 1, result.stderr
    match = _LINE.search(lines[0])
    assert match, lines[0]
    assert result.stderr.index("Startup identity:") < result.stderr.index("Traceback")
    assert _SECRET_MARKER not in result.stderr
    return match


@pytest.mark.parametrize(
    ("args", "env", "cause"),
    [
        # Parsed when the module is imported.
        (["-c", "import src.main"], {"EMBED_BATCH_SIZE": "oops"}, "EMBED_BATCH_SIZE"),
        # Parsed inside main().
        (["-m", "src.main"], {"INDEXER_MAX_ATTEMPTS": "oops"}, "INDEXER_MAX_ATTEMPTS"),
    ],
)
def test_a_malformed_setting_still_logs_the_identity_first(tmp_path, args, env, cause):
    result = _run_indexer(tmp_path, args, env)
    _assert_identity_logged_before_failure(result)
    assert cause in result.stderr


@pytest.mark.parametrize(
    ("version", "application_id"),
    [
        # Newer than the code: a downgrade, refused.
        (SCHEMA_VERSION + 1, SCHEMA_APPLICATION_ID),
        # Predates the application-ID stamp: refused with rebuild steps.
        (SCHEMA_VERSION, 0),
    ],
)
def test_a_refused_index_still_logs_the_identity_first(tmp_path, version, application_id):
    _stamp(tmp_path / "mail.db", version, application_id)
    result = _run_indexer(tmp_path, ["-m", "src.main"], {})
    match = _assert_identity_logged_before_failure(result)
    assert match["stored"] == str(version)
    assert "RuntimeError" in result.stderr

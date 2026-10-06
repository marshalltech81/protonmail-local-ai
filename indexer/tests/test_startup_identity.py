"""#887: the indexer logs one INFO line at startup naming the source
commit, a random boot ID, the code's and the stored schema version and a
hash of the non-secret settings, so log lines from before and after a
rebuild or restart can be told apart."""

import logging
import re
import sqlite3
from contextlib import closing

import pytest
from src import main
from src.database import SCHEMA_APPLICATION_ID, SCHEMA_VERSION, Database
from src.reconciler import ReconcilerConfig

from tests import test_main

_LINE = re.compile(
    r"Startup identity: service=indexer commit=(?P<commit>\S+) "
    r"boot=(?P<boot>[0-9a-f]{12}) schema_code=(?P<code>\d+) "
    r"schema_stored=(?P<stored>\d+|none|unreadable) config=(?P<config>[0-9a-f]{12})"
)
_SECRET_MARKER = "synthetic-secret-marker-887"  # pragma: allowlist secret
_QUEUE_CFG = {"max_attempts": 5, "base_backoff_seconds": 30}
_RECONCILER_CFG = ReconcilerConfig(
    enabled=True, grace_days=7, sweep_interval_secs=3600, max_batch_pct=0.05, force=False
)


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(path, caplog) -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main._log_startup_identity(main._read_stored_schema_version(path), _QUEUE_CFG, _RECONCILER_CFG)
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
    fresh = _log(path, caplog)
    assert fresh["commit"] == "abc1234-dirty"
    assert fresh["code"] == str(SCHEMA_VERSION)
    # No index yet: nothing is stored.
    assert fresh["stored"] == "none"
    Database(path).close()
    reopened = _log(path, caplog)
    assert reopened["stored"] == str(SCHEMA_VERSION)
    # Each start gets its own boot ID; the settings did not change.
    assert reopened["boot"] != fresh["boot"]
    assert reopened["config"] == fresh["config"]


@pytest.mark.parametrize("value", [None, "", "  ", "abc 123", "abc\nforged line", "x" * 65])
def test_missing_or_unusable_commit_is_logged_as_unknown(tmp_path, monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("GIT_COMMIT", value)
    assert _log(tmp_path / "mail.db", caplog)["commit"] == "unknown"


class TestReadStoredSchemaVersion:
    """Codex review round 3 on #893: the stored version is read read-only
    before ``Database`` opens and migrates the file, so the identity line
    is logged even when that open fails."""

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


@pytest.mark.parametrize(
    ("version", "application_id"),
    [
        # Newer than the code: a downgrade, refused.
        (SCHEMA_VERSION + 1, SCHEMA_APPLICATION_ID),
        # Predates the application-ID stamp: refused with rebuild steps.
        (SCHEMA_VERSION, 0),
    ],
)
def test_identity_is_logged_before_a_refused_open(
    tmp_path, monkeypatch, caplog, version, application_id
):
    path = tmp_path / "mail.db"
    _stamp(path, version, application_id)
    monkeypatch.setattr(main, "SQLITE_PATH", path)
    monkeypatch.setattr(main, "EMBED_BASE_URL", "http://host.docker.internal:8001/v1")
    monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-embed")
    monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError):
        main.main()
    [record] = _identity_records(caplog)
    match = _LINE.fullmatch(record.getMessage())
    assert match
    assert match["stored"] == str(version)
    assert _SECRET_MARKER not in caplog.text


def test_config_hash_covers_only_the_named_non_secret_settings():
    names = set(main._identity_settings(_QUEUE_CFG, _RECONCILER_CFG))
    assert names == {
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


def test_changing_the_api_key_does_not_change_the_hash(tmp_path, monkeypatch, caplog):
    path = tmp_path / "mail.db"
    monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER + "-a")
    monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-a")
    before = _log(path, caplog)["config"]
    monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER + "-b")
    monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-b")
    after = _log(path, caplog)["config"]
    assert _SECRET_MARKER not in caplog.text
    # A named setting does change it, so the hash is not a constant.
    monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-other-model")
    changed = _log(path, caplog)["config"]
    assert before == after
    assert changed != before


@pytest.mark.parametrize(
    ("name", "value", "effective"),
    [
        # 0 disables the per-message parse cap.
        ("INDEXER_PARSE_MAX_BYTES", "0", 0),
        ("EMBED_WARMUP_TIMEOUT_SECS", "30", 30.0),
    ],
)
def test_settings_read_outside_main_change_the_hash(monkeypatch, name, value, effective):
    """Codex review round 1 on #893: the parse cap and the warmup timeout
    are read by ``parser.py`` and ``embedder.py``; the hash takes the
    effective value from the same readers."""
    monkeypatch.delenv(name, raising=False)
    before = main._identity_settings(_QUEUE_CFG, _RECONCILER_CFG)
    monkeypatch.setenv(name, value)
    after = main._identity_settings(_QUEUE_CFG, _RECONCILER_CFG)
    assert after[name] == effective
    assert before[name] != after[name]
    assert main._config_hash(before) != main._config_hash(after)


def test_main_logs_the_identity_line_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER)
    caplog.set_level(logging.DEBUG)
    test_main.TestMainStartupAndLoop()._run_main(tmp_path, monkeypatch, sweep_due=False)
    [record] = _identity_records(caplog)
    assert _LINE.fullmatch(record.getMessage())
    assert _SECRET_MARKER not in caplog.text

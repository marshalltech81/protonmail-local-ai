"""#887: the indexer logs one INFO line at startup naming the source
commit, a random boot ID, the code's and the stored schema version and a
hash of the non-secret settings, so log lines from before and after a
rebuild or restart can be told apart."""

import logging
import re

import pytest
from src import main
from src.database import SCHEMA_VERSION, Database
from src.reconciler import ReconcilerConfig

from tests import test_main

_LINE = re.compile(
    r"Startup identity: service=indexer commit=(?P<commit>\S+) "
    r"boot=(?P<boot>[0-9a-f]{12}) schema_code=(?P<code>\d+) "
    r"schema_stored=(?P<stored>\d+|none) config=(?P<config>[0-9a-f]{12})"
)
_SECRET_MARKER = "synthetic-secret-marker-887"  # pragma: allowlist secret
_QUEUE_CFG = {"max_attempts": 5, "base_backoff_seconds": 30}
_RECONCILER_CFG = ReconcilerConfig(
    enabled=True, grace_days=7, sweep_interval_secs=3600, max_batch_pct=0.05, force=False
)


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(db: Database, caplog) -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main._log_startup_identity(db, _QUEUE_CFG, _RECONCILER_CFG)
    [record] = _identity_records(caplog)
    assert record.levelno == logging.INFO
    match = _LINE.fullmatch(record.getMessage())
    assert match, record.getMessage()
    return match


def test_line_format_on_a_fresh_and_a_reopened_index(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GIT_COMMIT", "abc1234-dirty")
    db = Database(tmp_path / "mail.db")
    try:
        fresh = _log(db, caplog)
    finally:
        db.close()
    assert fresh["commit"] == "abc1234-dirty"
    assert fresh["code"] == str(SCHEMA_VERSION)
    # A new index had no stored version when the indexer opened it.
    assert fresh["stored"] == "none"
    db = Database(tmp_path / "mail.db")
    try:
        reopened = _log(db, caplog)
    finally:
        db.close()
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
    db = Database(tmp_path / "mail.db")
    try:
        assert _log(db, caplog)["commit"] == "unknown"
    finally:
        db.close()


def test_config_hash_covers_only_the_named_non_secret_settings():
    names = set(main._identity_settings(_QUEUE_CFG, _RECONCILER_CFG))
    assert names == {
        "EMBED_MODE",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
        "EMBED_BATCH_SIZE",
        "EMBED_CONCURRENCY",
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
    db = Database(tmp_path / "mail.db")
    try:
        monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER + "-a")
        monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-a")
        before = _log(db, caplog)["config"]
        monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER + "-b")
        monkeypatch.setenv("EMBED_API_KEY", _SECRET_MARKER + "-b")
        after = _log(db, caplog)["config"]
        assert _SECRET_MARKER not in caplog.text
        # A named setting does change it, so the hash is not a constant.
        monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-other-model")
        changed = _log(db, caplog)["config"]
    finally:
        db.close()
    assert before == after
    assert changed != before


def test_main_logs_the_identity_line_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(main, "EMBED_API_KEY", _SECRET_MARKER)
    caplog.set_level(logging.DEBUG)
    test_main.TestMainStartupAndLoop()._run_main(tmp_path, monkeypatch, sweep_due=False)
    [record] = _identity_records(caplog)
    assert _LINE.fullmatch(record.getMessage())
    assert _SECRET_MARKER not in caplog.text

"""Tests for src/force_deletion.py, the one-run ``make force-deletion``
command (#1609): one deletion-reconciliation pass with the mass-delete
brake off, after which the indexer's own passes keep the brake.

Real SQLite and real .eml files in tmp_path; the embedder is a mock, so no
embedding service is needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from src import force_deletion
from src import main as indexer_main
from src.account import account_config_from_env
from src.database import EMBEDDING_DIM, Database
from src.reconciler import Reconciler, load_config_from_env
from src.threader import Threader

from tests.conftest import TEST_ACCOUNT, count_pending_deletions, make_mock_embedder
from tests.test_reconciler import _index, _write_eml

# Mail content the command must never log.
MARKER = "zq9forcemarker"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The indexer's environment as Compose passes it, with the paths in
    tmp_path. Grace 0 so the staged tombstones are due at once; the brake
    at its 5% default (with the 10-tombstone floor)."""
    maildir = tmp_path / "maildir"
    (maildir / "INBOX" / "cur").mkdir(parents=True)
    values = {
        "ACCOUNT_SCOPE": TEST_ACCOUNT.scope_uuid,
        "INDEXER_DELETION_GRACE_DAYS": "0",
        "INDEXER_DELETION_MAX_BATCH_PCT": "0.05",
        # The removed setting, set the wrong way: it must change nothing.
        "INDEXER_DELETION_FORCE": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("INDEXER_DELETION_ENABLED", raising=False)
    monkeypatch.setattr(indexer_main, "SQLITE_PATH", tmp_path / "mail.db")
    monkeypatch.setattr(indexer_main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(indexer_main, "_validate_embed_config", lambda: "http://embed.test/v1")
    embedder = make_mock_embedder([0.1] * EMBEDDING_DIM)
    embedder.base_url = "http://embed.test/v1"
    monkeypatch.setattr(indexer_main, "OpenAIEmbedder", lambda **_kw: embedder)
    return maildir


def _open(maildir: Path, tmp_path: Path) -> Database:
    """The index as the indexer left it, its embedder recorded."""
    account = account_config_from_env(os.environ, maildir_root=maildir)
    db = Database(tmp_path / "mail.db", account=account)
    indexer_main._check_embedder_identity(db, indexer_main.OpenAIEmbedder())
    return db


def _stage(maildir: Path, db: Database, start: int, count: int) -> list[Path]:
    threader = Threader(db)
    paths = []
    for i in range(start, start + count):
        path = maildir / "INBOX" / "cur" / f"1700000{i:04d}.M1.host:2,S"
        _write_eml(path, f"force{i}@example.com", subject=f"{MARKER} {i}", body=f"{MARKER} body")
        _index(path, db, threader)
        paths.append(path)
    return paths


def _trash(paths: list[Path]) -> None:
    for path in paths:
        path.rename(path.with_name(path.name + "T"))


def _normal_pass(db: Database, maildir: Path) -> dict:
    """One pass of the indexer's own reconciler, configured from the
    environment as main() configures it."""
    reconciler = Reconciler(
        db, make_mock_embedder(), load_config_from_env(os.environ), maildir_root=maildir
    )
    reconciler.sweep()
    return reconciler.reap()


def test_a_forced_pass_reaps_past_the_brake_exactly_once(env, tmp_path, caplog):
    db = _open(env, tmp_path)
    paths = _stage(env, db, 0, 60)
    _trash(paths[:20])
    # The service's pass: 20 tombstones exceed max(10, 5% of 60), so the
    # brake holds them.
    assert _normal_pass(db, env)["aborted"] is True
    assert db.count_total_messages() == 60
    db.close()

    caplog.set_level(logging.INFO)
    assert force_deletion.main([]) == 0

    db = _open(env, tmp_path)
    assert db.count_total_messages() == 40
    assert count_pending_deletions(db) == 0
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "brake off" in r.getMessage()
    ]
    assert len(warnings) == 1
    passes = [
        r.getMessage() for r in caplog.records if "maintenance pass=reconcile" in r.getMessage()
    ]
    assert len(passes) == 1
    assert "threads_reaped=20" in passes[0]
    assert passes[0].endswith("brake=forced")
    assert MARKER not in caplog.text

    # Back to normal: the next over-brake batch is held again.
    _trash(paths[20:40])
    result = _normal_pass(db, env)
    assert result["aborted"] is True
    assert db.count_total_messages() == 40
    db.close()


def test_archive_mode_is_refused_without_touching_the_index(env, tmp_path, monkeypatch, caplog):
    db = _open(env, tmp_path)
    paths = _stage(env, db, 0, 30)
    _trash(paths[:20])
    db.close()
    monkeypatch.setenv("INDEXER_DELETION_ENABLED", "false")

    assert force_deletion.main([]) == 1

    assert "archive mode" in caplog.text
    db = _open(env, tmp_path)
    assert db.count_total_messages() == 30
    assert count_pending_deletions(db) == 0
    db.close()


def test_a_missing_index_is_refused_without_creating_one(env, tmp_path, caplog):
    assert force_deletion.main([]) == 1
    assert not (tmp_path / "mail.db").exists()
    assert "No index database" in caplog.text


def test_a_failed_pass_exits_non_zero_with_its_type(env, tmp_path, monkeypatch, caplog):
    db = _open(env, tmp_path)
    _stage(env, db, 0, 12)
    db.close()

    def boom(self):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(Reconciler, "reap", boom)
    assert force_deletion.main([]) == 1
    assert "periodic reconciliation failed: RuntimeError" in caplog.text
    assert MARKER not in caplog.text


def test_an_argument_is_refused():
    with pytest.raises(SystemExit):
        force_deletion.main(["--yes"])

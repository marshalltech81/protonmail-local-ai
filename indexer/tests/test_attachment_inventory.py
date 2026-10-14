"""Current attachment-row evidence commits with the occurrence inventory."""

from email.message import EmailMessage
from pathlib import Path

import pytest
from src import main
from src.database import SCHEMA_VERSION, Database
from src.queue import REPARSE_ENQUEUE_SQL
from src.threader import Threader

from tests.conftest import TEST_ACCOUNT, make_message, make_mock_embedder, make_thread
from tests.test_database import _v14_from_fresh
from tests.test_main import _drain, _make_queue


def _stamp(db, claimant):
    return db._conn.execute(
        "SELECT attachment_rows_current FROM messages WHERE claimant_id=?", (claimant,)
    ).fetchone()[0]


@pytest.mark.parametrize("current", [True, False])
def test_phase1_resets_committed_inventory(db, current):
    msg = make_message(message_id="inventory@test.example")
    thread = make_thread(messages=[msg])
    db.upsert_thread(thread, [0.1] * 4096)
    assert _stamp(db, msg.claimant_id) is None
    db.set_attachment_rows_current(msg.claimant_id, current)
    assert _stamp(db, msg.claimant_id) == int(current)
    db.upsert_thread(thread, [0.1] * 4096)
    assert _stamp(db, msg.claimant_id) is None


def test_inventory_stamp_rolls_back(db):
    msg = make_message(message_id="rollback@test.example")
    db.upsert_thread(make_thread(messages=[msg]), [0.1] * 4096)
    with pytest.raises(RuntimeError), db.transaction():
        db.set_attachment_rows_current(msg.claimant_id, True)
        raise RuntimeError("synthetic failure")
    assert _stamp(db, msg.claimant_id) is None


def test_migration_keeps_old_inventory_unknown_and_queues_reparse(tmp_path):
    path = tmp_path / "inventory.db"
    db = Database(path, account=TEST_ACCOUNT)
    msg = make_message(message_id="old@test.example")
    db.upsert_thread(make_thread(messages=[msg]), [0.1] * 4096)
    _v14_from_fresh(db)
    db.close()
    db = Database(path, account=TEST_ACCOUNT)
    try:
        assert _stamp(db, msg.claimant_id) is None
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
            == 18
        )
        assert (
            db._conn.execute(
                "SELECT reason FROM indexing_jobs WHERE filepath=?", (msg.filepath,)
            ).fetchone()[0]
            == "reparse"
        )
        migration = (
            Path(__file__).parents[1] / "src/migrations/0015_attachment_inventory_current.sql"
        )
        assert REPARSE_ENQUEUE_SQL in migration.read_text()
    finally:
        db.close()


@pytest.mark.parametrize(
    ("enabled", "fail_after_stamp"), [(True, False), (False, False), (True, True)]
)
def test_pipeline_stamps_current_rows_and_disabled_reparse(
    tmp_path, monkeypatch, enabled, fail_after_stamp
):
    maildir = tmp_path / "maildir"
    path = maildir / "INBOX/cur/m.eml"
    path.parent.mkdir(parents=True)
    email = EmailMessage()
    email["Message-ID"] = "<pipeline@test.example>"
    email["From"] = "a@test.example"
    email["Date"] = "Mon, 01 Jan 2024 00:00:00 +0000"
    email.set_content("Synthetic body with enough words for a stable chunk.")
    email.add_attachment(
        b"Synthetic attachment text.", maintype="text", subtype="plain", filename="example.txt"
    )
    path.write_bytes(email.as_bytes())
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(main, "touch_health_file", lambda: None)
    db = Database(tmp_path / "pipeline.db", account=TEST_ACCOUNT)
    try:
        queue = _make_queue(db)
        threader = Threader(db)
        embedder = make_mock_embedder([1.0] + [0.0] * 4095)
        attempted_stamps = []
        if fail_after_stamp:
            store = db.set_attachment_rows_current

            def fail(claimant, current):
                store(claimant, current)
                attempted_stamps.append(current)
                raise RuntimeError("synthetic failure after inventory stamp")

            monkeypatch.setattr(db, "set_attachment_rows_current", fail)
        queue.enqueue(str(path), reason="initial_scan")
        _drain(queue, db, embedder, threader)
        claimant = db._conn.execute("SELECT claimant_id FROM messages").fetchone()[0]
        if fail_after_stamp:
            assert attempted_stamps == [True, True, True]
            assert _stamp(db, claimant) is None
            assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
            return
        assert _stamp(db, claimant) == 1
        rows = db._conn.execute("SELECT attachment_occurrence_id FROM attachments").fetchall()
        assert len(rows) == 1
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", enabled)
        queue.enqueue(str(path), reason="reparse")
        _drain(queue, db, embedder, threader)
        assert _stamp(db, claimant) == int(enabled)
        assert (
            db._conn.execute("SELECT attachment_occurrence_id FROM attachments").fetchall() == rows
        )
    finally:
        db.close()

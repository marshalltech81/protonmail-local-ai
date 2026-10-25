"""Synthetic bounded observed Bcc capture, storage and migration (#1090)."""

from pathlib import Path

import pytest
from src import parser
from src.database import SCHEMA_VERSION
from src.parser import SourceMetadata, parse_email_bytes
from src.queue import REPARSE_ENQUEUE_SQL

from tests.conftest import make_thread
from tests.test_database import FAKE_EMBEDDING


def parsed(headers, folder="Sent"):
    raw = (
        "Message-ID: <synthetic-bcc>\nFrom: Own <own@example.test>\n" + headers + "\nbody"
    ).encode()
    return parse_email_bytes(
        raw, SourceMetadata(folder, frozenset(), len(raw), None, f"/maildir/{folder}/cur/synthetic")
    )


def test_observed_bcc_merges_aliases_and_is_not_logged(caplog):
    msg = parsed(
        "Bcc: First <MARKER@example.test>\nBcc: Second <marker@example.test>, other@example.test\n"
    )
    assert len(msg.bcc_addrs) == 3
    assert msg.bcc_addresses_complete is True
    assert {
        (role, address, name) for role, address, name in msg.participant_names if role == "bcc"
    } == {("bcc", "marker@example.test", "First"), ("bcc", "marker@example.test", "Second")}
    assert "MARKER" not in caplog.text


@pytest.mark.parametrize("header", ["", "Bcc:\n", "Bcc: undisclosed:;\n"])
def test_absent_and_legal_empty_bcc_are_complete_capture_only(header):
    msg = parsed(header, "INBOX")
    assert msg.bcc_addrs == []
    assert msg.bcc_addresses_complete is True
    assert "address_unparsed" not in msg.parse_caps


def test_bcc_spends_shared_budget_last(monkeypatch):
    monkeypatch.setattr(parser, "MAX_MESSAGE_ADDRESSES", 3)
    calls = []
    original = parser._parse_addrs

    def count(value, caps, budget, *, is_bcc=False):
        result = original(value, caps, budget, is_bcc=is_bcc)
        calls.append(budget.parse_calls)
        return result

    monkeypatch.setattr(parser, "_parse_addrs", count)
    msg = parsed("Bcc: secret@example.test\nCc: cc@example.test\nTo: to@example.test\n")
    assert msg.to_addrs == ["to@example.test"] and msg.cc_addrs == ["cc@example.test"]
    assert msg.bcc_addrs == [] and msg.bcc_addresses_complete is False
    assert msg.parse_caps["address_count"] == 1
    assert calls[-1] <= 6


def test_writer_retains_bcc_names_and_capture(db):
    msg = parsed("Bcc: First <marker@example.test>\nBcc: Second <marker@example.test>\n")
    db.upsert_thread(make_thread(messages=[msg], thread_id="synthetic-thread"), FAKE_EMBEDDING)
    assert db._conn.execute("SELECT bcc_addresses_complete FROM messages").fetchone()[0] == 1
    assert [
        tuple(r)
        for r in db._conn.execute("SELECT address,name FROM message_participants WHERE role='bcc'")
    ] == [("marker@example.test", "First")]
    assert {
        r[0]
        for r in db._conn.execute("SELECT name FROM message_participant_names WHERE role='bcc'")
    } == {"First", "Second"}
    assert db._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not db.thread_has_assessed_correspondents(
        "synthetic-thread", ["own@example.test"], ["marker@example.test"]
    )


def test_migration_rebuild_preserves_participant_order_and_names(db):
    msg = parsed("To: z@example.test, a@example.test\n")
    db.upsert_thread(make_thread(messages=[msg], thread_id="migration-thread"), FAKE_EMBEDDING)
    conn = db._conn
    before = [tuple(r) for r in conn.execute("SELECT * FROM message_participants ORDER BY rowid")]
    names = [tuple(r) for r in conn.execute("SELECT * FROM message_participant_names")]
    conn.execute("ALTER TABLE messages DROP COLUMN bcc_addresses_complete")
    sql = (Path(__file__).parents[1] / "src/migrations/0017_bcc_participants.sql").read_text()
    assert sql.rstrip().endswith(REPARSE_ENQUEUE_SQL.rstrip())
    conn.executescript(sql)
    assert [
        tuple(r) for r in conn.execute("SELECT * FROM message_participants ORDER BY rowid")
    ] == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM message_participant_names")] == names
    assert conn.execute("SELECT bcc_addresses_complete FROM messages").fetchone()[0] is None
    conn.execute(
        "INSERT INTO message_participants VALUES (?, 'bcc', 'blind@example.test', NULL)",
        (msg.claimant_id,),
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (
        conn.execute(
            "SELECT reason FROM indexing_jobs WHERE filepath=?", (msg.filepath,)
        ).fetchone()[0]
        == "reparse"
    )
    assert SCHEMA_VERSION == 23


def test_bcc_name_loss_keeps_visible_name_evidence_complete(monkeypatch):
    monkeypatch.setattr(parser, "MAX_EXTRA_PARTICIPANT_NAMES", 0)
    msg = parsed(
        "To: Visible <to@example.test>\nBcc: First <hidden@example.test>, Second <hidden@example.test>\n"
    )
    assert msg.participant_names_complete is True
    assert msg.parse_caps["bcc_participant_names"] == 1


def test_bcc_worst_case_shares_existing_work_ceilings(monkeypatch, caplog):
    import time

    calls = []
    original = parser._parse_addrs

    def measured(value, caps, budget, *, is_bcc=False):
        result = original(value, caps, budget, is_bcc=is_bcc)
        calls.append((budget.parse_calls, budget.addresses, budget.chars, budget.occurrences))
        return result

    monkeypatch.setattr(parser, "_parse_addrs", measured)
    value = ",".join(f"MARKER{i}@example.test" for i in range(1000))
    started = time.perf_counter()
    msg = parsed(("Bcc: " + value + "\n") * 70)
    assert time.perf_counter() - started < 15
    assert len(msg.bcc_addrs) <= parser.MAX_MESSAGE_ADDRESSES - 1
    assert calls[-1][0] <= 2 * parser.MAX_MESSAGE_ADDRESSES
    assert calls[-1][1] <= parser.MAX_MESSAGE_ADDRESSES
    assert calls[-1][2] <= parser._MAX_ADDRESS_TOTAL_CHARS
    assert calls[-1][3] <= parser.MAX_ADDRESS_OCCURRENCES
    assert not msg.bcc_addresses_complete
    assert "MARKER" not in caplog.text


def test_migration_failure_after_parent_drop_rolls_back_and_retries(db, tmp_path):
    import sqlite3

    from src.migrations.runner import apply_pending

    msg = parsed("To: z@example.test, a@example.test\n")
    db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
    conn = db._conn
    conn.execute("ALTER TABLE messages DROP COLUMN bcc_addresses_complete")
    conn.execute("UPDATE schema_version SET version=16")
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM message_participants ORDER BY rowid")]
    directory = tmp_path / "migrations"
    directory.mkdir()
    migration = directory / "0017_bcc_participants.sql"
    original = (Path(__file__).parents[1] / "src/migrations" / migration.name).read_text()
    migration.write_text(
        original.replace(
            "CREATE TABLE message_participants (",
            "SELECT nonexistent();\nCREATE TABLE message_participants (",
        )
    )
    with pytest.raises(sqlite3.OperationalError):
        apply_pending(conn, current_version=16, target_version=17, migration_dir=directory)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 16
    assert [
        tuple(r) for r in conn.execute("SELECT * FROM message_participants ORDER BY rowid")
    ] == before
    assert "bcc_addresses_complete" not in {
        r[1] for r in conn.execute("PRAGMA table_info(messages)")
    }
    migration.write_text(original)
    assert apply_pending(conn, current_version=16, target_version=17, migration_dir=directory) == [
        17
    ]
    conn.execute("DELETE FROM messages WHERE claimant_id=?", (msg.claimant_id,))
    assert conn.execute("SELECT COUNT(*) FROM message_participants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM message_participant_names").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

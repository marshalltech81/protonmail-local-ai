"""Capability coverage uses only synthetic message evidence."""

import json

from src.lib.predicates import LEAVES
from src.lib.sqlite import Database
from src.tools.system import _mailbox_status, _render

from tests.conftest import _insert_message, claimant_of, write_ingestion
from tests.test_sqlite import _open_built_db_conn


def test_every_registered_leaf_has_a_coverage_rule():
    assert all(kind.coverage is not None for kind in LEAVES.values())


def test_backfill_transitions_follow_evidence_even_when_queue_is_empty(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "capabilities.db")
    for i in range(3):
        _insert_message(
            conn,
            message_id=f"m{i}",
            thread_id=f"t{i}",
            sent_at="2024-01-01T00:00:00+00:00",
            subject="SYNTHETIC_PRIVATE_MARKER",
            completeness={"subject_complete": None},
        )
    conn.commit()
    db = Database(str(path))
    report = db.get_mailbox_status()["query_capabilities"]
    assert set(report) == set(LEAVES)
    assert report["subject"]["status"] == "supported after backfill"
    assert report["subject"]["unassessed_messages"] == 3
    write_ingestion(str(path), jobs=(("dead", 3, "parse", "reparse"),))
    conn.execute("UPDATE messages SET subject_complete = 1 WHERE message_id = 'm0'")
    conn.commit()
    status = _mailbox_status(db)
    assert status.queue.reparse == 0
    assert status.query_capabilities["subject"].unassessed_messages == 2
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(status.model_dump(mode="json"))
    assert "subject: supported after backfill" in _render(status)
    conn.execute("UPDATE messages SET subject_complete = 1")
    conn.commit()
    assert db.get_mailbox_status()["query_capabilities"]["subject"]["status"] == "supported"
    conn.execute("UPDATE messages SET subject_complete = 0 WHERE message_id = 'm1'")
    conn.commit()
    report = db.get_mailbox_status()["query_capabilities"]["subject"]
    assert report == {
        "status": "supported when present",
        "available_messages": 2,
        "unassessed_messages": 0,
        "missing_messages": 1,
        "condition": None,
    }
    conn.close()


def test_missing_dates_and_schema_columns_are_not_reported_as_supported(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "missing.db")
    _insert_message(conn, message_id="m", thread_id="t", sent_at=None)
    conn.commit()
    db = Database(str(path))
    report = db.get_mailbox_status()["query_capabilities"]
    assert report["occurred_from"]["status"] == "supported when present"
    assert report["occurred_from"]["available_messages"] == 0
    conn.execute("ALTER TABLE messages DROP COLUMN subject_complete")
    conn.commit()
    report = db.get_mailbox_status()["query_capabilities"]
    assert report["subject"]["status"] == "unavailable"
    assert report["subject"]["condition"]
    conn.close()


def test_attachment_coverage_counts_messages_once_and_distinguishes_unknown(tmp_path):
    from tests.conftest import _insert_attachment

    conn, path = _open_built_db_conn(tmp_path, "attachments.db")
    for i in range(4):
        _insert_message(
            conn,
            message_id=f"a{i}",
            thread_id=f"t{i}",
            sent_at="2024-01-01T00:00:00+00:00",
            has_attachments=i != 3,
        )
    for i, state in enumerate((1, 0, None)):
        for j in range(2):
            _insert_attachment(
                conn,
                message_id=f"a{i}",
                thread_id=f"t{i}",
                attachment_id=f"a{i}-{j}",
                filename="SYNTHETIC_PRIVATE_MARKER",
            )
        conn.execute(
            "UPDATE attachments SET text_complete = ? WHERE claimant_id = ?",
            (state, claimant_of(f"a{i}")),
        )
    conn.commit()
    db = Database(str(path))
    entry = db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]
    assert entry == {
        "status": "supported after backfill",
        "available_messages": 2,
        "unassessed_messages": 1,
        "missing_messages": 1,
        "condition": None,
    }
    conn.execute("UPDATE attachments SET text_complete = 1")
    conn.commit()
    assert (
        db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]["status"]
        == "supported"
    )
    conn.execute("ALTER TABLE messages DROP COLUMN headers_complete")
    conn.commit()
    status = _mailbox_status(db)
    assert status.header_predicates.status == "unavailable"
    assert status.query_capabilities["header_exists"].status == "unavailable"
    assert "SYNTHETIC_PRIVATE_MARKER" not in _render(status)
    conn.close()


def test_schema_version_and_empty_population(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "empty.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (14)")
    conn.commit()
    status = _mailbox_status(Database(str(path)))
    assert status.index_schema_version == 14
    assert all(
        entry.status == "supported" and entry.available_messages == 0
        for entry in status.query_capabilities.values()
    )
    assert "Index schema version: 14" in _render(status)
    conn.close()


def test_dates_sizes_and_address_coverage_follow_stored_provenance(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "provenance.db")
    for i, sent_status in enumerate(("parsed", "missing", None)):
        _insert_message(
            conn,
            message_id=f"p{i}",
            thread_id=f"t{i}",
            sent_at="2024-01-01T00:00:00+00:00",
            sent_at_status=sent_status,
            occurred_at="2024-01-02T00:00:00+00:00" if i == 0 else None,
            sender_ambiguous=i if i < 2 else None,
            completeness={"participant_names_complete": 1},
        )
    conn.execute("UPDATE messages SET size_bytes = NULL WHERE message_id = 'p2'")
    conn.commit()
    report = Database(str(path)).get_mailbox_status()["query_capabilities"]
    for name in ("sent_from", "effective_from", "dated", "sender", "address_is"):
        assert report[name]["available_messages"] == 1
        assert report[name]["unassessed_messages"] == 1
        assert report[name]["missing_messages"] == 1
    assert report["occurred_from"]["status"] == "supported when present"
    assert report["occurred_from"]["missing_messages"] == 2
    assert report["size_min"]["unassessed_messages"] == 1
    assert report["recipient"]["status"] == "supported"
    conn.close()


def test_absent_attachment_schema_does_not_hide_other_capabilities(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "no-attachments.db")
    conn.execute("DROP TABLE attachments")
    conn.commit()
    report = Database(str(path)).get_mailbox_status()["query_capabilities"]
    assert report["attachment_text_words"]["status"] == "unavailable"
    assert report["attachment_text_words"]["available_messages"] is None
    assert report["subject"]["status"] == "supported"
    conn.close()


def test_an_unregistered_status_rule_fails_closed(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest
    from src.lib.query_capabilities import query_capabilities

    conn, _ = _open_built_db_conn(tmp_path, "no-rule.db")
    monkeypatch.setitem(LEAVES, "subject", replace(LEAVES["subject"], coverage=None))
    with pytest.raises(ValueError, match="Registered predicate has no coverage rule"):
        query_capabilities(conn)
    conn.close()

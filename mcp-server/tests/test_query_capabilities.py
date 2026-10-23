"""Capability coverage uses only synthetic message evidence."""

import json

from src.lib.predicates import LEAVES
from src.lib.sqlite import Database
from src.tools.system import _mailbox_status, _render

from tests.conftest import _insert_message, claimant_of, write_ingestion
from tests.test_sqlite import _open_built_db_conn


def test_every_registered_leaf_has_a_coverage_rule():
    assert all(kind.coverage is not None and kind.schema is not None for kind in LEAVES.values())


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
        for name, entry in status.query_capabilities.items()
        if name not in {"direction_is", "list_id_is", "precedence_is"}
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
    for name in ("sent_from", "effective_from", "dated", "sender"):
        assert report[name]["available_messages"] == 1
        assert report[name]["unassessed_messages"] == 1
        assert report[name]["missing_messages"] == 1
    assert report["occurred_from"]["status"] == "supported when present"
    assert report["occurred_from"]["missing_messages"] == 2
    assert report["address_is"]["available_messages"] == 0
    assert report["address_is"]["unassessed_messages"] == 1
    assert report["address_is"]["missing_messages"] == 2
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
    original = LEAVES["subject"]
    monkeypatch.setitem(LEAVES, "subject", replace(original, coverage=None))
    with pytest.raises(ValueError, match="Registered predicate has no coverage rule"):
        query_capabilities(conn)
    monkeypatch.setitem(LEAVES, "subject", replace(original, schema=None))
    with pytest.raises(ValueError, match="Registered predicate has no schema dependency rule"):
        query_capabilities(conn)
    conn.close()


def test_phase_one_attachment_occurrences_are_unassessed_until_committed(tmp_path):
    from tests.conftest import _insert_attachment

    conn, path = _open_built_db_conn(tmp_path, "phase-one.db")
    _insert_message(
        conn,
        message_id="phase-one",
        thread_id="t",
        sent_at="2024-01-01T00:00:00+00:00",
        has_attachments=True,
    )
    conn.commit()
    db = Database(str(path))
    report = db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]
    assert report["status"] == "supported after backfill"
    assert report["unassessed_messages"] == 1
    assert report["missing_messages"] == 0
    _insert_attachment(
        conn, message_id="phase-one", thread_id="t", attachment_id="a", filename="synthetic.txt"
    )
    conn.execute("UPDATE attachments SET text_complete = 0")
    conn.commit()
    assert (
        db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]["status"]
        == "supported when present"
    )
    conn.execute("UPDATE attachments SET text_complete = 1")
    conn.commit()
    assert (
        db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]["status"]
        == "supported"
    )
    conn.close()


def test_match_dependencies_are_required_even_when_coverage_flags_exist(tmp_path):
    from src.lib.query_capabilities import query_capabilities

    conn, _ = _open_built_db_conn(tmp_path, "match-dependencies.db")
    # ``seen`` moved from ``messages`` to ``message_occurrences`` under
    # mailbox occurrences (#1568/#1612/#1627 panel).
    conn.execute("ALTER TABLE message_occurrences RENAME COLUMN seen TO missing_seen")
    conn.execute("DROP TABLE message_headers")
    conn.execute("DROP TABLE message_participants")
    conn.execute("DROP TABLE message_chunks_fts")
    report = query_capabilities(conn)
    for name in ("seen", "header_exists", "sender", "body_words", "attachment_text_words"):
        assert report[name]["status"] == "unavailable"
        assert report[name]["available_messages"] is None
    assert report["subject"]["status"] == "supported"
    conn.close()


def test_schema_rules_cover_actual_compiler_dependencies(tmp_path):
    """Removing any real field must not leave a broken compiler advertised."""
    import sqlite3

    from src.lib.predicates import DATE_BASES, WHERE_ROLES
    from src.lib.query_capabilities import query_capabilities

    conn, _ = _open_built_db_conn(tmp_path, "compiler-dependencies.db")
    from src.lib.list_metadata import sqlite_normalize_fields, with_metadata

    conn.create_function("mcp_list_metadata", 2, sqlite_normalize_fields)
    conn.create_function("mcp_casefold", 1, lambda value: value.casefold() if value else value)
    values = {
        "address": ("synthetic@example.com", "synthetic"),
        "role, address": tuple((role, "synthetic@example.com") for role in WHERE_ROLES),
        "role, text": tuple((role, "synthetic") for role in WHERE_ROLES),
        "role, domain": tuple((role, "example.com") for role in WHERE_ROLES),
        "header name": ("subject",),
        "header name, text": (("subject", "synthetic"),),
        "text": ("synthetic",),
        "words": (("synthetic",),),
        "MIME type": ("application/pdf",),
        "folders": (("INBOX",),),
        "instant": ("2024-01-01T00:00:00+00:00",),
        "basis": tuple(DATE_BASES),
        "bool": (True,),
        "bytes": (1,),
        "class": ("unclassified",),
        "none": (None,),
        "direction": ("inbound", "outbound", "self"),
        "list_id": ("series.example", None),
        "precedence": ("bulk", None),
    }
    tables = (
        "messages",
        "message_participants",
        "message_participant_names",
        "message_headers",
        "message_chunks",
        "attachments",
        "entities",
        # Folder, flag, Trash and Spam terms correlate on one mailbox
        # occurrence and its account (#1568/#1612/#1627 panel).
        "message_occurrences",
        "accounts",
        "operator_identity",
        "operator_addresses",
    )
    for table in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for column in columns:
            conn.execute("SAVEPOINT missing_field")
            conn.execute(f'ALTER TABLE {table} RENAME COLUMN "{column}" TO absent_field')
            report = query_capabilities(conn)
            for name, kind in LEAVES.items():
                for value in values[kind.param]:
                    params = []
                    sql = kind.compile(value, params)
                    try:
                        metadata = (kind.normalized_metadata,) if kind.normalized_metadata else ()
                        conn.execute(
                            "EXPLAIN "
                            + with_metadata(f"SELECT 1 FROM messages m WHERE {sql}", metadata),
                            params,
                        )
                    except sqlite3.OperationalError:
                        assert report[name]["status"] == "unavailable", (table, column, name)
            conn.execute("ROLLBACK TO missing_field")
            conn.execute("RELEASE missing_field")
    conn.close()


def test_attachment_coverage_checks_every_retained_occurrence(tmp_path):
    from tests.conftest import _insert_attachment

    conn, path = _open_built_db_conn(tmp_path, "retained-occurrences.db")
    _insert_message(
        conn,
        message_id="retained",
        thread_id="t",
        sent_at="2024-01-01T00:00:00+00:00",
        has_attachments=False,
    )
    _insert_attachment(
        conn, message_id="retained", thread_id="t", attachment_id="a", filename="synthetic.txt"
    )
    conn.commit()
    db = Database(str(path))
    entry = db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]
    assert entry["status"] == "supported after backfill"
    assert entry["available_messages"] == 0
    conn.execute("UPDATE attachments SET text_complete = 0")
    conn.commit()
    assert (
        db.get_mailbox_status()["query_capabilities"]["attachment_text_words"]["status"]
        == "supported when present"
    )
    conn.close()

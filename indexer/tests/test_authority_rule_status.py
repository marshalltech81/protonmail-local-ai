"""Distinguish missing versus explicitly empty authority rules (#1234)."""

import pytest
from src.entities import load_authority_rules


@pytest.mark.parametrize("exists", [False, True])
def test_rule_loader_retains_whether_a_rules_file_was_loaded(tmp_path, exists):
    path = tmp_path / "synthetic-authority.toml"
    if exists:
        path.write_text("")
    rules = load_authority_rules(path)
    assert rules.pattern_count == 0
    assert rules.configured is exists


def test_applied_rule_metadata_and_entities_change_together(db, caplog):
    import json

    from src.entities import AuthorityRules

    from tests.conftest import make_message, make_thread

    db.upsert_thread(
        make_thread(messages=[make_message(from_addr="SYNTHETIC_PRIVATE@example.test")]),
        [0.0] * 4096,
    )
    rules = AuthorityRules(domains={"example.test": "vendor"})
    db.set_authority_rules(rules)
    row = db._conn.execute(
        "SELECT configured, rule_count, classes FROM authority_rule_state"
    ).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == (1, 1, ["vendor"])
    assert (
        db._conn.execute("SELECT COUNT(*) FROM entities WHERE authority_class='vendor'").fetchone()[
            0
        ]
        > 0
    )
    assert "SYNTHETIC_PRIVATE" not in caplog.text
    assert "example.test" not in caplog.text
    db.set_authority_rules(AuthorityRules(configured=False))
    row = db._conn.execute(
        "SELECT configured, rule_count, classes FROM authority_rule_state"
    ).fetchone()
    assert (row[0], row[1], json.loads(row[2])) == (0, 0, [])
    assert (
        db._conn.execute("SELECT COUNT(*) FROM entities WHERE authority_class='vendor'").fetchone()[
            0
        ]
        == 0
    )


def test_metadata_failure_rolls_back_reclassification_and_keeps_applied_rules(db):
    import sqlite3

    from src.entities import AuthorityRules

    from tests.conftest import make_message, make_thread

    db.upsert_thread(
        make_thread(messages=[make_message(from_addr="SYNTHETIC_PRIVATE@example.test")]),
        [0.0] * 4096,
    )
    old = AuthorityRules(domains={"example.test": "vendor"})
    db.set_authority_rules(old)
    before = [tuple(row) for row in db._conn.execute("SELECT * FROM entities")]
    db._conn.execute(
        "CREATE TRIGGER reject_rule_state BEFORE UPDATE ON authority_rule_state BEGIN SELECT RAISE(ABORT, 'synthetic metadata failure'); END"
    )
    db._conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic metadata failure"):
        db.set_authority_rules(AuthorityRules(domains={"example.test": "personal"}))
    assert [tuple(row) for row in db._conn.execute("SELECT * FROM entities")] == before
    assert (
        db._conn.execute("SELECT classes FROM authority_rule_state").fetchone()[0] == '["vendor"]'
    )
    assert db._authority_rules is old


def test_v23_migration_keeps_existing_evidence_and_unknown_rules(tmp_path):
    from src.database import SCHEMA_VERSION, Database

    from tests.conftest import TEST_ACCOUNT, make_message, make_thread
    from tests.test_database import _schema_shape

    path = tmp_path / "old.db"
    old = Database(path, account=TEST_ACCOUNT)
    old.upsert_thread(make_thread(messages=[make_message()]), [0.0] * 4096)
    before = [dict(row) for row in old._conn.execute("SELECT * FROM messages")]
    for row in before:
        row["attachments_manifest_complete"] = None  # v29 requires reassessment.
    from tests.conftest import drop_literal_schema

    drop_literal_schema(old._conn)
    old._conn.execute("DROP TABLE authority_rule_state")
    old._conn.execute("UPDATE schema_version SET version=23")
    old._conn.commit()
    old.close()
    migrated = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert (
            migrated._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert [dict(row) for row in migrated._conn.execute("SELECT * FROM messages")] == before
        assert (
            migrated._conn.execute("SELECT COUNT(*) FROM authority_rule_state").fetchone()[0] == 0
        )
        assert (
            migrated._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 1
        )  # v25 queues literal-index backfill.
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
    finally:
        migrated.close()
        fresh.close()

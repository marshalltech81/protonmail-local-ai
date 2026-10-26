"""Declared Bridge scope is mutable metadata, independent of account identity."""

import sqlite3
from dataclasses import replace
from datetime import datetime

import pytest
from src.account import AccountConfigError, account_config_from_env
from src.database import SCHEMA_VERSION, Database

from tests.conftest import TEST_ACCOUNT, drop_extraction_fingerprint_schema


def _scope(db):
    return dict(
        db._conn.execute(
            "SELECT declared, previous_declared, changed_at FROM account_address_scope WHERE account_id=?",
            (db.account_id,),
        ).fetchone()
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "unverified"),
        (" \t", "unverified"),
        ("COMBINED", "combined"),
        (" Split \n", "split"),
        ("unverified", "unverified"),
    ],
)
def test_scope_is_normalized_without_changing_account_identity(raw, expected):
    account = account_config_from_env(
        {"ACCOUNT_SCOPE": TEST_ACCOUNT.scope_uuid, "PROTON_DECLARED_ADDRESS_SCOPE": raw},
        maildir_root=TEST_ACCOUNT.maildir_root,
    )
    assert account.declared_address_scope == expected
    assert account.scope_uuid == TEST_ACCOUNT.scope_uuid


def test_invalid_scope_never_quotes_private_input():
    marker = "SYNTHETIC_PRIVATE_SCOPE_MARKER"
    with pytest.raises(AccountConfigError) as exc:
        account_config_from_env(
            {"ACCOUNT_SCOPE": TEST_ACCOUNT.scope_uuid, "PROTON_DECLARED_ADDRESS_SCOPE": marker},
            maildir_root=TEST_ACCOUNT.maildir_root,
        )
    assert marker not in str(exc.value)
    assert "PROTON_DECLARED_ADDRESS_SCOPE" in str(exc.value)


def test_changes_are_reported_not_refused_and_unchanged_restarts_keep_the_time(tmp_path):
    path = tmp_path / "mail.db"
    first = replace(TEST_ACCOUNT, declared_address_scope="combined")
    db = Database(path, account=first)
    account_id = db.account_id
    assert _scope(db) == {
        "declared": "combined",
        "previous_declared": None,
        "changed_at": None,
    }
    db.close()
    db = Database(path, account=replace(first, declared_address_scope="split"))
    changed = _scope(db)
    assert db.account_id == account_id
    assert changed["declared"] == "split" and changed["previous_declared"] == "combined"
    assert datetime.fromisoformat(changed["changed_at"]).utcoffset().total_seconds() == 0
    with pytest.raises(sqlite3.IntegrityError, match="accounts rows are never changed"):
        db._conn.execute("UPDATE accounts SET provider='other'")
    db.close()
    db = Database(path, account=replace(first, declared_address_scope="split"))
    assert _scope(db) == changed
    db.close()


def test_v25_migration_preserves_account_and_queues_no_reparse(tmp_path):
    path = tmp_path / "mail.db"
    db = Database(path, account=TEST_ACCOUNT)
    account_id = db.account_id
    drop_extraction_fingerprint_schema(db._conn)
    db._conn.execute("DROP TABLE account_address_scope")
    db._conn.execute("UPDATE schema_version SET version=25")
    db._conn.commit()
    db.close()
    db = Database(path, account=replace(TEST_ACCOUNT, declared_address_scope="split"))
    assert SCHEMA_VERSION == 29
    assert db.account_id == account_id
    assert _scope(db)["declared"] == "split"
    assert db.queue_stats() == {"queued": 0, "dead": 0}
    db.close()


def test_migrated_and_fresh_scope_schemas_match(tmp_path):
    from tests.test_database import _schema_shape

    path = tmp_path / "old.db"
    old = Database(path, account=TEST_ACCOUNT)
    drop_extraction_fingerprint_schema(old._conn)
    old._conn.execute("DROP TABLE account_address_scope")
    old._conn.execute("UPDATE schema_version SET version=25")
    old._conn.commit()
    old.close()
    migrated = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
        with pytest.raises(sqlite3.IntegrityError):
            migrated._conn.execute("UPDATE account_address_scope SET declared='invalid'")
    finally:
        migrated.close()
        fresh.close()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " \t\r\n\v\f",
        "COMBINED",
        " Split ",
        "unverified",
        "SYNTHETIC_PRIVATE_SCOPE_MARKER",
        "splİt",
        "\u00a0split",
    ],
)
def test_shell_and_indexer_accept_the_same_scope_values(raw):
    import subprocess
    from pathlib import Path

    from src.account import validate_declared_address_scope

    script = Path(__file__).parents[2] / "mbsync" / "proton-settings.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; proton_settings_problem proton-bridge host.docker.internal 1143 '
            '127.0.0.1 "All Mail|Labels/*|Starred" protonmail far near channel "$2"',
            "scope-test",
            str(script),
            raw,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        validate_declared_address_scope(raw)
    except AccountConfigError as exc:
        assert result.returncode != 0
        assert result.stdout.strip() == str(exc)
    else:
        assert result.returncode == 0 and result.stdout == ""

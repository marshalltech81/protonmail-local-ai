"""#1596: the account registry. The index records the configured account
(``ACCOUNT_SCOPE`` and the ``SOURCE_MODE`` provider) on a fresh index and
in the v14 migration, atomically with the version stamp, and refuses to
open under any other account, with fixed text that names no value."""

import os
import shutil
import sqlite3
import subprocess  # nosec B404 - runs bash on the repository's own script
from dataclasses import replace
from pathlib import Path

import pytest
from src import account as account_mod
from src import main
from src.account import (
    ACCOUNT_SCOPE_MALFORMED,
    ACCOUNT_SCOPE_MISSING,
    SOURCE_MODE_REFUSED,
    AccountConfig,
    AccountConfigError,
    AccountMismatchError,
    account_config_from_env,
    register_account_functions,
)
from src.database import EMBEDDING_DIM, SCHEMA_VERSION, Database
from src.migrations import runner
from src.occurrences import SeedFunctions

from tests.conftest import TEST_ACCOUNT, make_message, make_thread
from tests.test_database import _schema_shape, _v13_from_fresh

_REPO = Path(__file__).resolve().parents[2]
_PROTON_SETTINGS = _REPO / "mbsync" / "proton-settings.sh"
_MIGRATIONS = Path(__file__).resolve().parents[1] / "src" / "migrations"

OTHER_SCOPE = "00000000-0000-4000-8000-0000000000ff"
MARKER = "zq9marker"

# SOURCE_MODE values as Compose passes them (an unset one as empty).
_SOURCE_MODES = [
    "",
    "proton-bridge",
    "Proton-Bridge",
    " PROTON-BRIDGE\t",
    "\nproton-bridge\r\n",
    " ",
    "\t",
    "proton bridge",
    "proton-bridge-x",
    "imap",
    "proton-brİdge",  # a non-ASCII capital, not lower-cased by tr under LC_ALL=C
    "PROTON-BRIDGE ",  # no-break space, not C-locale whitespace
    MARKER,
]


def _shell_accepts(value: str) -> bool:
    """Whether ``scripts/validate-env.sh`` and the mbsync entrypoint accept
    ``value``: the shared helper's normalization and check, run by bash."""
    script = (
        'source "$1"; v="$(proton_normalize_source_mode "${2:-$PROTON_DEFAULT_SOURCE_MODE}")"; '
        'proton_settings_problem "$v" host.docker.internal 1143 127.0.0.1 false '
        '"$PROTON_DEFAULT_FOLDER_EXCLUSIONS" protonmail protonmail-remote protonmail-local '
        "protonmail"
    )
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # nosec B603 - fixed argv, synthetic values
        [bash, "-c", script, "bash", str(_PROTON_SETTINGS), value],
        capture_output=True,
        text=True,
        check=False,
        # The C locale the helper names and the mbsync image runs in: its
        # ``local LC_ALL=C`` is not exported to ``tr``, so a UTF-8 locale
        # in the caller's environment would lower-case non-ASCII letters
        # on the host only (#1597).
        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
    )
    if result.returncode:
        # The shell's refusal is the text the services use.
        assert result.stdout.strip() == SOURCE_MODE_REFUSED
    return result.returncode == 0


class TestConfiguration:
    @pytest.mark.parametrize("value", _SOURCE_MODES)
    def test_source_mode_matches_the_shared_shell_rule(self, value):
        env = {"SOURCE_MODE": value, "ACCOUNT_SCOPE": TEST_ACCOUNT.scope_uuid}
        try:
            config = account_config_from_env(env, maildir_root=Path("/maildir"))
        except AccountConfigError as exc:
            assert str(exc) == SOURCE_MODE_REFUSED
            assert not _shell_accepts(value)
        else:
            assert config.provider == "proton-bridge"
            assert _shell_accepts(value)

    def test_the_source_mode_table_covers_both_outcomes(self):
        outcomes = {_shell_accepts(v) for v in _SOURCE_MODES}
        assert outcomes == {True, False}

    @pytest.mark.parametrize(
        ("env", "expected"),
        [({}, Path("/maildir")), ({"MAILDIR_PATH": "/data/mail"}, Path("/data/mail"))],
    )
    def test_maildir_root_is_read_as_given_when_absolute(self, env, expected):
        assert account_mod.maildir_root_from_env(env) == expected

    def test_a_relative_maildir_root_is_made_absolute(self, tmp_path, monkeypatch):
        """Occurrence locators need absolute paths (#1566): a relative
        ``MAILDIR_PATH`` (a run outside Compose) is resolved against the
        working directory once, so the walk's paths are absolute too."""
        monkeypatch.chdir(tmp_path)
        assert account_mod.maildir_root_from_env({"MAILDIR_PATH": "mail"}) == tmp_path / "mail"

    def test_unset_source_mode_is_the_default(self):
        config = account_config_from_env(
            {"ACCOUNT_SCOPE": TEST_ACCOUNT.scope_uuid}, maildir_root=Path("/m")
        )
        assert config == AccountConfig(TEST_ACCOUNT.scope_uuid, "proton-bridge", Path("/m"))

    @pytest.mark.parametrize("env", [{}, {"ACCOUNT_SCOPE": ""}])
    def test_missing_scope_is_refused(self, env):
        with pytest.raises(AccountConfigError) as exc:
            account_config_from_env(env, maildir_root=Path("/m"))
        assert str(exc.value) == ACCOUNT_SCOPE_MISSING

    @pytest.mark.parametrize(
        "value",
        [
            MARKER,
            "0000000A-0000-4000-8000-000000000001",
            " 00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000001\n",
            "{00000000-0000-4000-8000-000000000001}",
            "00000000000040008000000000000001",
            "00000000-0000-4000-8000-0000000000012",
            "０0000000-0000-4000-8000-000000000001",  # a full-width digit
        ],
    )
    def test_malformed_scope_is_refused_without_its_value(self, value):
        with pytest.raises(AccountConfigError) as exc:
            account_config_from_env({"ACCOUNT_SCOPE": value}, maildir_root=Path("/m"))
        assert str(exc.value) == ACCOUNT_SCOPE_MALFORMED
        assert value.strip() not in str(exc.value)

    def test_source_mode_is_checked_before_the_scope(self):
        with pytest.raises(AccountConfigError) as exc:
            account_config_from_env({"SOURCE_MODE": MARKER}, maildir_root=Path("/m"))
        assert str(exc.value) == SOURCE_MODE_REFUSED

    def test_refusals_match_validate_env(self):
        """``scripts/validate-env.sh`` prints the same fixed text."""
        text = (_REPO / "scripts" / "validate-env.sh").read_text()
        assert ACCOUNT_SCOPE_MISSING in text
        assert ACCOUNT_SCOPE_MALFORMED in text


def _rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, scope_uuid, provider FROM accounts").fetchall()
    finally:
        conn.close()


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version FROM schema_version").fetchone()[0]
    finally:
        conn.close()


def _v13_database(path: Path, *, messages: int = 2) -> None:
    """A populated v13 index, as the deployed indexer left it."""
    db = Database(path, account=TEST_ACCOUNT)
    for i in range(messages):
        msg = make_message(message_id=f"m{i}@example.test", filepath=f"/maildir/INBOX/cur/m{i}")
        db.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
    _v13_from_fresh(db)
    db.close()


class TestRegistry:
    def test_fresh_index_stores_one_row(self, tmp_path, caplog):
        caplog.set_level("INFO")
        db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
        try:
            rows = db._conn.execute("SELECT * FROM accounts").fetchall()
            assert [(r["scope_uuid"], r["provider"]) for r in rows] == [
                (TEST_ACCOUNT.scope_uuid, "proton-bridge")
            ]
            assert rows[0]["created_at"]
            assert db.account_id == rows[0]["id"]
        finally:
            db.close()
        assert "Account verified against the index (provider=proton-bridge)" in caplog.text
        assert TEST_ACCOUNT.scope_uuid not in caplog.text

    def test_restart_with_the_same_account_starts(self, tmp_path):
        Database(tmp_path / "mail.db", account=TEST_ACCOUNT).close()
        # The root is not part of the account: relocating it keeps the index.
        moved = replace(TEST_ACCOUNT, maildir_root=Path("/elsewhere"))
        db = Database(tmp_path / "mail.db", account=moved)
        try:
            assert len(_rows(tmp_path / "mail.db")) == 1
        finally:
            db.close()

    @pytest.mark.parametrize(
        ("changed", "expected"),
        [
            ({"scope_uuid": OTHER_SCOPE}, account_mod.ACCOUNT_SCOPE_DIFFERS),
            ({"provider": "imap"}, account_mod.ACCOUNT_PROVIDER_DIFFERS),
        ],
    )
    def test_a_different_account_fails_closed_naming_no_value(self, tmp_path, changed, expected):
        Database(tmp_path / "mail.db", account=TEST_ACCOUNT).close()
        with pytest.raises(AccountMismatchError) as exc:
            Database(tmp_path / "mail.db", account=replace(TEST_ACCOUNT, **changed))
        assert str(exc.value) == expected
        for value in (TEST_ACCOUNT.scope_uuid, OTHER_SCOPE, "imap"):
            assert value not in str(exc.value)
        # Nothing was written: the stored account is unchanged.
        assert [r[1:] for r in _rows(tmp_path / "mail.db")] == [
            (TEST_ACCOUNT.scope_uuid, "proton-bridge")
        ]

    def test_no_row_or_several_rows_fail_closed(self, tmp_path):
        path = tmp_path / "mail.db"
        Database(path, account=TEST_ACCOUNT).close()
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO accounts (scope_uuid, provider, created_at) VALUES (?, 'proton-bridge', 'd')",
            (OTHER_SCOPE,),
        )
        conn.commit()
        conn.close()
        with pytest.raises(AccountMismatchError) as exc:
            Database(path, account=TEST_ACCOUNT)
        assert str(exc.value) == account_mod.ACCOUNT_SEVERAL_STORED

        conn = sqlite3.connect(path)
        conn.execute("DROP TRIGGER accounts_never_deleted")
        conn.execute("DELETE FROM accounts")
        conn.commit()
        conn.close()
        with pytest.raises(AccountMismatchError) as exc:
            Database(path, account=TEST_ACCOUNT)
        assert str(exc.value) == account_mod.ACCOUNT_NONE_STORED

    def test_triggers_reject_update_and_delete(self, tmp_path):
        db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="never changed"):
                db._conn.execute("UPDATE accounts SET scope_uuid = ?", (OTHER_SCOPE,))
            with pytest.raises(sqlite3.IntegrityError, match="never changed"):
                db._conn.execute("UPDATE accounts SET provider = 'imap'")
            with pytest.raises(sqlite3.IntegrityError, match="never removed"):
                db._conn.execute("DELETE FROM accounts")
            db._conn.rollback()
            assert len(db._conn.execute("SELECT * FROM accounts").fetchall()) == 1
        finally:
            db.close()

    def test_a_rebuild_with_the_same_configuration_reproduces_the_row(self, tmp_path):
        Database(tmp_path / "a.db", account=TEST_ACCOUNT).close()
        Database(tmp_path / "b.db", account=TEST_ACCOUNT).close()
        assert [r[1:] for r in _rows(tmp_path / "a.db")] == [
            r[1:] for r in _rows(tmp_path / "b.db")
        ]


class TestMigrationV14:
    def test_upgrade_binds_the_configured_account(self, tmp_path, caplog):
        caplog.set_level("INFO")
        path = tmp_path / "v13.db"
        _v13_database(path)
        migrated = Database(path, account=TEST_ACCOUNT)
        fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
        try:
            assert _version(path) == SCHEMA_VERSION
            assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
            assert [r[1:] for r in _rows(path)] == [(TEST_ACCOUNT.scope_uuid, "proton-bridge")]
            assert migrated.account_id == _rows(path)[0][0]
            # The triggers came with the table.
            triggers = {
                r[0]
                for r in migrated._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'accounts'"
                )
            }
            assert triggers == {"accounts_never_updated", "accounts_never_deleted"}
        finally:
            migrated.close()
            fresh.close()
        assert "applied migrations: [14" in caplog.text
        assert TEST_ACCOUNT.scope_uuid not in caplog.text

    def test_upgrade_binds_whatever_scope_is_configured(self, tmp_path):
        path = tmp_path / "v13.db"
        _v13_database(path)
        other = replace(TEST_ACCOUNT, scope_uuid=OTHER_SCOPE)
        Database(path, account=other).close()
        assert [r[1:] for r in _rows(path)] == [(OTHER_SCOPE, "proton-bridge")]
        with pytest.raises(AccountMismatchError):
            Database(path, account=TEST_ACCOUNT)

    def test_an_interruption_inside_the_migration_leaves_no_row_and_no_version(self, tmp_path):
        """A failure after the account row is inserted rolls it back with
        the rest of the migration; the next start binds it once."""
        path = tmp_path / "v13.db"
        _v13_database(path)
        broken = tmp_path / "migrations"
        shutil.copytree(_MIGRATIONS, broken, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
        target = next(broken.glob("0014_*.sql"))
        target.write_text(target.read_text() + "\nINSERT INTO no_such_table VALUES (1);\n")
        conn = sqlite3.connect(path)
        register_account_functions(conn, TEST_ACCOUNT)
        SeedFunctions(TEST_ACCOUNT.maildir_root).register(conn)
        try:
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                runner.apply_pending(
                    conn, current_version=13, target_version=14, migration_dir=broken
                )
        finally:
            conn.close()
        assert _version(path) == 13
        conn = sqlite3.connect(path)
        try:
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
                ).fetchone()
                is None
            )
        finally:
            conn.close()
        Database(path, account=TEST_ACCOUNT).close()
        assert len(_rows(path)) == 1

    def test_the_migration_cannot_run_without_the_configured_account(self, tmp_path):
        """The SQL reads the account only through the connection-local
        functions: without them nothing is bound and v13 stays."""
        path = tmp_path / "v13.db"
        _v13_database(path)
        conn = sqlite3.connect(path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="pmai_account_scope"):
                runner.apply_pending(
                    conn, current_version=13, target_version=14, migration_dir=_MIGRATIONS
                )
        finally:
            conn.close()
        assert _version(path) == 13


class TestStartup:
    """``main()`` refuses a missing or malformed account before the
    database opens, so no migration runs under it."""

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"ACCOUNT_SCOPE": None}, ACCOUNT_SCOPE_MISSING),
            ({"ACCOUNT_SCOPE": MARKER}, ACCOUNT_SCOPE_MALFORMED),
            ({"SOURCE_MODE": MARKER}, SOURCE_MODE_REFUSED),
        ],
    )
    def test_refused_before_the_database_opens(self, monkeypatch, env, expected):
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://host.docker.internal:8001/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "unauthenticated")
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        built: list[str] = []
        monkeypatch.setattr(main, "Database", lambda path, account: built.append("database"))
        with pytest.raises(AccountConfigError) as exc:
            main.main()
        assert str(exc.value) == expected
        assert MARKER not in str(exc.value)
        assert built == []

"""#1596: mcp-server's account check. The indexer registers the account
the index serves (``accounts``); mcp-server validates its own
``ACCOUNT_SCOPE`` and ``SOURCE_MODE`` and refuses to start unless the
index holds exactly that one account, with fixed text naming no value."""

import logging
import os
import shutil
import sqlite3
import subprocess  # nosec B404 - runs bash on the repository's own script
from contextlib import closing
from pathlib import Path

import pytest
from src.lib import account as account_mod
from src.lib.account import (
    ACCOUNT_SCOPE_MALFORMED,
    ACCOUNT_SCOPE_MISSING,
    SOURCE_MODE_REFUSED,
    AccountConfig,
    AccountConfigError,
    AccountMismatchError,
    account_config_from_env,
    run_startup_account_check,
    verify_account,
)
from src.lib.sqlite import Database

from tests.conftest import TEST_ACCOUNT_SCOPE

_REPO = Path(__file__).resolve().parents[2]
_PROTON_SETTINGS = _REPO / "mbsync" / "proton-settings.sh"
_INDEXER_ACCOUNT = _REPO / "indexer" / "src" / "account.py"

OTHER_SCOPE = "00000000-0000-4000-8000-0000000000ff"
MARKER = "zq9marker"
ACCOUNT = AccountConfig(scope_uuid=TEST_ACCOUNT_SCOPE, provider="proton-bridge")

# The indexer's DDL (``indexer/src/database.py`` ``ACCOUNT_SCHEMA_STATEMENTS``).
_DDL = """
CREATE TABLE accounts (
    id         INTEGER PRIMARY KEY,
    scope_uuid TEXT NOT NULL UNIQUE,
    provider   TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_SOURCE_MODES = [
    "",
    "proton-bridge",
    "Proton-Bridge",
    " PROTON-BRIDGE\t",
    " ",
    "proton bridge",
    "imap",
    "proton-brİdge",
    "PROTON-BRIDGE ",
    MARKER,
]


def _shell_accepts(value: str) -> bool:
    """Whether ``mbsync/proton-settings.sh`` accepts ``value``, normalized
    as ``scripts/validate-env.sh`` and the entrypoint do (C locale, as in
    the image; #1597)."""
    script = (
        'source "$1"; v="$(proton_normalize_source_mode "${2:-$PROTON_DEFAULT_SOURCE_MODE}")"; '
        'proton_settings_problem "$v" host.docker.internal 1143 127.0.0.1 '
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
        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
    )
    if result.returncode:
        assert result.stdout.strip() == SOURCE_MODE_REFUSED
    return result.returncode == 0


def _db(tmp_path, rows: list[tuple[str, str]] | None, *, table: bool = True) -> Database:
    path = tmp_path / "mail.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        if table:
            conn.execute(_DDL)
            conn.executemany(
                "INSERT INTO accounts (scope_uuid, provider, created_at) VALUES (?, ?, 'd')",
                rows or [],
            )
        conn.commit()
    finally:
        conn.close()
    return Database(str(path))


class TestConfiguration:
    @pytest.mark.parametrize("value", _SOURCE_MODES)
    def test_source_mode_matches_the_shared_shell_rule(self, value):
        env = {"SOURCE_MODE": value, "ACCOUNT_SCOPE": TEST_ACCOUNT_SCOPE}
        try:
            config = account_config_from_env(env)
        except AccountConfigError as exc:
            assert str(exc) == SOURCE_MODE_REFUSED
            assert not _shell_accepts(value)
        else:
            assert config == ACCOUNT
            assert _shell_accepts(value)

    @pytest.mark.parametrize("env", [{}, {"ACCOUNT_SCOPE": ""}])
    def test_missing_scope_is_refused(self, env):
        with pytest.raises(AccountConfigError) as exc:
            account_config_from_env(env)
        assert str(exc.value) == ACCOUNT_SCOPE_MISSING

    @pytest.mark.parametrize(
        "value",
        [
            MARKER,
            "0000000A-0000-4000-8000-000000000001",
            " 00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000001\n",
            "00000000000040008000000000000001",
        ],
    )
    def test_malformed_scope_is_refused_without_its_value(self, value):
        with pytest.raises(AccountConfigError) as exc:
            account_config_from_env({"ACCOUNT_SCOPE": value})
        assert str(exc.value) == ACCOUNT_SCOPE_MALFORMED
        assert value.strip() not in str(exc.value)

    def test_texts_match_the_indexer_and_validate_env(self):
        indexer = _INDEXER_ACCOUNT.read_text()
        validator = (_REPO / "scripts" / "validate-env.sh").read_text()
        for text in (ACCOUNT_SCOPE_MISSING, ACCOUNT_SCOPE_MALFORMED):
            assert text in validator
        for name in (
            "ACCOUNT_SCOPE_MISSING",
            "ACCOUNT_SCOPE_MALFORMED",
            "SOURCE_MODE_REFUSED",
            "ACCOUNT_NONE_STORED",
            "ACCOUNT_SEVERAL_STORED",
            "ACCOUNT_SCOPE_DIFFERS",
            "ACCOUNT_PROVIDER_DIFFERS",
            "_REMEDY",
            "_ACCOUNT_SCOPE_RE",
        ):
            assert _definition(indexer, name) == _definition(
                Path(account_mod.__file__).read_text(), name
            ), name


def _definition(source: str, name: str) -> str:
    """The text of the top-level assignment to ``name`` in ``source``."""
    start = source.index(f"\n{name} = ")
    end = source.index("\n)\n", start)
    return source[start:end]


class TestVerify:
    def test_the_configured_account_passes(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        run_startup_account_check(_db(tmp_path, [(TEST_ACCOUNT_SCOPE, "proton-bridge")]), ACCOUNT)
        assert "Account verified against the index (provider=proton-bridge)" in caplog.text
        assert TEST_ACCOUNT_SCOPE not in caplog.text

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            ([(OTHER_SCOPE, "proton-bridge")], account_mod.ACCOUNT_SCOPE_DIFFERS),
            ([(TEST_ACCOUNT_SCOPE, "imap")], account_mod.ACCOUNT_PROVIDER_DIFFERS),
            ([], account_mod.ACCOUNT_NONE_STORED),
            (
                [(TEST_ACCOUNT_SCOPE, "proton-bridge"), (OTHER_SCOPE, "proton-bridge")],
                account_mod.ACCOUNT_SEVERAL_STORED,
            ),
        ],
    )
    def test_any_other_registry_fails_closed_naming_no_value(self, tmp_path, rows, expected):
        db = _db(tmp_path, rows)
        with pytest.raises(AccountMismatchError) as exc:
            verify_account(db, ACCOUNT)
        assert str(exc.value) == expected
        with pytest.raises(SystemExit) as stop:
            run_startup_account_check(db, ACCOUNT)
        assert str(stop.value) == expected
        for value in (TEST_ACCOUNT_SCOPE, OTHER_SCOPE, "imap"):
            assert value not in expected

    def test_an_index_without_the_registry_waits_for_the_indexer(self, tmp_path):
        with pytest.raises(SystemExit) as stop:
            run_startup_account_check(_db(tmp_path, None, table=False), ACCOUNT)
        assert str(stop.value) == account_mod.ACCOUNT_REGISTRY_MISSING

    def test_the_check_reads_through_the_read_only_connection(self, tmp_path):
        db = _db(tmp_path, [(TEST_ACCOUNT_SCOPE, "proton-bridge")])
        with closing(db._connect()) as conn, conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        before = (tmp_path / "mail.db").read_bytes()
        verify_account(db, ACCOUNT)
        assert (tmp_path / "mail.db").read_bytes() == before


class TestStartup:
    """``main()`` refuses a missing or malformed account before it opens
    the index."""

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"ACCOUNT_SCOPE": None}, ACCOUNT_SCOPE_MISSING),
            ({"ACCOUNT_SCOPE": MARKER}, ACCOUNT_SCOPE_MALFORMED),
            ({"SOURCE_MODE": MARKER}, SOURCE_MODE_REFUSED),
        ],
    )
    def test_refused_before_the_index_opens(self, monkeypatch, env, expected):
        import src.main as main_mod

        monkeypatch.setattr(main_mod, "MCP_AUTH_TOKEN", "x" * 40)
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        opened: list[str] = []
        monkeypatch.setattr(main_mod, "Database", lambda path: opened.append(path))
        with pytest.raises(SystemExit) as stop:
            main_mod.main()
        assert str(stop.value) == expected
        assert MARKER not in str(stop.value)
        assert opened == []

    def test_main_checks_the_account_before_serving(self, monkeypatch):
        import src.main as main_mod

        order: list[str] = []

        class _Db:
            def __init__(self, _path):
                order.append("open")

            def get_accounts(self):
                order.append("account")
                return [(OTHER_SCOPE, "proton-bridge")]

        monkeypatch.setattr(main_mod, "MCP_AUTH_TOKEN", "x" * 40)
        monkeypatch.setattr(main_mod, "EMBED_BASE_URL", "http://host.docker.internal:8001/v1")
        monkeypatch.setattr(main_mod, "EMBED_MODEL", "synthetic")
        monkeypatch.setattr(main_mod, "EMBED_API_KEY", "unauthenticated")
        monkeypatch.setattr(main_mod, "INFERENCE_MODE", "none")
        monkeypatch.setattr(main_mod, "RERANK_MODE", "none")
        monkeypatch.setattr(main_mod, "Database", _Db)
        monkeypatch.setattr(
            main_mod, "run_startup_identity_check", lambda *a, **k: order.append("identity")
        )
        monkeypatch.setattr(main_mod, "_run_server", lambda *_: order.append("serve"))
        with pytest.raises(SystemExit) as stop:
            main_mod.main()
        assert str(stop.value) == account_mod.ACCOUNT_SCOPE_DIFFERS
        assert order == ["open", "account"]

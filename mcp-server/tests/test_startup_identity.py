"""#887: mcp-server logs one INFO line at startup naming the source
commit, a random boot ID, the schema version the index carries and a
hash of the non-secret settings, so log lines from before and after a
rebuild or restart can be told apart."""

import logging
import re
import sqlite3
from contextlib import closing

import pytest
import src.main as main_mod
from src.lib.sqlite import read_stored_schema_version

_LINE = re.compile(
    r"Startup identity: service=mcp-server commit=(?P<commit>\S+) "
    r"boot=(?P<boot>[0-9a-f]{12}) schema_stored=(?P<stored>\d+|none|unreadable) "
    r"config=(?P<config>[0-9a-f]{12})"
)
_SECRET_MARKER = "synthetic-secret-marker-887"  # pragma: allowlist secret
_SECRET_NAMES = ("INFERENCE_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "MCP_AUTH_TOKEN")


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(caplog, stored: str = "0") -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main_mod._log_startup_identity(stored)
    [record] = _identity_records(caplog)
    assert record.levelno == logging.INFO
    match = _LINE.fullmatch(record.getMessage())
    assert match, record.getMessage()
    return match


def test_line_format(monkeypatch, caplog):
    monkeypatch.setenv("GIT_COMMIT", "abc1234-dirty")
    first = _log(caplog, "0")
    assert first["commit"] == "abc1234-dirty"
    assert first["stored"] == "0"
    second = _log(caplog, "none")
    assert second["stored"] == "none"
    # Each start gets its own boot ID; the settings did not change.
    assert second["boot"] != first["boot"]
    assert second["config"] == first["config"]


@pytest.mark.parametrize("value", [None, "", "  ", "abc 123", "abc\nforged line", "x" * 65])
def test_missing_or_unusable_commit_is_logged_as_unknown(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("GIT_COMMIT", value)
    assert _log(caplog)["commit"] == "unknown"


def test_config_hash_covers_only_the_named_non_secret_settings():
    names = set(main_mod._identity_settings())
    assert names == {
        "INFERENCE_MODE",
        "INFERENCE_BASE_URL",
        "INFERENCE_MODEL",
        "INFERENCE_TIMEOUT_SECS",
        "INFERENCE_MAX_TOKENS",
        "INFERENCE_CONTEXT_TOKENS",
        "INFERENCE_STRUCTURED_OUTPUT",
        "EMBED_MODE",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
        "EMBED_TIMEOUT_SECS",
        "RERANK_MODE",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_CANDIDATES",
        "RERANK_TIMEOUT_SECS",
        "MCP_PORT",
        "MCP_SESSION_IDLE_TIMEOUT_SECS",
        "MCP_EXPERIMENTAL_TOOLS",
    }
    assert not names & set(_SECRET_NAMES)


def test_changing_a_secret_does_not_change_the_hash(monkeypatch, caplog):
    def set_secrets(suffix: str) -> None:
        for name in _SECRET_NAMES:
            monkeypatch.setattr(main_mod, name, f"{_SECRET_MARKER}-{suffix}")
            monkeypatch.setenv(name, f"{_SECRET_MARKER}-{suffix}")

    set_secrets("a")
    before = _log(caplog)["config"]
    set_secrets("b")
    after = _log(caplog)["config"]
    assert _SECRET_MARKER not in caplog.text
    # A named setting does change it, so the hash is not a constant.
    monkeypatch.setattr(main_mod, "EMBED_MODEL", "synthetic-other-model")
    changed = _log(caplog)["config"]
    assert before == after
    assert changed != before


class TestReadStoredSchemaVersion:
    """The stored version is read read-only, without the ``Database``
    checks that can stop startup (Codex round 3 on #893): ``none`` when
    the file, table or row is missing, ``unreadable`` when SQLite cannot
    read it."""

    def _db(self, tmp_path, statements: list[str]) -> str:
        path = tmp_path / "mail.db"
        with closing(sqlite3.connect(path)) as conn:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        return str(path)

    def test_returns_the_stored_version(self, tmp_path):
        path = self._db(
            tmp_path,
            [
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)",
                "INSERT INTO schema_version VALUES (3)",
            ],
        )
        assert read_stored_schema_version(path) == "3"

    def test_empty_table_is_none(self, tmp_path):
        path = self._db(tmp_path, ["CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"])
        assert read_stored_schema_version(path) == "none"

    def test_missing_table_is_none(self, tmp_path):
        path = self._db(tmp_path, ["CREATE TABLE other (x INTEGER)"])
        assert read_stored_schema_version(path) == "none"

    def test_missing_file_is_none_and_is_not_created(self, tmp_path):
        path = tmp_path / "missing" / "mail.db"
        assert read_stored_schema_version(str(path)) == "none"
        assert not path.exists()

    def test_a_file_sqlite_cannot_read_is_unreadable(self, tmp_path):
        path = tmp_path / "mail.db"
        path.write_bytes(b"not a database" * 100)
        assert read_stored_schema_version(str(path)) == "unreadable"


class _MainFakeDatabase:
    def __init__(self, _path):
        pass

    def get_embedding_dim(self):
        return 4


def _configure_main(monkeypatch) -> None:
    for name, value in {
        "EMBED_BASE_URL": "http://host.docker.internal:8001/v1",
        "EMBED_MODEL": "synthetic",
        "EMBED_API_KEY": _SECRET_MARKER,
        "MCP_AUTH_TOKEN": _SECRET_MARKER + "-xxxxxxxxxxxxxxxx",
        "INFERENCE_MODE": "none",
        "RERANK_MODE": "none",
    }.items():
        monkeypatch.setattr(main_mod, name, value)
    monkeypatch.setattr(main_mod, "run_startup_identity_check", lambda *a, **kw: None)


def test_main_logs_the_identity_line_once(monkeypatch, caplog):
    _configure_main(monkeypatch)
    monkeypatch.setattr(main_mod, "Database", _MainFakeDatabase)
    ran = []
    monkeypatch.setattr(main_mod, "_run_server", lambda *args: ran.append(args))
    caplog.set_level(logging.DEBUG)
    main_mod.main()
    assert ran
    [record] = _identity_records(caplog)
    assert _LINE.fullmatch(record.getMessage())
    assert _SECRET_MARKER not in caplog.text


@pytest.mark.parametrize("stage", ["token", "database"])
def test_identity_is_logged_before_startup_fails(tmp_path, monkeypatch, caplog, stage):
    """A missing token or an index that cannot be opened stops startup,
    and the identity line is already in the log."""
    _configure_main(monkeypatch)
    monkeypatch.setattr(main_mod, "SQLITE_PATH", str(tmp_path / "mail.db"))
    if stage == "token":
        monkeypatch.setattr(main_mod, "MCP_AUTH_TOKEN", "")
    caplog.set_level(logging.DEBUG)
    # The real ``Database`` refuses a missing index file.
    with pytest.raises((ValueError, FileNotFoundError, RuntimeError)):
        main_mod.main()
    [record] = _identity_records(caplog)
    match = _LINE.fullmatch(record.getMessage())
    assert match
    assert match["stored"] == "none"
    assert _SECRET_MARKER not in caplog.text

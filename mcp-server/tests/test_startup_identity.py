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
from src.lib.sqlite import Database

_LINE = re.compile(
    r"Startup identity: service=mcp-server commit=(?P<commit>\S+) "
    r"boot=(?P<boot>[0-9a-f]{12}) schema_stored=(?P<stored>\d+|none) "
    r"config=(?P<config>[0-9a-f]{12})"
)
_SECRET_MARKER = "synthetic-secret-marker-887"  # pragma: allowlist secret
_SECRET_NAMES = ("INFERENCE_API_KEY", "EMBED_API_KEY", "RERANK_API_KEY", "MCP_AUTH_TOKEN")


class _FakeDatabase:
    def __init__(self, version: int | None = 0):
        self.version = version

    def get_schema_version(self) -> int | None:
        return self.version


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(caplog, db=None) -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main_mod._log_startup_identity(db or _FakeDatabase())
    [record] = _identity_records(caplog)
    assert record.levelno == logging.INFO
    match = _LINE.fullmatch(record.getMessage())
    assert match, record.getMessage()
    return match


def test_line_format(monkeypatch, caplog):
    monkeypatch.setenv("GIT_COMMIT", "abc1234-dirty")
    first = _log(caplog, _FakeDatabase(0))
    assert first["commit"] == "abc1234-dirty"
    assert first["stored"] == "0"
    second = _log(caplog, _FakeDatabase(None))
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


class TestGetSchemaVersion:
    """The stored version is read read-only; ``None`` when the index has
    no ``schema_version`` row or table."""

    def _db(self, tmp_path, statements: list[str]) -> Database:
        path = tmp_path / "mail.db"
        with closing(sqlite3.connect(path)) as conn:
            for statement in statements:
                conn.execute(statement)
            conn.commit()
        return Database(str(path))

    def test_returns_the_stored_version(self, tmp_path):
        db = self._db(
            tmp_path,
            [
                "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)",
                "INSERT INTO schema_version VALUES (3)",
            ],
        )
        assert db.get_schema_version() == 3

    def test_empty_table_is_none(self, tmp_path):
        db = self._db(tmp_path, ["CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"])
        assert db.get_schema_version() is None

    def test_missing_table_is_none(self, tmp_path):
        db = self._db(tmp_path, ["CREATE TABLE other (x INTEGER)"])
        assert db.get_schema_version() is None


class _MainFakeDatabase:
    def __init__(self, _path):
        pass

    def get_embedding_dim(self):
        return 4

    def get_schema_version(self):
        return 0


def test_main_logs_the_identity_line_once(monkeypatch, caplog):
    for name, value in {
        "EMBED_BASE_URL": "http://host.docker.internal:8001/v1",
        "EMBED_MODEL": "synthetic",
        "EMBED_API_KEY": _SECRET_MARKER,
        "MCP_AUTH_TOKEN": _SECRET_MARKER + "-xxxxxxxxxxxxxxxx",
        "INFERENCE_MODE": "none",
        "RERANK_MODE": "none",
    }.items():
        monkeypatch.setattr(main_mod, name, value)
    monkeypatch.setattr(main_mod, "Database", _MainFakeDatabase)
    monkeypatch.setattr(main_mod, "run_startup_identity_check", lambda *a, **kw: None)
    ran = []
    monkeypatch.setattr(main_mod, "_run_server", lambda *args: ran.append(args))
    caplog.set_level(logging.DEBUG)
    main_mod.main()
    assert ran
    [record] = _identity_records(caplog)
    assert _LINE.fullmatch(record.getMessage())
    assert _SECRET_MARKER not in caplog.text

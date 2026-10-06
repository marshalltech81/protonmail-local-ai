"""#887: mcp-server logs one INFO line at startup naming the source
commit, a random boot ID, the schema version the index carries and a
hash of the non-secret settings, so log lines from before and after a
rebuild or restart can be told apart.

The line is built only from inputs that cannot fail (raw environment
strings and a read-only, never-raising schema read) and is logged when
``src.main`` is imported, before the module parses any setting, so a
malformed setting, a missing token or a missing index still leaves it
in the log (Codex review rounds 3 and 4 on #893)."""

import ast
import logging
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

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
_SERVICE_DIR = Path(__file__).resolve().parents[1]
_CONFIG_PREFIXES = ("INFERENCE_", "EMBED_", "RERANK_", "MCP_", "SQLITE_", "GIT_COMMIT")


def _identity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Startup identity:")]


def _log(caplog) -> re.Match[str]:
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    main_mod._log_startup_identity()
    [record] = _identity_records(caplog)
    assert record.levelno == logging.INFO
    match = _LINE.fullmatch(record.getMessage())
    assert match, record.getMessage()
    return match


def test_line_format(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GIT_COMMIT", "abc1234-dirty")
    path = tmp_path / "mail.db"
    monkeypatch.setenv("SQLITE_PATH", str(path))
    first = _log(caplog)
    assert first["commit"] == "abc1234-dirty"
    assert first["stored"] == "none"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (0)")
        conn.commit()
    second = _log(caplog)
    assert second["stored"] == "0"
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
        "SQLITE_PATH",
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
        "MCP_TRANSPORT",
        "MCP_SESSION_IDLE_TIMEOUT_SECS",
        "MCP_EXPERIMENTAL_TOOLS",
    }
    assert not names & set(_SECRET_NAMES)


# --- Completeness (Codex review round 5 on #893) -----------------------------
# Every environment variable ``src/`` reads is either a hash input or an
# explicit exclusion with a reason, so a new setting cannot be left out of
# the hash by accident.

_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")
# The service's own env helpers, which take the variable name as a literal
# argument (``_read_secret``'s env fallback, any ``*_env`` helper).
_ENV_HELPERS = {"getenv", "_read_secret"}


def _env_names_read_by_src() -> set[str]:
    """Names in literal reads: ``os.environ.get("X")`` / ``os.getenv`` /
    ``os.environ["X"]`` and the helpers above (any ``*_env`` too)."""
    names: set[str] = set()
    for path in (_SERVICE_DIR / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            candidates: list[ast.expr] = []
            if isinstance(node, ast.Call):
                func = node.func
                fn = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                receiver = ""
                if isinstance(func, ast.Attribute):
                    value = func.value
                    receiver = (
                        value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
                    )
                if (
                    (fn == "get" and receiver in {"environ", "env"})
                    or fn in _ENV_HELPERS
                    or fn.endswith("_env")
                ):
                    candidates = list(node.args)
            elif isinstance(node, ast.Subscript):
                value = node.value
                receiver = (
                    value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
                )
                if receiver in {"environ", "env"}:
                    candidates = [node.slice]
            names.update(
                c.value
                for c in candidates
                if isinstance(c, ast.Constant)
                and isinstance(c.value, str)
                and _ENV_NAME.fullmatch(c.value)
            )
    return names


def test_the_scan_finds_reads_in_every_module():
    # One read per module or helper, so a scan that silently stopped
    # matching would fail here first.
    assert {
        "EMBED_MODEL",  # os.environ.get in main.py
        "MCP_SESSION_IDLE_TIMEOUT_SECS",  # _float_env
        "RERANK_CANDIDATES",  # _int_env
        "MCP_EXPERIMENTAL_TOOLS",  # _flag_env
        "MCP_AUTH_TOKEN",  # _read_secret's env fallback
        "MCP_TRANSPORT",
    } <= _env_names_read_by_src()


def test_every_setting_read_is_hashed_or_excluded():
    read = _env_names_read_by_src()
    missing = read - set(main_mod._IDENTITY_SETTINGS) - set(main_mod._IDENTITY_EXCLUDED)
    assert not missing, f"add to _IDENTITY_SETTINGS or _IDENTITY_EXCLUDED: {sorted(missing)}"


def test_the_lists_name_only_settings_that_are_read_and_do_not_overlap():
    listed = set(main_mod._IDENTITY_SETTINGS) | set(main_mod._IDENTITY_EXCLUDED)
    assert not listed - _env_names_read_by_src()
    assert not set(main_mod._IDENTITY_SETTINGS) & set(main_mod._IDENTITY_EXCLUDED)
    assert len(set(main_mod._IDENTITY_SETTINGS)) == len(main_mod._IDENTITY_SETTINGS)


def test_each_exclusion_has_a_one_line_reason():
    for name, reason in main_mod._IDENTITY_EXCLUDED.items():
        assert reason.strip() and "\n" not in reason, name


def test_every_secret_is_excluded():
    assert set(_SECRET_NAMES) <= set(main_mod._IDENTITY_EXCLUDED)


def test_changing_a_secret_does_not_change_the_hash(monkeypatch, caplog):
    def set_secrets(suffix: str) -> None:
        for name in _SECRET_NAMES:
            monkeypatch.setenv(name, f"{_SECRET_MARKER}-{suffix}")

    set_secrets("a")
    before = _log(caplog)["config"]
    set_secrets("b")
    after = _log(caplog)["config"]
    assert _SECRET_MARKER not in caplog.text
    # A named setting does change it, so the hash is not a constant.
    monkeypatch.setenv("EMBED_MODEL", "synthetic-other-model")
    changed = _log(caplog)["config"]
    assert before == after
    assert changed != before


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SQLITE_PATH", "/synthetic/mail.db"),
        # A malformed value is hashed as given, never parsed.
        ("MCP_PORT", "oops"),
    ],
)
def test_each_raw_value_changes_the_hash(monkeypatch, name, value):
    monkeypatch.delenv(name, raising=False)
    before = main_mod._identity_settings()
    monkeypatch.setenv(name, value)
    after = main_mod._identity_settings()
    assert after[name] == value
    assert main_mod._config_hash(before) != main_mod._config_hash(after)


class TestReadStoredSchemaVersion:
    """The stored version is read read-only and never raises: ``none``
    when the file, table or row is missing, ``unreadable`` when it cannot
    be read."""

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

    def test_a_directory_is_unreadable(self, tmp_path):
        assert read_stored_schema_version(str(tmp_path)) == "unreadable"


def _run_server(tmp_path, args: list[str], env: dict[str, str]):
    """mcp-server in a child process with only the given settings, so a
    malformed one fails it the way it fails the container."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(_CONFIG_PREFIXES)}
    base.update(
        {
            "GIT_COMMIT": "abc1234",
            "SQLITE_PATH": str(tmp_path / "mail.db"),
            "EMBED_BASE_URL": "http://127.0.0.1:9/v1",
            "EMBED_MODEL": "synthetic-embed",
            "EMBED_API_KEY": _SECRET_MARKER,
            "MCP_AUTH_TOKEN": _SECRET_MARKER + "-xxxxxxxxxxxxxxxx",
            **env,
        }
    )
    return subprocess.run(
        [sys.executable, *args],
        cwd=_SERVICE_DIR,
        env=base,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("args", "env", "cause"),
    [
        # Parsed when the module is imported.
        (["-c", "import src.main"], {"MCP_PORT": "oops"}, "ValueError"),
        # Refused inside main(): no bearer token.
        (["-m", "src.main"], {"MCP_AUTH_TOKEN": ""}, "mcp_auth_token"),
        # Refused inside main(): no index file.
        (["-m", "src.main"], {}, "SQLite index not found"),
    ],
)
def test_startup_failures_still_log_the_identity_first(tmp_path, args, env, cause):
    result = _run_server(tmp_path, args, env)
    assert result.returncode != 0
    lines = [line for line in result.stderr.splitlines() if "Startup identity:" in line]
    assert len(lines) == 1, result.stderr
    match = _LINE.search(lines[0])
    assert match, lines[0]
    assert match["stored"] == "none"
    assert result.stderr.index("Startup identity:") < result.stderr.index("Traceback")
    assert cause in result.stderr
    assert _SECRET_MARKER not in result.stderr

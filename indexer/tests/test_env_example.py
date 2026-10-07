"""#920: ``.env.example`` is the operator reference for every environment
variable, and nothing else keeps it complete. This module derives the
variables from the code and checks that every name the indexer or
mcp-server reads, or a Compose file interpolates, is a key in
``.env.example`` (commented or not), unless ``_NOT_DOCUMENTED`` names it
with a reason.

- Python reads come from an ``ast`` scan of both services' ``src/``.
- Compose names come from Compose itself (``docker compose config
  --variables`` over every overlay), not from scanning the YAML. Compose
  reports the outer name of a nested fallback (``${A:-${B}}``) and not
  value-less ``environment:`` entries (``- NAME``), so those are not
  covered.
- The reverse direction (every key is still read) is not checked: it
  needs the shell scripts and the Makefile scanned, which a text scan
  does not do reliably (#929).

It lives in the indexer suite because CI has no repo-root pytest job; it
reads the mcp-server's sources by path."""

import ast
import json
import os
import re
import shutil
import subprocess  # nosec B404 - runs docker compose with a fixed argv
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")
# Project helpers that take the variable name as a literal argument, found
# by grepping both services' ``src/`` for wrappers around ``os.environ``:
# the indexer's ``main._int_env`` / ``_bool_env``, ``queue._int_env``,
# ``embedder._float_env`` and the reconciler's nested ``_int`` / ``_pct``
# / ``_bool`` / ``_mode``; the mcp-server's ``_int_env`` / ``_float_env``
# / ``_flag_env`` and ``_read_secret`` (a secret file with an env
# fallback). Any other ``*_env`` helper is matched by its suffix.
_ENV_HELPERS = {"getenv", "_int", "_pct", "_bool", "_mode", "_float", "_read_secret"}
# ``queue`` and ``reconciler`` read from a mapping parameter named ``env``.
_ENV_RECEIVERS = {"environ", "env"}

# Read by a service or Compose but deliberately not in ``.env.example``.
_NOT_DOCUMENTED = {
    "EMBED_API_KEY": "a secret: Docker secret file; the env fallback is for runs outside a container",  # pragma: allowlist secret
    "INFERENCE_API_KEY": "a secret: Docker secret file; the env fallback is for runs outside a container",  # pragma: allowlist secret
    "RERANK_API_KEY": "a secret: Docker secret file; the env fallback is for runs outside a container",  # pragma: allowlist secret
    "MCP_AUTH_TOKEN": "a secret: Docker secret file; validate-env.sh rejects it in .env",  # pragma: allowlist secret
    "MAILDIR_PATH": "container plumbing: the Compose volume mount path",
    "SQLITE_PATH": "container plumbing: the Compose volume mount path",
    "INDEXER_HEALTH_FILE": "container plumbing: the healthcheck file, a tmpfs default",
    "GIT_COMMIT": "a build arg the Makefile passes, not configuration",
}


def _name_argument(fn: str, call: ast.Call) -> list[ast.expr]:
    """The argument that names the variable, and no other: the first for
    ``get`` / ``getenv``, ``_read_secret``'s ``env_fallback`` (second),
    otherwise a ``name=`` keyword or the first string literal positional
    (``queue._int_env`` takes the env mapping first). A default value is
    never collected."""
    if fn in {"get", "getenv"}:
        return call.args[:1]
    if fn == "_read_secret":
        keyword = [k.value for k in call.keywords if k.arg == "env_fallback"]
        return keyword or call.args[1:2]
    keyword = [k.value for k in call.keywords if k.arg == "name"]
    literals = [a for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return keyword or literals[:1]


def _python_env_reads(src_dirs: list[Path]) -> set[str]:
    """Names in literal reads under ``src_dirs``: ``os.environ.get("X")``,
    ``os.environ["X"]``, ``os.getenv("X")``, ``env.get("X")`` and the
    helpers above."""
    names: set[str] = set()
    for src in src_dirs:
        for path in src.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                candidates: list[ast.expr] = []
                if isinstance(node, ast.Call):
                    func = node.func
                    fn = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    receiver = ""
                    if isinstance(func, ast.Attribute):
                        value = func.value
                        receiver = (
                            value.attr
                            if isinstance(value, ast.Attribute)
                            else getattr(value, "id", "")
                        )
                    if (
                        (fn == "get" and receiver in _ENV_RECEIVERS)
                        or fn in _ENV_HELPERS
                        or fn.endswith("_env")
                    ):
                        candidates = _name_argument(fn, node)
                elif isinstance(node, ast.Subscript):
                    value = node.value
                    receiver = (
                        value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")
                    )
                    if receiver in _ENV_RECEIVERS:
                        candidates = [node.slice]
                names.update(
                    c.value
                    for c in candidates
                    if isinstance(c, ast.Constant)
                    and isinstance(c.value, str)
                    and _ENV_NAME.fullmatch(c.value)
                )
    return names


def _compose_files(repo: Path) -> list[Path]:
    patterns = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
    return sorted({p for pattern in patterns for p in repo.glob(pattern)})


def _compose_variables(repo: Path) -> set[str]:
    """The variables Compose interpolates across every overlay, as Compose
    itself reports them."""
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("docker is required in CI to list the Compose variables")
        pytest.skip("docker is not installed")
    files = [arg for path in _compose_files(repo) for arg in ("-f", str(path))]
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [docker, "compose", *files, "config", "--variables", "--format", "json"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout))


# A key line, set (``VAR=``) or commented out (``# VAR=``).
_EXAMPLE_KEY = re.compile(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _example_keys(path: Path) -> set[str]:
    return set(_EXAMPLE_KEY.findall(path.read_text(encoding="utf-8")))


def _read_names(repo: Path) -> set[str]:
    python = _python_env_reads([repo / "indexer" / "src", repo / "mcp-server" / "src"])
    return python | _compose_variables(repo)


def _undocumented(repo: Path) -> set[str]:
    return _read_names(repo) - _example_keys(repo / ".env.example") - set(_NOT_DOCUMENTED)


# --- The checks --------------------------------------------------------------


def test_every_variable_read_is_in_env_example():
    missing = _undocumented(_REPO)
    assert not missing, f"add to .env.example or _NOT_DOCUMENTED: {sorted(missing)}"


def test_exclusions_are_still_needed_and_reasoned():
    # A stale exclusion would hide a future regression for that name.
    assert set(_NOT_DOCUMENTED) <= _read_names(_REPO)
    assert not set(_NOT_DOCUMENTED) & _example_keys(_REPO / ".env.example")
    for name, reason in _NOT_DOCUMENTED.items():
        assert reason.strip() and "\n" not in reason, name


# --- The scan itself ---------------------------------------------------------


def test_the_scan_finds_reads_from_every_source():
    # One name per reader, so a scan that silently stopped matching one of
    # them fails here rather than passing vacuously.
    python = _python_env_reads([_REPO / "indexer" / "src", _REPO / "mcp-server" / "src"])
    assert {
        "EMBED_MODEL",  # indexer main.py, os.environ.get
        "INDEXER_PARSE_MAX_BYTES",  # parser.py
        "EMBED_WARMUP_TIMEOUT_SECS",  # embedder._float_env
        "INDEXER_MAX_ATTEMPTS",  # queue._int_env on an env mapping
        "INDEXER_DELETION_FORCE",  # reconciler nested helper
        "INFERENCE_MAX_TOKENS",  # mcp-server _int_env
        "INFERENCE_STRUCTURED_OUTPUT",  # mcp-server _flag_env
        "INFERENCE_API_KEY",  # mcp-server _read_secret
    } <= python
    assert {"BRIDGE_USER", "GIT_COMMIT", "MCP_PORT"} <= _compose_variables(_REPO)
    assert len(_example_keys(_REPO / ".env.example")) > 40


@pytest.mark.parametrize(
    "line, expected",
    [
        ("os.environ.get('NEW_SETTING_920')", {"NEW_SETTING_920"}),
        ("os.environ['NEW_SETTING_920']", {"NEW_SETTING_920"}),
        ("os.getenv('NEW_SETTING_920', 'x')", {"NEW_SETTING_920"}),
        ("env.get('NEW_SETTING_920')", {"NEW_SETTING_920"}),
        ("_int_env('NEW_SETTING_920', 1)", {"NEW_SETTING_920"}),
        ("_read_secret('a_secret', 'NEW_SETTING_920')", {"NEW_SETTING_920"}),
        # Only the naming argument, never a default value.
        ("os.getenv('REAL_920', 'LOCAL_DEFAULT_920')", {"REAL_920"}),
        ("os.environ.get('REAL_920', 'LOCAL_DEFAULT_920')", {"REAL_920"}),
        ("_int_env(env, 'REAL_920', 1)", {"REAL_920"}),
        ("_read_secret('A_SECRET_FILE_920', 'REAL_920')", {"REAL_920"}),
        ("_read_secret('A_SECRET_FILE_920', env_fallback='REAL_920')", {"REAL_920"}),
        ("_flag_env(name='REAL_920', default=True)", {"REAL_920"}),
    ],
)
def test_python_read_shapes(tmp_path, line, expected):
    (tmp_path / "m.py").write_text(f"import os\n{line}\n", encoding="utf-8")
    assert _python_env_reads([tmp_path]) == expected


# --- The check catches drift (on a copy, never the real files) ----------------


@pytest.fixture
def repo_copy(tmp_path):
    """The files the check reads, copied, so a test can break one."""
    shutil.copy(_REPO / ".env.example", tmp_path / ".env.example")
    for path in _compose_files(_REPO):
        shutil.copy(path, tmp_path / path.name)
    for sub in ("indexer/src", "mcp-server/src"):
        shutil.copytree(_REPO / sub, tmp_path / sub, ignore=shutil.ignore_patterns("data"))
    assert not _undocumented(tmp_path)
    return tmp_path


def test_a_new_python_read_missing_from_env_example_is_caught(repo_copy):
    (repo_copy / "mcp-server" / "src" / "new_920.py").write_text(
        "import os\nos.environ.get('UNDOCUMENTED_SETTING_920')\n", encoding="utf-8"
    )
    assert _undocumented(repo_copy) == {"UNDOCUMENTED_SETTING_920"}


def test_a_new_compose_variable_missing_from_env_example_is_caught(repo_copy):
    (repo_copy / "docker-compose.extra920.yml").write_text(
        "services:\n  mcp-server:\n    environment:\n      EXTRA: ${UNDOCUMENTED_COMPOSE_920:-1}\n",
        encoding="utf-8",
    )
    assert _undocumented(repo_copy) == {"UNDOCUMENTED_COMPOSE_920"}


def test_a_dropped_env_example_key_is_caught(repo_copy):
    example = repo_copy / ".env.example"
    text = example.read_text(encoding="utf-8")
    assert "\nRERANK_CANDIDATES=" in text
    example.write_text(
        text.replace("\nRERANK_CANDIDATES=", "\nRERANK_CANDIDATEZ="), encoding="utf-8"
    )
    assert "RERANK_CANDIDATES" in _undocumented(repo_copy)

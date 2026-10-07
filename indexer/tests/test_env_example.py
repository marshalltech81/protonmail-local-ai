"""#920: ``.env.example`` is the operator reference for every environment
variable, and nothing else keeps it complete. This module derives the
variables from the code and checks both directions:

- every name the indexer or mcp-server reads, or a Compose file
  interpolates, is a key in ``.env.example`` (commented or not), unless
  ``_NOT_DOCUMENTED`` names it with a reason;
- every ``.env.example`` key is read somewhere (services, Compose, the
  Makefile or a script), unless ``_NOT_READ`` names it with a reason, so
  a removed setting does not leave a dead key behind.

It lives in the indexer suite because CI has no repo-root pytest job; it
reads the mcp-server's sources by path."""

import ast
import re
import shutil
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

# Keys in ``.env.example`` that no service, Compose file, Makefile or
# script reads. Empty today; a name added here needs a reason.
_NOT_READ: dict[str, str] = {}


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
                        candidates = list(node.args)
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


# ``${VAR}``, ``${VAR:-default}``, ``${VAR?err}`` and bare ``$VAR``; a
# ``$$`` escape is not interpolation.
_COMPOSE_REF = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def _compose_files(repo: Path) -> list[Path]:
    patterns = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
    return sorted({p for pattern in patterns for p in repo.glob(pattern)})


def _compose_refs(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            names.update(_COMPOSE_REF.findall(line))
    return names


# A key line, set (``VAR=``) or commented out (``# VAR=``).
_EXAMPLE_KEY = re.compile(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _example_keys(path: Path) -> set[str]:
    return set(_EXAMPLE_KEY.findall(path.read_text(encoding="utf-8")))


def _script_files(repo: Path) -> list[Path]:
    """Operator-run code outside the services: the Makefile and the shell
    scripts (tests excluded, since they set variables rather than read
    them)."""
    return [
        repo / "Makefile",
        *sorted((repo / "scripts").glob("*.sh")),
        *sorted((repo / "mbsync").glob("*.sh")),
    ]


def _mentioned_in(files: list[Path], names: set[str]) -> set[str]:
    text = "\n".join(p.read_text(encoding="utf-8") for p in files)
    return {n for n in names if re.search(rf"\b{re.escape(n)}\b", text)}


def _read_names(repo: Path) -> set[str]:
    return _python_env_reads([repo / "indexer" / "src", repo / "mcp-server" / "src"]) | (
        _compose_refs(_compose_files(repo))
    )


def _undocumented(repo: Path) -> set[str]:
    return _read_names(repo) - _example_keys(repo / ".env.example") - set(_NOT_DOCUMENTED)


def _dead_keys(repo: Path) -> set[str]:
    unread = _example_keys(repo / ".env.example") - _read_names(repo) - set(_NOT_READ)
    return unread - _mentioned_in(_script_files(repo), unread)


# --- The checks --------------------------------------------------------------


def test_every_variable_read_is_in_env_example():
    missing = _undocumented(_REPO)
    assert not missing, f"add to .env.example or _NOT_DOCUMENTED: {sorted(missing)}"


def test_every_env_example_key_is_read():
    dead = _dead_keys(_REPO)
    assert not dead, f"remove from .env.example or add to _NOT_READ: {sorted(dead)}"


def test_exclusions_are_still_needed_and_reasoned():
    read = _read_names(_REPO)
    keys = _example_keys(_REPO / ".env.example")
    # A stale exclusion would hide a future regression for that name.
    assert set(_NOT_DOCUMENTED) <= read
    assert not set(_NOT_DOCUMENTED) & keys
    assert not set(_NOT_READ) - keys
    for name, reason in {**_NOT_DOCUMENTED, **_NOT_READ}.items():
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
    assert {"BRIDGE_USER", "GIT_COMMIT", "MCP_PORT"} <= _compose_refs(_compose_files(_REPO))
    assert len(_example_keys(_REPO / ".env.example")) > 40


@pytest.mark.parametrize(
    "line, expected",
    [
        ("os.environ.get('NEW_SETTING_920')", "NEW_SETTING_920"),
        ("os.environ['NEW_SETTING_920']", "NEW_SETTING_920"),
        ("os.getenv('NEW_SETTING_920', 'x')", "NEW_SETTING_920"),
        ("env.get('NEW_SETTING_920')", "NEW_SETTING_920"),
        ("_int_env('NEW_SETTING_920', 1)", "NEW_SETTING_920"),
        ("_read_secret('a_secret', 'NEW_SETTING_920')", "NEW_SETTING_920"),
    ],
)
def test_python_read_shapes(tmp_path, line, expected):
    (tmp_path / "m.py").write_text(f"import os\n{line}\n", encoding="utf-8")
    assert _python_env_reads([tmp_path]) == {expected}


def test_compose_reference_shapes(tmp_path):
    compose = tmp_path / "docker-compose.extra.yml"
    compose.write_text(
        "services:\n"
        "  a:\n"
        "    environment:\n"
        "      A: ${REF_ONE}\n"
        "      B: ${REF_TWO:-default}\n"
        "      C: $REF_THREE\n"
        "      D: $${NOT_A_REF}\n"
        "      # E: ${COMMENTED_OUT}\n",
        encoding="utf-8",
    )
    assert _compose_files(tmp_path) == [compose]
    assert _compose_refs([compose]) == {"REF_ONE", "REF_TWO", "REF_THREE"}


# --- The check catches drift (on a copy, never the real files) ----------------


@pytest.fixture
def repo_copy(tmp_path):
    """The files the check reads, copied, so a test can break one."""
    for name in (".env.example", "Makefile"):
        shutil.copy(_REPO / name, tmp_path / name)
    for path in _compose_files(_REPO):
        shutil.copy(path, tmp_path / path.name)
    for sub in ("indexer/src", "mcp-server/src"):
        shutil.copytree(_REPO / sub, tmp_path / sub, ignore=shutil.ignore_patterns("data"))
    for sub in ("scripts", "mbsync"):
        (tmp_path / sub).mkdir()
        for path in (_REPO / sub).glob("*.sh"):
            shutil.copy(path, tmp_path / sub / path.name)
    assert not _undocumented(tmp_path)
    assert not _dead_keys(tmp_path)
    return tmp_path


def test_a_new_python_read_missing_from_env_example_is_caught(repo_copy):
    (repo_copy / "mcp-server" / "src" / "new_920.py").write_text(
        "import os\nos.environ.get('UNDOCUMENTED_SETTING_920')\n", encoding="utf-8"
    )
    assert _undocumented(repo_copy) == {"UNDOCUMENTED_SETTING_920"}


def test_a_new_compose_reference_missing_from_env_example_is_caught(repo_copy):
    compose = repo_copy / "docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8") + "# trailing\nx-extra: ${UNDOCUMENTED_COMPOSE_920}\n",
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


def test_a_dead_env_example_key_is_caught(repo_copy):
    example = repo_copy / ".env.example"
    example.write_text(
        example.read_text(encoding="utf-8") + "\n# DEAD_SETTING_920=1\n", encoding="utf-8"
    )
    assert _dead_keys(repo_copy) == {"DEAD_SETTING_920"}


def test_a_key_read_only_by_a_script_is_not_dead(repo_copy):
    example = repo_copy / ".env.example"
    example.write_text(
        example.read_text(encoding="utf-8") + "\nSCRIPT_ONLY_920=1\n", encoding="utf-8"
    )
    script = repo_copy / "scripts" / "validate-env.sh"
    script.write_text(
        script.read_text(encoding="utf-8") + '\n: "${SCRIPT_ONLY_920:-}"\n', encoding="utf-8"
    )
    assert not _dead_keys(repo_copy)

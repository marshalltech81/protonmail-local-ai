"""#920: ``.env.example`` is the operator reference for every environment
variable, and nothing else keeps it complete. This module derives the
variables from the code and checks that every name the indexer or
mcp-server reads, or a Compose file interpolates, is a key in
``.env.example`` (commented or not), unless ``_NOT_DOCUMENTED`` names it
with a reason.

- Python reads come from an ``ast`` scan of both services' ``src/`` for
  calls whose name argument is a string literal, plus the literal
  elements of each service's ``_IDENTITY_SETTINGS`` tuple (read through
  a loop). Any other read whose name is not a literal at the call site,
  outside those named tuples, is not covered.
- Compose names come from Compose itself (``docker compose config
  --variables`` over every overlay), not from scanning the YAML. Compose
  reports the outer name of a nested fallback (``${A:-${B}}``) and not
  value-less ``environment:`` entries (``- NAME``), so those are not
  covered.
- The reverse direction (#929): every ``.env.example`` key must reach a
  service that reads it. For the indexer and mcp-server, every name in
  the service's environment as Compose resolves it (``docker compose
  config --format json`` over every overlay) must be a literal Python
  read in that service's ``src/`` (``_NOT_READ_BY_PYTHON`` names the
  exceptions). A key counts as consumed when its value reaches such an
  environment under its own name (Compose resolves the base file alone,
  as ``make up`` runs it, and with each overlay, every key set to a
  marker) and that service reads it; a
  key a Python service reads must reach that service, so a shared
  setting dropped from one service fails. ``_IDENTITY_SETTINGS``
  entries are not reads here: a name hashed for the config identity but
  no longer used still fails.
  Keys read only by shell (the mbsync scripts and template) are listed
  in ``_SHELL_ONLY`` with their reader; each entry must still be in its
  service's resolved environment and in ``--variables``, but no shell
  is scanned, so deleting the shell read alone is not caught. A shell
  or YAML text scan drew a new edge case every review round in #925.

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
    """The base file first, then every overlay, so later files override
    earlier ones as in the Makefile's ``-f docker-compose.yml -f ...``."""
    patterns = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
    base = repo / "docker-compose.yml"
    overlays = sorted({p for pattern in patterns for p in repo.glob(pattern)} - {base})
    return [base, *overlays]


def _compose_config_json(
    repo: Path, *flags: str, env: dict[str, str] | None = None, files: list[Path] | None = None
):
    """``docker compose config <flags> --format json`` over ``files``
    (default: every file, base first), parsed. ``env`` overrides the named
    variables (a shell variable wins over ``.env``)."""
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("docker is required in CI to read the Compose configuration")
        pytest.skip("docker is not installed")
    paths = _compose_files(repo) if files is None else files
    args = [arg for path in paths for arg in ("-f", str(path))]
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [docker, "compose", *args, "config", *flags, "--format", "json"],
        cwd=repo,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _compose_variables(repo: Path) -> set[str]:
    """The variables Compose interpolates across every overlay, as Compose
    itself reports them."""
    return set(_compose_config_json(repo, "--variables"))


def _compose_combinations(repo: Path) -> list[list[Path]]:
    """The file sets a deployment runs: the base file alone (``make up``)
    and the base file with each overlay, as ``scripts/tests/compose_test.sh``
    checks them."""
    base, *overlays = _compose_files(repo)
    return [[base], *([base, overlay] for overlay in overlays)]


def _service_environments(repo: Path) -> list[dict[str, set[str]]]:
    """Per file combination, each service's environment names as Compose
    resolves them (value-less entries and nested defaults included).
    Names only: the values are never kept."""
    environments = []
    for files in _compose_combinations(repo):
        services = _compose_config_json(repo, files=files)["services"]
        environments.append(
            {name: set(svc.get("environment") or {}) for name, svc in services.items()}
        )
    return environments


def _delivered(repo: Path) -> set[tuple[str, str]]:
    """``(service, KEY)`` for each ``.env.example`` key whose value reaches
    that service's environment under its own name in every file
    combination. Compose resolves the configuration with every key set to
    a distinct numeric marker (a valid port, since ``MCP_PORT`` is
    published), so a fixed value, or a value interpolated from another
    name, does not count. Only the markers are compared; no real ``.env``
    value is kept."""
    keys = sorted(_example_keys(repo / ".env.example"))
    markers = {key: str(40000 + i) for i, key in enumerate(keys)}
    per_combination = []
    for files in _compose_combinations(repo):
        services = _compose_config_json(repo, env=markers, files=files)["services"]
        per_combination.append(
            {
                (service, key)
                for service, svc in services.items()
                for key, value in (svc.get("environment") or {}).items()
                if key in markers and value == markers[key]
            }
        )
    return set.intersection(*per_combination)


# A key line, set (``VAR=``) or commented out (``# VAR=``).
_EXAMPLE_KEY = re.compile(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _example_keys(path: Path) -> set[str]:
    return set(_EXAMPLE_KEY.findall(path.read_text(encoding="utf-8")))


def _literal_name_tuple(path: Path, name: str) -> set[str]:
    """The string elements of the module-level ``name = (...)`` in
    ``path``. Fails if it is missing or not a literal tuple of strings, so
    a rename or rewrite cannot silently drop it from the check."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            value = node.value
            assert isinstance(value, ast.Tuple), f"{path}: {name} is not a literal tuple"
            elements = [e.value for e in value.elts if isinstance(e, ast.Constant)]
            assert len(elements) == len(value.elts) and all(isinstance(e, str) for e in elements), (
                f"{path}: {name} has a non-string-literal element"
            )
            return {str(e) for e in elements}
    raise AssertionError(f"{path}: no module-level {name} assignment")


def _identity_settings(repo: Path) -> set[str]:
    """Names each service reads through a loop over its
    ``_IDENTITY_SETTINGS`` tuple (``os.environ.get(name)``), which the
    literal-argument scan cannot see."""
    return {
        n
        for service in ("indexer", "mcp-server")
        for n in _literal_name_tuple(repo / service / "src" / "main.py", "_IDENTITY_SETTINGS")
    }


def _read_names(repo: Path) -> set[str]:
    python = _python_env_reads([repo / "indexer" / "src", repo / "mcp-server" / "src"])
    return python | _identity_settings(repo) | _compose_variables(repo)


def _undocumented(repo: Path) -> set[str]:
    return _read_names(repo) - _example_keys(repo / ".env.example") - set(_NOT_DOCUMENTED)


# --- The reverse direction (#929) --------------------------------------------

_PYTHON_SERVICES = {"indexer": "indexer/src", "mcp-server": "mcp-server/src"}

# Set in a Python service's Compose environment but read outside its
# ``src/``. Each must still be in that service's resolved environment.
_NOT_READ_BY_PYTHON = {
    "indexer": {
        "HOME": "process plumbing: a fixed Compose value pointing the non-root user's home at its tmpfs, used by the runtime and child tools, not an operator setting",
    },
}

# ``.env.example`` keys read only by shell, as (service, reader). Each
# must still be in that service's resolved environment and in Compose's
# ``--variables``; the shell read itself is not checked.
_SHELL_ONLY = {
    "BRIDGE_USER": ("mbsync", "mbsync/entrypoint.sh, substituted into mbsync/mbsyncrc.template"),
    "BRIDGE_IMAP_PORT": ("mbsync", "mbsync/entrypoint.sh"),
    "BRIDGE_CERT_FINGERPRINT": ("mbsync", "mbsync/entrypoint.sh"),
    "BRIDGE_CERT_PIN_ROTATE": ("mbsync", "mbsync/entrypoint.sh"),
    "SYNC_INTERVAL": ("mbsync", "mbsync/entrypoint.sh and mbsync/healthcheck.sh"),
    "SYNC_DEADLINE_SECONDS": ("mbsync", "mbsync/entrypoint.sh and mbsync/healthcheck.sh"),
}


def _service_python_reads(repo: Path, service: str) -> set[str]:
    """Literal reads in one service's ``src/``; ``_IDENTITY_SETTINGS``
    entries are deliberately not added."""
    return _python_env_reads([repo / _PYTHON_SERVICES[service]])


def _unread_pass_throughs(repo: Path) -> set[str]:
    """``service:NAME`` for each name a Python service's resolved
    environment sets, in any file combination, that its ``src/`` does
    not read: a stale pass-through."""
    environments = _service_environments(repo)
    return {
        f"{service}:{name}"
        for service in _PYTHON_SERVICES
        for name in set().union(*(env[service] for env in environments))
        - _service_python_reads(repo, service)
        - set(_NOT_READ_BY_PYTHON.get(service, {}))
    }


def _consumed(repo: Path) -> set[str]:
    """``.env.example`` keys that reach a Python service that reads them."""
    reads = {service: _service_python_reads(repo, service) for service in _PYTHON_SERVICES}
    return {key for service, key in _delivered(repo) if key in reads.get(service, set())}


def _undelivered_reads(repo: Path) -> set[str]:
    """``service:KEY`` for each ``.env.example`` key a Python service reads
    that does not reach that service: a shared setting dropped from one
    service is caught even while the other still consumes it."""
    delivered = _delivered(repo)
    keys = _example_keys(repo / ".env.example")
    return {
        f"{service}:{key}"
        for service in _PYTHON_SERVICES
        for key in _service_python_reads(repo, service) & keys
        if (service, key) not in delivered
    }


def _dead_keys(repo: Path) -> set[str]:
    """``.env.example`` keys that no service consumes."""
    return _example_keys(repo / ".env.example") - _consumed(repo) - set(_SHELL_ONLY)


def _stale_shell_only(repo: Path) -> set[str]:
    """``_SHELL_ONLY`` entries whose key no longer reaches their service
    under its own name, or that a Python service now consumes (so the
    entry hides nothing)."""
    delivered = _delivered(repo)
    variables = _compose_variables(repo)
    consumed = _consumed(repo)
    return {
        name
        for name, (service, _reader) in _SHELL_ONLY.items()
        if (service, name) not in delivered
        or name not in variables
        or name in consumed
        or name not in _example_keys(repo / ".env.example")
    }


def _stale_not_read_by_python(repo: Path) -> set[str]:
    """``_NOT_READ_BY_PYTHON`` entries missing from their service's
    resolved environment in any file combination, or now read by its
    ``src/``."""
    environments = _service_environments(repo)
    return {
        f"{service}:{name}"
        for service, names in _NOT_READ_BY_PYTHON.items()
        for name in names
        if any(name not in env[service] for env in environments)
        or name in _service_python_reads(repo, service)
    }


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


# --- Review round 7: names read through the identity-settings tuples ---------


def test_the_identity_settings_tuples_are_collected():
    names = _identity_settings(_REPO)
    assert {"MAILDIR_PATH", "EMBED_BATCH_SIZE"} <= names  # indexer
    assert {"INFERENCE_MODE", "RERANK_MODE"} <= names  # mcp-server


@pytest.mark.parametrize(
    "source",
    [
        "OTHER = ('A_920',)\n",  # missing (renamed)
        "_IDENTITY_SETTINGS = tuple(NAMES)\n",  # not a literal
        "_IDENTITY_SETTINGS = ['A_920']\n",  # not a tuple
        "_IDENTITY_SETTINGS = ('A_920', NAME)\n",  # a non-literal element
    ],
)
def test_a_missing_or_non_literal_identity_tuple_fails(tmp_path, source):
    path = tmp_path / "main.py"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="_IDENTITY_SETTINGS"):
        _literal_name_tuple(path, "_IDENTITY_SETTINGS")


def test_a_name_read_only_through_an_identity_tuple_is_caught(repo_copy):
    main = repo_copy / "mcp-server" / "src" / "main.py"
    text = main.read_text(encoding="utf-8")
    assert "_IDENTITY_SETTINGS = (\n" in text
    main.write_text(
        text.replace(
            "_IDENTITY_SETTINGS = (\n", '_IDENTITY_SETTINGS = (\n    "TUPLE_ONLY_920",\n', 1
        ),
        encoding="utf-8",
    )
    assert _undocumented(repo_copy) == {"TUPLE_ONLY_920"}


# --- #929: every .env.example key is still consumed ---------------------------


def test_every_python_service_environment_name_is_read_by_that_service():
    stale = _unread_pass_throughs(_REPO)
    assert not stale, f"remove the Compose pass-through or add a read: {sorted(stale)}"


def test_every_env_example_key_is_consumed():
    dead = _dead_keys(_REPO)
    assert not dead, f"remove from .env.example, or name its reader in _SHELL_ONLY: {sorted(dead)}"


def test_reverse_check_exclusions_are_still_needed_and_reasoned():
    assert not _stale_shell_only(_REPO)
    assert not _stale_not_read_by_python(_REPO)
    for name, (service, reader) in _SHELL_ONLY.items():
        assert service not in _PYTHON_SERVICES, name
        assert reader.strip() and "\n" not in reader, name
    for names in _NOT_READ_BY_PYTHON.values():
        for name, reason in names.items():
            assert reason.strip() and "\n" not in reason, name


def test_the_resolved_environments_are_read():
    # A Compose call that silently returned nothing would pass vacuously.
    for environments in _service_environments(_REPO):
        assert {"EMBED_MODEL", "HOME"} <= environments["indexer"]
        assert {"RERANK_CANDIDATES", "MCP_PORT"} <= environments["mcp-server"]
        assert {"BRIDGE_USER", "SYNC_INTERVAL"} <= environments["mbsync"]


@pytest.fixture
def reverse_copy(repo_copy):
    assert not _unread_pass_throughs(repo_copy)
    assert not _dead_keys(repo_copy)
    assert not _stale_shell_only(repo_copy)
    assert not _stale_not_read_by_python(repo_copy)
    return repo_copy


def test_a_key_only_in_env_example_is_caught(reverse_copy):
    example = reverse_copy / ".env.example"
    example.write_text(
        example.read_text(encoding="utf-8") + "\n# DEAD_SETTING_929=1\n", encoding="utf-8"
    )
    assert _dead_keys(reverse_copy) == {"DEAD_SETTING_929"}


def test_a_removed_read_with_its_pass_through_and_identity_entry_left_is_caught(reverse_copy):
    # The sole operational read goes; the Compose pass-through and the
    # ``_IDENTITY_SETTINGS`` entry stay, as they would after a careless
    # removal. The identity entry must not count as a read.
    main = reverse_copy / "mcp-server" / "src" / "main.py"
    text = main.read_text(encoding="utf-8")
    read = 'RERANK_CANDIDATES = _int_env("RERANK_CANDIDATES", 20, minimum=1)'
    assert text.count(read) == 1 and '    "RERANK_CANDIDATES",\n' in text
    main.write_text(text.replace(read, "RERANK_CANDIDATES = 20"), encoding="utf-8")
    assert _unread_pass_throughs(reverse_copy) == {"mcp-server:RERANK_CANDIDATES"}
    assert _dead_keys(reverse_copy) == {"RERANK_CANDIDATES"}


def test_a_name_read_by_the_other_service_only_does_not_count(reverse_copy):
    # The indexer reads EMBED_BATCH_SIZE; passing it to the mcp-server,
    # which does not, is a stale pass-through for the mcp-server.
    (reverse_copy / "docker-compose.extra929.yml").write_text(
        "services:\n  mcp-server:\n    environment:\n"
        "      EMBED_BATCH_SIZE: ${EMBED_BATCH_SIZE:-}\n",
        encoding="utf-8",
    )
    assert _unread_pass_throughs(reverse_copy) == {"mcp-server:EMBED_BATCH_SIZE"}


def test_a_value_less_environment_entry_is_seen(reverse_copy):
    (reverse_copy / "docker-compose.extra929.yml").write_text(
        "services:\n  indexer:\n    environment:\n      - VALUELESS_929\n", encoding="utf-8"
    )
    assert _unread_pass_throughs(reverse_copy) == {"indexer:VALUELESS_929"}


def test_a_shell_only_entry_dropped_from_compose_is_stale(reverse_copy):
    compose = reverse_copy / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    line = "      BRIDGE_CERT_PIN_ROTATE: ${BRIDGE_CERT_PIN_ROTATE:-false}\n"
    assert text.count(line) == 1
    compose.write_text(text.replace(line, ""), encoding="utf-8")
    assert _stale_shell_only(reverse_copy) == {"BRIDGE_CERT_PIN_ROTATE"}


def test_a_not_read_by_python_entry_dropped_from_compose_is_stale(reverse_copy):
    compose = reverse_copy / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    line = "      HOME: /home/indexer\n"
    assert text.count(line) == 1
    compose.write_text(text.replace(line, ""), encoding="utf-8")
    assert _stale_not_read_by_python(reverse_copy) == {"indexer:HOME"}


# --- Review round 1 ----------------------------------------------------------


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")


_RERANK_CANDIDATES_LINE = "      RERANK_CANDIDATES: ${RERANK_CANDIDATES:-20}\n"


@pytest.mark.parametrize(
    "value",
    [
        '"20"',  # a fixed value: editing the key in .env no longer reaches it
        "${RERANK_CANDIDATEZ_929:-20}",  # interpolated from another name
    ],
)
def test_a_name_set_but_not_from_its_key_is_not_consumed(reverse_copy, value):
    _replace_once(
        reverse_copy / "docker-compose.yml",
        _RERANK_CANDIDATES_LINE,
        f"      RERANK_CANDIDATES: {value}\n",
    )
    assert _dead_keys(reverse_copy) == {"RERANK_CANDIDATES"}
    assert _undelivered_reads(reverse_copy) == {"mcp-server:RERANK_CANDIDATES"}


def test_the_base_compose_file_is_merged_before_its_overlays(reverse_copy):
    # This overlay sorts before docker-compose.yml; merged after it, as
    # every Makefile invocation does, its fixed value wins.
    (reverse_copy / "docker-compose.a929.yml").write_text(
        'services:\n  mcp-server:\n    environment:\n      RERANK_CANDIDATES: "20"\n',
        encoding="utf-8",
    )
    assert _compose_files(reverse_copy)[0].name == "docker-compose.yml"
    assert _dead_keys(reverse_copy) == {"RERANK_CANDIDATES"}


def test_a_read_setting_dropped_from_one_service_is_caught(reverse_copy):
    # Both services read EMBED_MODEL; the indexer still consumes it, so
    # only the per-service edge shows the mcp-server lost it.
    text = (reverse_copy / "docker-compose.yml").read_text(encoding="utf-8")
    line = "      EMBED_MODEL: ${EMBED_MODEL}\n"
    assert text.count(line) == 2
    head, _, tail = text.rpartition(line)
    (reverse_copy / "docker-compose.yml").write_text(head + tail, encoding="utf-8")
    assert _undelivered_reads(reverse_copy) == {"mcp-server:EMBED_MODEL"}
    assert not _dead_keys(reverse_copy)


def test_a_binding_only_an_overlay_supplies_is_caught(reverse_copy):
    # ``make up`` runs the base file alone, so a setting that only an
    # optional overlay passes never reaches the default deployment.
    _replace_once(reverse_copy / "docker-compose.yml", _RERANK_CANDIDATES_LINE, "")
    (reverse_copy / "docker-compose.extra929.yml").write_text(
        "services:\n  mcp-server:\n    environment:\n" + _RERANK_CANDIDATES_LINE,
        encoding="utf-8",
    )
    assert _dead_keys(reverse_copy) == {"RERANK_CANDIDATES"}
    assert _undelivered_reads(reverse_copy) == {"mcp-server:RERANK_CANDIDATES"}


def test_an_exclusion_only_an_overlay_supplies_is_stale(reverse_copy):
    _replace_once(reverse_copy / "docker-compose.yml", "      HOME: /home/indexer\n", "")
    (reverse_copy / "docker-compose.extra929.yml").write_text(
        "services:\n  indexer:\n    environment:\n      HOME: /home/indexer\n",
        encoding="utf-8",
    )
    assert _stale_not_read_by_python(reverse_copy) == {"indexer:HOME"}


def test_every_overlay_combination_is_resolved():
    names = [[p.name for p in files] for files in _compose_combinations(_REPO)]
    assert names == [["docker-compose.yml"], ["docker-compose.yml", "docker-compose.hardened.yml"]]


def test_every_env_example_key_a_python_service_reads_reaches_it():
    missing = _undelivered_reads(_REPO)
    assert not missing, f"pass the key to the service through Compose: {sorted(missing)}"

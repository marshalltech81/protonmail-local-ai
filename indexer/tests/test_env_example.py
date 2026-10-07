"""#920: ``.env.example`` is the operator reference for every environment
variable, and nothing else keeps it complete. This module derives the
variables from the code and checks both directions:

- every name the indexer or mcp-server reads, or a Compose file
  interpolates, is a key in ``.env.example`` (commented or not), unless
  ``_NOT_DOCUMENTED`` names it with a reason;
- every ``.env.example`` key has a reader (a Python service, a value
  Compose itself uses, or an expansion in the mbsync scripts, the
  Makefile or an operator script; a Compose pass-through, a comment or a
  message alone does not count), unless ``_NOT_READ`` names it with a
  reason, so a removed setting does not leave a dead key behind.

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

# Keys in ``.env.example`` with no reader (``_runtime_readers``). Empty
# today; a name added here needs a reason, and a test fails once it is
# read again or leaves ``.env.example``.
_NOT_READ: dict[str, str] = {}


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


# ``${VAR}``, ``${VAR:-default}``, ``${VAR?err}`` and bare ``$VAR``; a
# ``$$`` escape is not interpolation.
_COMPOSE_REF = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def _compose_files(repo: Path) -> list[Path]:
    patterns = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")
    return sorted({p for pattern in patterns for p in repo.glob(pattern)})


# Value-less ``environment:`` entries (``- NAME`` or ``NAME:``), which
# Compose fills from the invoking shell or ``.env`` without a ``$``.
_VALUE_LESS = re.compile(r"^\s*(?:-\s*([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*)\s*:)\s*$")


def _strip_yaml_comment(line: str) -> str:
    """``line`` without a YAML comment: a ``#`` at the start or after
    whitespace, outside quotes."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _compose_refs(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in files:
        env_indent: int | None = None  # set while inside an ``environment:`` block
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = _strip_yaml_comment(raw)
            if not line.strip():
                continue
            if env_indent is not None and _indent(line) <= env_indent:
                env_indent = None
            key = line.strip().split(":", 1)[0]
            if key == "environment":
                value = line.split(":", 1)[1].strip()
                assert not value.startswith(("[", "{")), (
                    f"{path.name}: use a block environment: list or map, not flow-style"
                )
                env_indent = _indent(line)
            elif env_indent is not None and (m := _VALUE_LESS.match(line)):
                names.add(m.group(1) or m.group(2))
            names.update(_COMPOSE_REF.findall(line))
    return names


# A key line, set (``VAR=``) or commented out (``# VAR=``).
_EXAMPLE_KEY = re.compile(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _example_keys(path: Path) -> set[str]:
    return set(_EXAMPLE_KEY.findall(path.read_text(encoding="utf-8")))


# A Compose ``environment:`` entry that only forwards the variable of the
# same name into a container (``NAME: ${NAME:-default}``). It is not a
# read on its own: the container still has to consume it.
_COMPOSE_PASS_THROUGH = re.compile(
    r"""^\s*(?:-\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*["']?\$\{\1(?:[:?+-](.*))?\}["']?\s*$"""
)


def _compose_own_uses(files: list[Path]) -> set[str]:
    """Compose references other than pass-throughs: ports, build args, a
    pass-through's nested default (``${A:-$B}`` reads ``B``) and any other
    value Compose itself consumes."""
    names: set[str] = set()
    for path in files:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = _strip_yaml_comment(raw)
            if not line.strip():
                continue
            if m := _COMPOSE_PASS_THROUGH.match(line):
                names.update(_COMPOSE_REF.findall(m.group(2) or ""))
                continue
            names.update(_COMPOSE_REF.findall(line))
    return names


def _script_files(repo: Path) -> list[Path]:
    """Code outside the Python services that reads settings: every shell
    script one directory down (the services' entrypoints and healthchecks
    and the operator scripts; tests, which set variables rather than read
    them, sit deeper), the mbsync template and the Makefile."""
    scripts = [p for p in repo.glob("*/*.sh") if not p.parent.name.startswith(".")]
    return [repo / "Makefile", *sorted(scripts), repo / "mbsync" / "mbsyncrc.template"]


# A shell or Make expansion: ``$NAME``, ``${NAME...}`` or ``$(NAME)``.
_EXPANSION = re.compile(r"\$[{(]?([A-Za-z_][A-Za-z0-9_]*)")


def _shell_expansions(line: str) -> list[str]:
    """Names Bash would expand on ``line``: not inside single quotes, not
    after a backslash, not in a comment."""
    names: list[str] = []
    in_double = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'" and not in_double:
            end = line.find("'", i + 1)
            i = len(line) if end < 0 else end + 1
            continue
        if ch == '"':
            in_double = not in_double
        elif ch == "#" and not in_double and (i == 0 or line[i - 1].isspace()):
            break
        elif ch == "$" and (m := _EXPANSION.match(line, i)):
            names.append(m.group(1))
        i += 1
    return names


def _script_expansions(files: list[Path]) -> set[str]:
    """Names expanded by the scripts. A comment, or a message that only
    mentions a name, is not a read. Shell scripts follow Bash quoting; the
    Makefile and the template are expanded before any shell sees them, so
    only their comment lines are skipped."""
    names: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if path.suffix == ".sh":
                names.update(_shell_expansions(line))
            elif not line.lstrip().startswith("#"):
                names.update(_EXPANSION.findall(line))
    return names


def _python_reads(repo: Path) -> set[str]:
    return _python_env_reads([repo / "indexer" / "src", repo / "mcp-server" / "src"])


def _read_names(repo: Path) -> set[str]:
    """Forward direction: what an operator can set. Containers get no
    ``env_file``, so a ``.env`` value reaches one only through a Compose
    interpolation; the Python services are also scanned for reads Compose
    does not forward."""
    return _python_reads(repo) | _compose_refs(_compose_files(repo))


def _runtime_readers(repo: Path) -> set[str]:
    """Reverse direction: names something actually consumes. A Compose
    pass-through alone does not count."""
    return (
        _python_reads(repo)
        | _compose_own_uses(_compose_files(repo))
        | _script_expansions(_script_files(repo))
    )


def _undocumented(repo: Path) -> set[str]:
    return _read_names(repo) - _example_keys(repo / ".env.example") - set(_NOT_DOCUMENTED)


def _dead_keys(repo: Path) -> set[str]:
    return _example_keys(repo / ".env.example") - _runtime_readers(repo) - set(_NOT_READ)


def _stale_not_read(repo: Path, not_read: dict[str, str]) -> set[str]:
    """``_NOT_READ`` names that have left ``.env.example`` or gained a
    reader, so the exclusion no longer hides anything."""
    keys = _example_keys(repo / ".env.example")
    return {n for n in not_read if n not in keys or n in _runtime_readers(repo)}


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
    assert not _stale_not_read(_REPO, _NOT_READ)
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
    assert "MCP_PORT" in _compose_own_uses(_compose_files(_REPO))  # the published port
    assert "BRIDGE_USER" not in _compose_own_uses(_compose_files(_REPO))  # a pass-through
    assert {"BRIDGE_CERT_PIN_ROTATE", "SYNC_DEADLINE_SECONDS"} <= _script_expansions(
        _script_files(_REPO)
    )
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
    for path in _script_files(_REPO):
        target = tmp_path / path.relative_to(_REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, target)
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


# --- Review round 1: a mention, a pass-through or a stale exclusion ----------


def _add_key(repo: Path, name: str) -> None:
    example = repo / ".env.example"
    example.write_text(example.read_text(encoding="utf-8") + f"\n{name}=1\n", encoding="utf-8")


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_a_comment_or_message_mention_in_a_script_is_not_a_read(repo_copy):
    _add_key(repo_copy, "MENTIONED_ONLY_920")
    _append(
        repo_copy / "scripts" / "validate-env.sh",
        '\n# MENTIONED_ONLY_920 was removed\necho "remove MENTIONED_ONLY_920 from .env"\n',
    )
    _append(repo_copy / "Makefile", "\n# MENTIONED_ONLY_920\n")
    assert _dead_keys(repo_copy) == {"MENTIONED_ONLY_920"}


def test_a_makefile_expansion_is_a_read(repo_copy):
    _add_key(repo_copy, "MAKE_ONLY_920")
    _append(repo_copy / "Makefile", "\nx:\n\t@echo $(MAKE_ONLY_920)\n")
    assert not _dead_keys(repo_copy)


def test_a_compose_pass_through_without_a_runtime_reader_is_dead(repo_copy):
    _add_key(repo_copy, "PASS_THROUGH_920")
    _append(repo_copy / "docker-compose.yml", "      PASS_THROUGH_920: ${PASS_THROUGH_920:-1}\n")
    assert _dead_keys(repo_copy) == {"PASS_THROUGH_920"}


def test_a_pass_through_read_by_an_mbsync_script_is_not_dead(repo_copy):
    _add_key(repo_copy, "PASS_THROUGH_920")
    _append(repo_copy / "docker-compose.yml", "      PASS_THROUGH_920: ${PASS_THROUGH_920:-1}\n")
    _append(repo_copy / "mbsync" / "entrypoint.sh", '\n: "${PASS_THROUGH_920}"\n')
    assert not _dead_keys(repo_copy)


def test_a_setting_compose_itself_uses_is_not_dead(repo_copy):
    _add_key(repo_copy, "COMPOSE_USE_920")
    _append(repo_copy / "docker-compose.yml", '      - "127.0.0.1:${COMPOSE_USE_920:-1}:3000"\n')
    assert not _dead_keys(repo_copy)


def test_a_not_read_exclusion_for_a_key_that_is_read_is_stale(repo_copy):
    assert _stale_not_read(repo_copy, {"RERANK_CANDIDATES": "x"}) == {"RERANK_CANDIDATES"}
    _add_key(repo_copy, "UNREAD_920")
    assert not _stale_not_read(repo_copy, {"UNREAD_920": "x"})


# --- Review round 2: value-less entries, other scripts, nested defaults ------


def test_value_less_compose_environment_entries_are_reads(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  a:\n"
        "    cap_drop:\n"
        "      - ALL\n"
        "    environment:\n"
        "      - LIST_FORM_920\n"
        "      - SET_920=1\n"
        "  b:\n"
        "    environment:\n"
        "      MAP_FORM_920:\n"
        "      SET_920: x\n"
        "    cap_add:\n"
        "      - NET_RAW\n",
        encoding="utf-8",
    )
    assert _compose_refs([compose]) == {"LIST_FORM_920", "MAP_FORM_920"}


def test_a_flow_style_compose_environment_is_rejected(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  a:\n    environment: [FLOW_920]\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="flow-style"):
        _compose_refs([compose])


def test_every_service_script_is_scanned():
    scanned = set(_script_files(_REPO))
    assert _REPO / "indexer" / "healthcheck.sh" in scanned
    assert _REPO / "mbsync" / "entrypoint.sh" in scanned
    assert not [p for p in scanned if ".semgrep" in p.parts or "tests" in p.parts]


def test_a_key_read_only_by_the_indexer_healthcheck_is_not_dead(repo_copy):
    _add_key(repo_copy, "PROBE_ONLY_920")
    _append(repo_copy / "docker-compose.yml", "      PROBE_ONLY_920: ${PROBE_ONLY_920:-1}\n")
    _append(repo_copy / "indexer" / "healthcheck.sh", '\n: "${PROBE_ONLY_920}"\n')
    assert not _dead_keys(repo_copy)


def test_a_nested_compose_default_is_a_compose_read(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "      PRIMARY_920: ${PRIMARY_920:-$FALLBACK_920}\n"
        "      OTHER_920: ${OTHER_920:-${NESTED_920}}\n"
        "      PLAIN_920: ${PLAIN_920:-1}\n",
        encoding="utf-8",
    )
    assert _compose_own_uses([compose]) == {"FALLBACK_920", "NESTED_920"}


# --- Review round 3: inline comments, literal dollars, argument position ----


def test_inline_yaml_comments_do_not_hide_compose_entries(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  a:\n"
        "    environment:\n"
        "      - LIST_920 # forwarded from the shell\n"
        "      PASS_920: ${PASS_920:-1} # a pass-through\n"
        '      HASH_920: "${HASH_920:-a#b}"\n'
        '    ports: ["127.0.0.1:${PORT_920:-1}:1"] # published\n',
        encoding="utf-8",
    )
    assert _compose_refs([compose]) == {"LIST_920", "PASS_920", "HASH_920", "PORT_920"}
    assert _compose_own_uses([compose]) == {"PORT_920"}


def test_literal_dollars_in_a_script_are_not_reads(tmp_path):
    script = tmp_path / "s.sh"
    script.write_text(
        "echo 'set $SINGLE_QUOTED_920'\n"
        "echo \\$ESCAPED_920\n"
        'echo "it\'s ${DOUBLE_QUOTED_920}"\n'
        'echo "don\'t" $AFTER_QUOTES_920\n'
        "x=${BARE_920:-1} # $COMMENTED_920\n",
        encoding="utf-8",
    )
    assert _script_expansions([script]) == {"DOUBLE_QUOTED_920", "AFTER_QUOTES_920", "BARE_920"}


@pytest.mark.parametrize(
    "line, expected",
    [
        ("os.getenv('REAL_920', 'LOCAL_DEFAULT_920')", {"REAL_920"}),
        ("os.environ.get('REAL_920', 'LOCAL_DEFAULT_920')", {"REAL_920"}),
        ("_int_env(env, 'REAL_920', 1)", {"REAL_920"}),
        ("_read_secret('A_SECRET_FILE_920', 'REAL_920')", {"REAL_920"}),
        ("_read_secret('A_SECRET_FILE_920', env_fallback='REAL_920')", {"REAL_920"}),
        ("_flag_env(name='REAL_920', default=True)", {"REAL_920"}),
    ],
)
def test_only_the_name_argument_is_collected(tmp_path, line, expected):
    (tmp_path / "m.py").write_text(f"import os\n{line}\n", encoding="utf-8")
    assert _python_env_reads([tmp_path]) == expected

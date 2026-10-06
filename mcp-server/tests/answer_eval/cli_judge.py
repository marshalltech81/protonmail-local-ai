"""Subscription CLI judges (#806): the judge through a vendor CLI on the host.

Each judge call runs the CLI once, under the operator's logged-in
subscription instead of a metered API key. Both CLIs are agents, so
every call is isolated to the judge prompt alone, with the prompt on
stdin (never the process list), a fresh empty working directory removed
afterwards, and the process killed if the judge's timeout cancels it.
Failures are fixed text: a CLI's reply can quote the prompt, so none of
its text is kept or logged.

``claude-cli`` runs ``claude -p`` (Claude Code):

- ``--tools ""``: no tools, so injected mail cannot make the judge read
  files or run commands.
- ``--setting-sources ""``: no user, project or local settings, which
  also keeps every ``CLAUDE.md`` (the working directory's, its
  parents' and the operator's own), hooks and plugins out of the call.
- ``--strict-mcp-config`` with no ``--mcp-config``: no MCP servers.
- ``--no-session-persistence`` and the judge's ``--system-prompt``.
- ``--bare`` would do most of this in one flag but accepts only an API
  key, never the subscription login, so it is not used.

Both CLIs get an allowlisted environment (``_ENV_ALLOWLIST``: the
path, home, user, locale, temporary directory, proxy and certificate
variables) and nothing else of the caller's. A strip list kept missing
variables that change the call: ``ANTHROPIC_API_KEY`` takes it off the
subscription (a bad one hangs the CLI), a ``CLAUDE_CODE_USE_*`` switch
routes it through a cloud account, ``CLAUDE_CODE_EFFORT_LEVEL`` or
``MAX_THINKING_TOKENS`` change the judge's reasoning unrecorded, and
``OTEL_*`` telemetry can export the whole prompt. Claude keeps
``CLAUDE_CONFIG_DIR`` (where its login lives) and gets auto-updates
off, so one run cannot mix CLI versions. Claude Code before 2.1.211,
whose ``--setting-sources ""`` still loaded nested ``.claude/rules``
files, is refused. Before the run, ``claude auth status`` (in
the same environment) must report the subscription login. An enterprise
``managed-mcp.json`` (under which ``--strict-mcp-config`` exits at
startup), an organization-wide ``CLAUDE.md`` and any
``managed-settings.json`` (its ``claudeMd``, hooks and other settings
apply whatever the flags) are refused; managed settings delivered by MDM or from the server
cannot be seen from here. ``JUDGE_MAX_TOKENS`` becomes
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``; on hitting it the CLI makes its own
continuation attempts (not ours to turn off) before reporting the cap,
which the judge records as ``judge_truncated``.

``codex-cli`` runs ``codex exec`` (Codex CLI):

- ``--disable`` for the shell (``shell_tool``, ``unified_exec``) and
  every tool or extension that could read the disk or reach the network
  (browser, computer use, apps, plugins, hooks, images, sub-agents,
  goals), and ``web_search="disabled"``. ``apply_patch`` cannot be
  removed (the model's own tool list carries it), but it only writes,
  and ``-s read-only`` refuses the write.
- ``CODEX_HOME`` is a fresh mode-700 directory holding only a symbolic
  link to the operator's ``auth.json``: the global ``AGENTS.md``,
  ``config.toml``, hooks, plugins and skills of the real home never
  load (``--ignore-user-config`` alone still loads the global
  ``AGENTS.md``), and Codex's own logs of the call land there and are
  removed with it. The login is linked, never copied.
- ``tools.experimental_request_user_input.enabled=false``, and the
  permissions, collaboration-mode and environment-context prompt blocks
  off, so the prompt carries only the judge's instructions.
- ``project_doc_max_bytes=0`` (the working directory's ``AGENTS.md``),
  ``--ignore-rules``, ``--ephemeral``, ``--skip-git-repo-check``, no
  update check, the file credential store (the linked ``auth.json``,
  never the keyring), and ``model_instructions_file`` set to the
  judge's system prompt in place of Codex's coding-agent instructions.

Bundled skills are off (``skills.bundled.enabled``,
``skills.include_instructions``; disabling ``skill_search`` alone left
their catalog in the prompt), and so is ``multi_agent_v2``, which a
model's catalog entry can turn on whatever ``multi_agent`` says. Codex
older than 0.160.1, the version these flags were checked against, is
refused. Before the run, ``codex login
status``, in the same kind of private home with the same credential
store, must report a ChatGPT login: an API-key login bills API usage,
and a logged-out CLI still sends the prompt before the 401. Managed
and system config files (``/etc/codex``, macOS managed preferences)
load above or beside the session flags and can add MCP servers, so
they are refused; a cloud-managed enterprise layer cannot be seen from
here. Codex has
no output-token setting, so ``JUDGE_MAX_TOKENS`` does not apply; the
judge's timeout bounds the call.

Known limitation (#827): Codex also registers tools a model's catalog
entry advertises (``experimental_supported_tools``), and no setting
turns those off. In Codex 0.160.1 ``gpt-6-astra``, ``gpt-6-sol`` and
``gpt-6-luna`` advertise ``clock`` and ``send_user_message_async``, so
with them the judge is not tool-free; use a model that advertises none
(``gpt-5.5`` does not).
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from src.lib.inference import InferenceTruncatedError

# The executable each CLI mode runs, and the product it belongs to.
EXECUTABLES = {"claude-cli": "claude", "codex-cli": "codex"}
PRODUCTS = {"claude-cli": "Claude Code", "codex-cli": "Codex"}
EXECUTABLE = EXECUTABLES["claude-cli"]
VERSION_TIMEOUT_SECS = 30.0

# The only caller variables a CLI judge call inherits (plus ``LC_*``):
# what a process needs to find files, its login and the network.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TERM",
        "TZ",
        "LANG",
        "__CF_USER_TEXT_ENCODING",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        # Codex reads its own CA variable before SSL_CERT_FILE.
        "CODEX_CA_CERTIFICATE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
)
# The oldest Codex the judge's flags and features were checked against.
CODEX_MIN_VERSION = (0, 160, 1)
# The first Claude Code whose --setting-sources "" also keeps nested
# .claude/rules files out.
CLAUDE_MIN_VERSION = (2, 1, 211)
# ``claude auth status``'s ``authMethod`` for a Claude subscription.
SUBSCRIPTION_AUTH_METHOD = "claude.ai"
# Where an enterprise ``managed-mcp.json`` lives (macOS, Linux).
MANAGED_MCP_PATHS = (
    Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
    Path("/etc/claude-code/managed-mcp.json"),
)
# Organization-wide instructions that load into every Claude session:
# the managed CLAUDE.md, and ``claudeMd`` in managed settings.
MANAGED_CLAUDE_MD_PATHS = (
    Path("/Library/Application Support/ClaudeCode/CLAUDE.md"),
    Path("/etc/claude-code/CLAUDE.md"),
)
MANAGED_SETTINGS_PATHS = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
)
# Codex config layers that apply whatever the session flags say,
# including the requirements layer (developer instructions, hooks, MCP
# servers and forced features).
CODEX_MANAGED_CONFIG_PATHS = (
    Path("/etc/codex/managed_config.toml"),
    Path("/etc/codex/config.toml"),
    Path("/etc/codex/requirements.toml"),
    Path("/Library/Managed Preferences/com.openai.codex.plist"),
)
# Codex reads only the linked auth.json, never the keyring.
_CODEX_FILE_STORE = 'cli_auth_credentials_store="file"'
# Codex features turned off for a judge call: the shell and everything
# that could read the disk, reach the network or load extensions.
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "apps",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "plugins",
    "remote_plugin",
    "hooks",
    "skill_search",
    "multi_agent",
    "multi_agent_v2",
    "view_image",
    "image_generation",
    "goals",
    "sleep_tool",
    "tool_suggest",
)

_LOGGED_OUT_RE = re.compile(r"not logged in|/login", re.IGNORECASE)
_CODEX_LOGGED_OUT_RE = re.compile(r"401|unauthorized|not logged in", re.IGNORECASE)
# Usage-specific wording only: "Context limit reached" is the model's
# context window, not the subscription.
_USAGE_LIMIT_RE = re.compile(r"usage limit|usage_limit|hit your limit", re.IGNORECASE)
_TOKEN_CAP_RE = re.compile(r"output token maximum", re.IGNORECASE)
# A version number, with any prerelease or build suffix kept.
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")


class CliJudgeError(Exception):
    """A CLI failure with a fixed ``category`` (one of the judge's
    ``JUDGE_ERRORS``) and fixed ``detail`` text."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


# Path-valued variables made absolute: each call runs in a temporary
# directory, where a relative path would name something else.
_PATH_VARS = (
    "HOME",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "CODEX_CA_CERTIFICATE",
)
# Directory lists (OpenSSL's SSL_CERT_DIR is separated like PATH): each
# entry is made absolute on its own and empty entries are dropped.
_PATH_LIST_VARS = ("SSL_CERT_DIR",)


def _allowed_env(*extra: str) -> dict[str, str]:
    """The caller's allowlisted variables, plus ``extra`` names, with
    path-valued ones made absolute."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in _ENV_ALLOWLIST or k.startswith("LC_") or k in extra
    }
    for name in _PATH_VARS:
        if env.get(name, "").strip():
            env[name] = str(Path(env[name]).resolve())
    for name in _PATH_LIST_VARS:
        if env.get(name, "").strip():
            entries = (e for e in env[name].split(os.pathsep) if e.strip())
            env[name] = os.pathsep.join(str(Path(e).resolve()) for e in entries)
    if "PATH" in env:
        # The CLI's launcher may find its interpreter or helpers through
        # PATH: a relative entry, or an empty one (the current directory),
        # is made absolute against this directory; absolute entries stay
        # exactly as given.
        cwd = os.getcwd()
        env["PATH"] = os.pathsep.join(
            e if os.path.isabs(e) else os.path.join(cwd, e) if e else cwd
            for e in env["PATH"].split(os.pathsep)
        )
    return env


def version_at_least(version: str, minimum: tuple[int, ...]) -> bool:
    """``version`` compared with ``minimum``, a prerelease of the
    minimum itself counting as below it; ``False`` when ``version`` has
    no number (``unknown``)."""
    match = re.match(r"(\d+(?:\.\d+)*)(-[^+]*)?", version)
    if not match:
        return False
    numbers = tuple(int(part) for part in match.group(1).split("."))
    if numbers != minimum:
        return numbers > minimum
    return match.group(2) is None


def _not_json(name: str) -> CliJudgeError:
    return CliJudgeError("judge_provider_error", f"{name} CLI output is not the expected JSON")


def platform_supported() -> bool:
    """POSIX only: each call runs in its own process group so a timeout
    can stop everything the CLI started."""
    return os.name == "posix"


def find_executable(name: str = EXECUTABLE) -> str | None:
    """The CLI's absolute path: each call runs in a temporary directory,
    where a relative ``PATH`` entry would no longer resolve."""
    path = shutil.which(name)
    return str(Path(path).resolve()) if path else None


def cli_version(executable: str) -> str:
    """The CLI's version number, or ``unknown``."""
    try:
        out = subprocess.run(  # nosec B603 - fixed argument list, no shell
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECS,
            check=False,
        ).stdout
    except OSError, subprocess.TimeoutExpired:
        return "unknown"
    match = _VERSION_RE.search(out)
    return match.group(0) if match else "unknown"


async def _run_cli(argv: list[str], stdin: str, cwd: str, env: dict[str, str]) -> bytes:
    """Run one CLI call and return its stdout; stderr is discarded. The
    CLI leads its own process group, so a timeout stops every process
    it started (an npm launcher, for one, cannot forward SIGKILL to the
    native binary it runs)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, _ = await proc.communicate(stdin.encode())
    finally:
        # A timeout cancels ``communicate``: stop the CLI too, so it
        # does not keep running (and using the subscription).
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    return stdout


# ------------------------------------------------------------------ claude


def claude_env(max_tokens: int | None = None) -> dict[str, str]:
    """The CLI's environment: the allowlist and ``CLAUDE_CONFIG_DIR``,
    with auto-updates off."""
    env = _allowed_env("CLAUDE_CONFIG_DIR")
    env["DISABLE_AUTOUPDATER"] = "1"
    if max_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    return env


def auth_method(executable: str) -> str | None:
    """``authMethod`` from ``claude auth status`` in the judge's
    environment, or ``None`` when logged out or unreadable."""
    try:
        out = subprocess.run(  # nosec B603 - fixed argument list, no shell
            [executable, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECS,
            check=False,
            env=claude_env(),
        ).stdout
        status = json.loads(out)
    except OSError, subprocess.TimeoutExpired, ValueError:
        return None
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return None
    method = status.get("authMethod")
    return method if isinstance(method, str) else ""


def managed_mcp_present() -> bool:
    return any(path.exists() for path in MANAGED_MCP_PATHS)


def managed_policy_present() -> bool:
    """A managed CLAUDE.md or any managed settings file: both apply to
    every session whatever the flags (instructions, ``claudeMd``,
    hooks that can add context or export the prompt)."""
    return any(path.exists() for path in (*MANAGED_CLAUDE_MD_PATHS, *MANAGED_SETTINGS_PATHS))


class ClaudeCliClient:
    """The judge client for ``claude-cli``: ``complete`` as the
    inference client has it, one isolated ``claude -p`` per call."""

    mode = "claude-cli"
    base_url = ""

    def __init__(
        self,
        *,
        executable: str,
        model: str,
        max_tokens: int,
        workdir_parent: str | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.max_tokens = max_tokens
        self.workdir_parent = workdir_parent
        # Models the CLI reports having served, for the run identity.
        self.served_models: set[str] = set()

    def _argv(self, system: str) -> list[str]:
        return [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--system-prompt",
            system,
            "--strict-mcp-config",
            "--no-session-persistence",
            # No skill or command catalog: an instruction channel outside
            # the system prompt that untrusted evidence could name.
            "--disable-slash-commands",
        ]

    async def complete(self, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix="judge-", dir=self.workdir_parent) as workdir:
            stdout = await _run_cli(self._argv(system), user, workdir, claude_env(self.max_tokens))
        return self._result(stdout)

    def _result(self, stdout: bytes) -> str:
        try:
            data = json.loads(stdout)
        except ValueError:
            raise _not_json("claude") from None
        if not isinstance(data, dict) or not isinstance(data.get("result"), str):
            raise _not_json("claude")
        usage = data.get("modelUsage")
        if isinstance(usage, dict):
            self.served_models.update(k for k in usage if isinstance(k, str))
        result: str = data["result"]
        if data.get("is_error"):
            # Classify the CLI's own message; never keep its text.
            if _LOGGED_OUT_RE.search(result):
                raise CliJudgeError(
                    "judge_cli_logged_out", "claude CLI is not logged in: run `claude` and /login"
                )
            if _USAGE_LIMIT_RE.search(result):
                raise CliJudgeError(
                    "judge_cli_usage_limit", "claude CLI subscription usage limit reached"
                )
            if _TOKEN_CAP_RE.search(result):
                raise InferenceTruncatedError("")
            raise CliJudgeError("judge_provider_error", "claude CLI reported an error")
        if data.get("stop_reason") == "max_tokens":
            raise InferenceTruncatedError(result)
        return result


# ------------------------------------------------------------------ codex


def codex_auth_file() -> str:
    """The operator's Codex login file, absolute (the link to it lives
    in another directory): ``$CODEX_HOME/auth.json``,
    ``~/.codex/auth.json`` by default."""
    home = os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return str(Path(home).resolve() / "auth.json")


def codex_managed_config_present() -> bool:
    return any(path.exists() for path in CODEX_MANAGED_CONFIG_PATHS)


@contextlib.contextmanager
def _codex_private_home(auth_file: str, parent: str | None = None) -> Iterator[str]:
    """A fresh temporary directory (resolved, so ``-C`` names the
    directory the CLI reports) holding ``home``, a mode-700
    ``CODEX_HOME`` with only a link to ``auth_file``, and ``work``, an
    empty mode-700 working directory; removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="judge-", dir=parent) as tmp:
        root = os.path.realpath(tmp)
        for name in ("home", "work"):
            os.mkdir(os.path.join(root, name))
            os.chmod(os.path.join(root, name), 0o700)
        os.symlink(auth_file, os.path.join(root, "home", "auth.json"))
        yield root


def codex_env(home: str | None = None) -> dict[str, str]:
    """The CLI's environment: the allowlist, with ``CODEX_HOME`` set to
    ``home`` when given."""
    env = _allowed_env()
    if home is not None:
        env["CODEX_HOME"] = home
    return env


def codex_login(executable: str, auth_file: str) -> str | None:
    """``chatgpt`` or ``other`` from ``codex login status`` for the login
    in ``auth_file``, read as a judge call reads it (a private home and
    the file store), or ``None`` when logged out or unreadable. Only
    classified: the status text is never kept."""
    try:
        with _codex_private_home(auth_file) as root:
            done = subprocess.run(  # nosec B603 - fixed argument list, no shell
                [executable, "login", "status", "-c", _CODEX_FILE_STORE],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_SECS,
                check=False,
                cwd=os.path.join(root, "work"),
                env=codex_env(os.path.join(root, "home")),
            )
    except OSError, subprocess.TimeoutExpired:
        return None
    out = f"{done.stdout}\n{done.stderr}"
    if re.search(r"logged in using chatgpt", out, re.IGNORECASE):
        return "chatgpt"
    if re.search(r"^\s*logged in", out, re.IGNORECASE | re.MULTILINE):
        return "other"
    return None


class CodexCliClient:
    """The judge client for ``codex-cli``: one isolated ``codex exec``
    per call, in a private ``CODEX_HOME`` that links the login."""

    mode = "codex-cli"
    base_url = ""

    def __init__(
        self,
        *,
        executable: str,
        model: str,
        auth_file: str,
        workdir_parent: str | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.auth_file = auth_file
        self.workdir_parent = workdir_parent

    def _argv(self, workdir: str, instructions: str) -> list[str]:
        argv = [
            self.executable,
            "exec",
            "--json",
            "-m",
            self.model,
            "-s",
            "read-only",
            "-C",
            workdir,
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        for override in (
            'web_search="disabled"',
            "project_doc_max_bytes=0",
            "check_for_update_on_startup=false",
            _CODEX_FILE_STORE,
            "skills.bundled.enabled=false",
            "skills.include_instructions=false",
            "tools.experimental_request_user_input.enabled=false",
            # Only the judge's instructions: no permissions, collaboration
            # mode or environment context (date, working directory) blocks.
            "include_permissions_instructions=false",
            "include_collaboration_mode_instructions=false",
            "include_environment_context=false",
            # A JSON string is a valid TOML basic string.
            f"model_instructions_file={json.dumps(instructions)}",
        ):
            argv += ["-c", override]
        return [*argv, "-"]

    async def complete(self, system: str, user: str) -> str:
        with _codex_private_home(self.auth_file, self.workdir_parent) as root:
            home, workdir = os.path.join(root, "home"), os.path.join(root, "work")
            instructions = os.path.join(home, "judge_system.md")
            Path(instructions).write_text(system, encoding="utf-8")
            stdout = await _run_cli(
                self._argv(workdir, instructions), user, workdir, codex_env(home)
            )
        return self._result(stdout)

    def _result(self, stdout: bytes) -> str:
        """The last agent message of a completed turn, from the
        ``--json`` event stream."""
        messages: list[str] = []
        errors: list[str] = []
        completed = failed = False
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            item = event.get("item")
            if kind == "item.completed" and isinstance(item, dict):
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
            elif kind == "error" and isinstance(event.get("message"), str):
                errors.append(event["message"])
            elif kind == "turn.failed":
                failed = True
                error = event.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    errors.append(error["message"])
            elif kind == "turn.completed":
                completed = True
        if failed:
            # Classify the CLI's own messages; never keep their text.
            text = "\n".join(errors)
            if _USAGE_LIMIT_RE.search(text):
                raise CliJudgeError(
                    "judge_cli_usage_limit", "codex CLI subscription usage limit reached"
                )
            if _CODEX_LOGGED_OUT_RE.search(text):
                raise CliJudgeError(
                    "judge_cli_logged_out", "codex CLI is not logged in: run `codex login`"
                )
            raise CliJudgeError("judge_provider_error", "codex CLI reported an error")
        if not completed or not messages:
            raise _not_json("codex")
        return messages[-1]

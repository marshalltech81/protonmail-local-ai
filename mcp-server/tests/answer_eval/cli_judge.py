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

``ANTHROPIC_*`` and every ``CLAUDE_CODE_USE_*`` provider switch are
removed from the CLI's environment: a set ``ANTHROPIC_API_KEY`` takes
the call off the subscription (a bad one hangs the CLI), and a provider
switch routes it through a cloud account. Auto-updates are off, so one
run cannot mix CLI versions. Before the run, ``claude auth status`` (in
the same environment) must report the subscription login, and an
enterprise ``managed-mcp.json``, under which ``--strict-mcp-config``
exits at startup, is refused. ``JUDGE_MAX_TOKENS`` becomes
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
- ``project_doc_max_bytes=0`` (the working directory's ``AGENTS.md``),
  ``--ignore-rules``, ``--ephemeral``, ``--skip-git-repo-check``, no
  update check, and ``model_instructions_file`` set to the judge's
  system prompt in place of Codex's coding-agent instructions.

``OPENAI_*`` and ``CODEX_*`` variables are removed so an API key cannot
take the call off the subscription. Before the run, ``codex login
status`` must report a ChatGPT login: an API-key login bills API usage,
and a logged-out CLI still sends the prompt before the 401. Codex has
no output-token setting, so ``JUDGE_MAX_TOKENS`` does not apply; the
judge's timeout bounds the call.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.lib.inference import InferenceTruncatedError

# The executable each CLI mode runs, and the product it belongs to.
EXECUTABLES = {"claude-cli": "claude", "codex-cli": "codex"}
PRODUCTS = {"claude-cli": "Claude Code", "codex-cli": "Codex"}
EXECUTABLE = EXECUTABLES["claude-cli"]
VERSION_TIMEOUT_SECS = 30.0

# Variables that would move a Claude call off the subscription login:
# API credentials and endpoints, and every cloud-provider switch.
_STRIPPED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
# The same for Codex: API keys, endpoints and Codex's own settings.
_CODEX_STRIPPED_ENV_PREFIXES = ("OPENAI_", "CODEX_")
# ``claude auth status``'s ``authMethod`` for a Claude subscription.
SUBSCRIPTION_AUTH_METHOD = "claude.ai"
# Where an enterprise ``managed-mcp.json`` lives (macOS, Linux).
MANAGED_MCP_PATHS = (
    Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
    Path("/etc/claude-code/managed-mcp.json"),
)
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
    "view_image",
    "image_generation",
    "goals",
    "sleep_tool",
    "tool_suggest",
)

_LOGGED_OUT_RE = re.compile(r"not logged in|/login", re.IGNORECASE)
_CODEX_LOGGED_OUT_RE = re.compile(r"401|unauthorized|not logged in", re.IGNORECASE)
_USAGE_LIMIT_RE = re.compile(r"usage limit|usage_limit|limit reached|hit your limit", re.IGNORECASE)
_TOKEN_CAP_RE = re.compile(r"output token maximum", re.IGNORECASE)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


class CliJudgeError(Exception):
    """A CLI failure with a fixed ``category`` (one of the judge's
    ``JUDGE_ERRORS``) and fixed ``detail`` text."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


def _not_json(name: str) -> CliJudgeError:
    return CliJudgeError("judge_provider_error", f"{name} CLI output is not the expected JSON")


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
    """Run one CLI call and return its stdout; stderr is discarded."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, _ = await proc.communicate(stdin.encode())
    finally:
        # A timeout cancels ``communicate``: stop the CLI too, so it
        # does not keep running (and using the subscription).
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    return stdout


# ------------------------------------------------------------------ claude


def claude_env(max_tokens: int | None = None) -> dict[str, str]:
    """The CLI's environment: the caller's, minus anything that would
    move the call off the subscription, with auto-updates off."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_STRIPPED_ENV_PREFIXES)}
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
    """The operator's Codex login file: ``$CODEX_HOME/auth.json``,
    ``~/.codex/auth.json`` by default."""
    home = os.environ.get("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    return str(Path(home) / "auth.json")


def codex_env(home: str | None = None) -> dict[str, str]:
    """The CLI's environment: the caller's, minus API keys, endpoints
    and Codex settings; ``CODEX_HOME`` set to ``home`` when given."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_CODEX_STRIPPED_ENV_PREFIXES)}
    if home is not None:
        env["CODEX_HOME"] = home
    return env


def codex_login(executable: str, auth_file: str) -> str | None:
    """``chatgpt`` or ``other`` from ``codex login status`` for the login
    in ``auth_file``'s directory, or ``None`` when logged out or
    unreadable. Only classified: the status text is never kept."""
    try:
        done = subprocess.run(  # nosec B603 - fixed argument list, no shell
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECS,
            check=False,
            env=codex_env(str(Path(auth_file).parent)),
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
            # A JSON string is a valid TOML basic string.
            f"model_instructions_file={json.dumps(instructions)}",
        ):
            argv += ["-c", override]
        return [*argv, "-"]

    async def complete(self, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix="judge-", dir=self.workdir_parent) as tmp:
            # Resolved, so ``-C`` names the directory the CLI reports.
            root = os.path.realpath(tmp)
            home, workdir = os.path.join(root, "home"), os.path.join(root, "work")
            for path in (home, workdir):
                os.mkdir(path)
                os.chmod(path, 0o700)
            os.symlink(self.auth_file, os.path.join(home, "auth.json"))
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

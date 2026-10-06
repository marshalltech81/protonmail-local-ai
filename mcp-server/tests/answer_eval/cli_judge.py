"""``JUDGE_MODE=claude-cli``: the judge through Claude Code on the host (#806).

Each judge call runs ``claude -p`` once, under the operator's logged-in
Claude subscription instead of a metered API key. The CLI is an agent,
so every call is isolated to the judge prompt alone:

- ``--tools ""``: no tools, so injected mail cannot make the judge read
  files or run commands.
- ``--setting-sources ""``: no user, project or local settings, which
  also keeps every ``CLAUDE.md`` (the working directory's, its
  parents' and the operator's own), hooks and plugins out of the call.
- ``--strict-mcp-config`` with no ``--mcp-config``: no MCP servers.
- ``--no-session-persistence``, a fixed ``--system-prompt`` and a fresh
  empty working directory, removed afterwards.
- ``--bare`` would do most of this in one flag but accepts only an API
  key, never the subscription login, so it is not used.

The prompt goes on stdin, never the process list. ``ANTHROPIC_*`` and
the cloud-provider switches are removed from the CLI's environment: a
set ``ANTHROPIC_API_KEY`` takes the call off the subscription (a bad
one hangs the CLI). ``JUDGE_MAX_TOKENS`` becomes
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``; on hitting it the CLI makes its own
continuation attempts (not ours to turn off) before reporting the cap,
which the judge records as ``judge_truncated``.

Failures are fixed text: the CLI's reply can quote the prompt, so none
of its text is kept or logged.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from src.lib.inference import InferenceTruncatedError

EXECUTABLE = "claude"
VERSION_TIMEOUT_SECS = 30.0

# Variables that would move the call off the subscription login.
_STRIPPED_ENV_PREFIXES = ("ANTHROPIC_",)
_STRIPPED_ENV = frozenset(
    {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}
)

_LOGGED_OUT_RE = re.compile(r"not logged in|/login", re.IGNORECASE)
_USAGE_LIMIT_RE = re.compile(r"usage limit|limit reached|hit your limit", re.IGNORECASE)
_TOKEN_CAP_RE = re.compile(r"output token maximum", re.IGNORECASE)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_NOT_JSON = "claude CLI output is not the expected JSON"


class CliJudgeError(Exception):
    """A CLI failure with a fixed ``category`` (one of the judge's
    ``JUDGE_ERRORS``) and fixed ``detail`` text."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


def find_executable() -> str | None:
    return shutil.which(EXECUTABLE)


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

    def _env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(_STRIPPED_ENV_PREFIXES) and k not in _STRIPPED_ENV
        }
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_tokens)
        return env

    async def complete(self, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix="judge-", dir=self.workdir_parent) as workdir:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(system),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=workdir,
                env=self._env(),
            )
            try:
                stdout, _ = await proc.communicate(user.encode())
            finally:
                # A timeout cancels ``communicate``: stop the CLI too, so
                # it does not keep running (and using the subscription).
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        return self._result(stdout)

    def _result(self, stdout: bytes) -> str:
        try:
            data = json.loads(stdout)
        except ValueError:
            raise CliJudgeError("judge_provider_error", _NOT_JSON) from None
        if not isinstance(data, dict) or not isinstance(data.get("result"), str):
            raise CliJudgeError("judge_provider_error", _NOT_JSON)
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

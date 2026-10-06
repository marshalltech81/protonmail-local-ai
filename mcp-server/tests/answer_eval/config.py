"""Provider configuration for the answer evaluation.

Two layers, each in the repository's ``{LAYER}_MODE`` / ``_BASE_URL`` /
``_MODEL`` / ``_API_KEY`` shape and read independently:

- ``INFERENCE_*``: the answering model under test, as the server reads
  it (``anthropic`` or ``openai``; the evaluation needs one, and the
  mode defaults to ``none`` as for the server).
- ``JUDGE_*``: the grader (``anthropic``, ``openai``, ``claude-cli`` or
  ``none``, the default). It never reads the answerer's variables or key file, so the
  judge cannot silently inherit the answerer's provider or credential,
  and there is no fallback between modes.

An enabled layer needs a non-empty model, key and base URL; the base
URL ``default`` means the SDK's documented default (Anthropic API,
OpenAI proper) and an empty one is refused, as for the server's layers
(#750). The key comes from ``<secrets_dir>/<layer>
_api_key.txt``, which must be mode 600, or, for local development only,
the ``{LAYER}_API_KEY`` environment variable. Keys are never logged,
written to a report or taken as a command argument.

``JUDGE_MODE=claude-cli`` (#806) runs the judge through Claude Code on
the host under the operator's subscription (``cli_judge.py``): it takes
``JUDGE_MODEL`` and no key, and ``JUDGE_BASE_URL`` may only be unset or
``default`` (the CLI's login decides the endpoint).
"""

import hashlib
import os
import stat
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src.lib.inference import (
    DEFAULT_COMPLETE_TIMEOUT_SECS,
    InferenceClient,
    default_token_budget,
)

from tests.answer_eval import cli_judge

ENABLED_MODES = frozenset({"anthropic", "openai"})
# Judge-only modes that run a vendor CLI on the host.
CLI_MODES = frozenset({"claude-cli"})
# The variable each SDK reads for its endpoint when none is passed, and
# the host it uses when that variable is unset too.
_SDK_BASE_URL_VARS = {"anthropic": "ANTHROPIC_BASE_URL", "openai": "OPENAI_BASE_URL"}
_SDK_DEFAULT_HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}
# Hosts that keep a provider call on this machine (as ``main.py``'s
# privacy warning counts them).
_HOST_LOCAL = frozenset({"127.0.0.1", "::1", "localhost", "host.docker.internal"})

JUDGE_DEFAULT_TIMEOUT_SECS = 120.0
JUDGE_DEFAULT_MAX_TOKENS = 2048
# Largest judge prompt sent, in characters. A case whose question,
# reference facts, supplied evidence and answer do not fit is reported
# as an incomplete assessment, never graded on part of its evidence.
JUDGE_DEFAULT_MAX_INPUT_CHARS = 60_000


class ConfigError(ValueError):
    """Fixed-text configuration failure; never quotes a value."""


@dataclass(frozen=True)
class LayerConfig:
    layer: str  # "INFERENCE" or "JUDGE"
    mode: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_secs: float
    max_tokens: int
    context_tokens: int
    max_input_chars: int = 0
    # INFERENCE_STRUCTURED_OUTPUT, read as the server reads it (#808).
    structured_output: bool = True
    cli_path: str = ""
    cli_version: str = ""

    def endpoint_kind(self) -> str:
        """``host-local``, ``remote`` or ``sdk-default``: the only form of
        the endpoint a report records (never the URL)."""
        if self.mode in CLI_MODES:
            return "remote"
        if not self.base_url:
            return "sdk-default"
        host = urllib.parse.urlsplit(self.base_url).hostname
        return "host-local" if host in _HOST_LOCAL else "remote"

    def label(self) -> dict[str, object]:
        """Safe run-identity fields: no URL, no key."""
        out: dict[str, object] = {
            "mode": self.mode,
            "model": self.model,
            "endpoint": self.endpoint_kind(),
            # Tells two configured endpoints apart without recording the URL
            # (a hash of the base URL; null for the SDK default).
            "endpoint_id": (
                hashlib.sha256(self.base_url.rstrip("/").encode()).hexdigest()[:12]
                if self.base_url
                else None
            ),
            "max_tokens": self.max_tokens,
            "timeout_secs": self.timeout_secs,
        }
        if self.layer == "INFERENCE":
            out["context_tokens"] = self.context_tokens
        else:
            out["max_input_chars"] = self.max_input_chars
            out["retries"] = 0
            out["concurrency"] = 1
        if self.mode in CLI_MODES:
            out["cli"] = cli_judge.EXECUTABLE
            out["cli_version"] = self.cli_version
        return out

    def client(self) -> InferenceClient | cli_judge.ClaudeCliClient:
        if self.mode in CLI_MODES:
            return cli_judge.ClaudeCliClient(
                executable=self.cli_path, model=self.model, max_tokens=self.max_tokens
            )
        return InferenceClient.create(
            mode=self.mode,
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            max_tokens=self.max_tokens,
            timeout_secs=self.timeout_secs,
            structured_output=self.structured_output,
        )


def _read_key(layer: str, env: Mapping[str, str], secrets_dir: Path | None) -> str:
    name = f"{layer.lower()}_api_key.txt"
    if secrets_dir is not None:
        path = secrets_dir / name
        if path.exists():
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ConfigError(f".secrets/{name} must be mode 600")
            return path.read_text(encoding="utf-8").strip()
    return env.get(f"{layer}_API_KEY", "").strip()


def _number(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum:g}")
    return value


def load_layer(
    layer: str, env: Mapping[str, str] = os.environ, secrets_dir: Path | None = None
) -> LayerConfig | None:
    """The ``layer``'s configuration, or ``None`` when its mode is ``none``.

    Both modes default to ``none``; ``INFERENCE`` must be enabled.
    """
    mode = env.get(f"{layer}_MODE", "none").strip().lower()
    if mode == "none":
        if layer == "INFERENCE":
            raise ConfigError("the answer evaluation needs INFERENCE_MODE=anthropic or openai")
        return None
    if layer == "JUDGE" and mode in CLI_MODES:
        return _load_cli_judge(mode, env)
    if mode not in ENABLED_MODES:
        modes = (
            "anthropic, claude-cli, none, openai" if layer == "JUDGE" else "anthropic, none, openai"
        )
        raise ConfigError(f"{layer}_MODE must be one of: {modes}")
    base_url = env.get(f"{layer}_BASE_URL", "").strip()
    if not base_url:
        raise ConfigError(
            f"{layer}_BASE_URL is empty: set it to the provider's URL, or to `default` "
            f"to use the SDK's default endpoint (sends prompts to {_SDK_DEFAULT_HOSTS[mode]})."
        )
    if base_url.lower() == "default":
        base_url = ""
    if base_url and "@" in urllib.parse.urlsplit(base_url).netloc:
        raise ConfigError(f"{layer}_BASE_URL must not embed credentials (user:pass@host)")
    # With ``default`` the SDK would read its own endpoint variable,
    # and the report would call a custom endpoint "sdk-default".
    ambient = _SDK_BASE_URL_VARS[mode]
    if not base_url and env.get(ambient, "").strip():
        raise ConfigError(f"{ambient} is set: set {layer}_BASE_URL explicitly instead")
    if mode == "anthropic" and base_url.rstrip("/").endswith("/v1"):
        raise ConfigError(f"{layer}_BASE_URL must not end with '/v1' when {layer}_MODE=anthropic")
    model = env.get(f"{layer}_MODEL", "").strip()
    if not model:
        raise ConfigError(f"{layer}_MODEL must be set when {layer}_MODE={mode}")
    api_key = _read_key(layer, env, secrets_dir)
    if not api_key:
        raise ConfigError(
            f"{layer}_MODE={mode} needs a non-empty key in .secrets/{layer.lower()}_api_key.txt "
            "(a placeholder such as 'unauthenticated' for a host-side server)"
        )
    if layer == "INFERENCE":
        timeout = _number(env, "INFERENCE_TIMEOUT_SECS", DEFAULT_COMPLETE_TIMEOUT_SECS, 1.0)
        default_max, default_context = default_token_budget(mode)
        max_tokens = int(_number(env, "INFERENCE_MAX_TOKENS", default_max, 1))
        context = int(_number(env, "INFERENCE_CONTEXT_TOKENS", default_context, 1))
        max_input = 0
        structured = env.get("INFERENCE_STRUCTURED_OUTPUT", "").strip().lower()
        if structured not in {"", "true", "false"}:
            raise ConfigError("INFERENCE_STRUCTURED_OUTPUT must be 'true' or 'false'")
        structured_output = structured != "false"
    else:
        timeout = _number(env, "JUDGE_TIMEOUT_SECS", JUDGE_DEFAULT_TIMEOUT_SECS, 1.0)
        max_tokens = int(_number(env, "JUDGE_MAX_TOKENS", JUDGE_DEFAULT_MAX_TOKENS, 256))
        context = 0
        max_input = int(_number(env, "JUDGE_MAX_INPUT_CHARS", JUDGE_DEFAULT_MAX_INPUT_CHARS, 1000))
        structured_output = False  # the judge sends no schema
    return LayerConfig(
        layer=layer,
        mode=mode,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_secs=timeout,
        max_tokens=max_tokens,
        context_tokens=context,
        max_input_chars=max_input,
        structured_output=structured_output,
    )


def _load_cli_judge(mode: str, env: Mapping[str, str]) -> LayerConfig:
    """``JUDGE_MODE=claude-cli``: a model, the judge bounds and the CLI
    on PATH; no key and no base URL."""
    base_url = env.get("JUDGE_BASE_URL", "").strip()
    if base_url and base_url.lower() != "default":
        raise ConfigError(f"JUDGE_BASE_URL does not apply to JUDGE_MODE={mode}: unset it")
    model = env.get("JUDGE_MODEL", "").strip()
    if not model:
        raise ConfigError(f"JUDGE_MODEL must be set when JUDGE_MODE={mode}")
    path = cli_judge.find_executable()
    if path is None:
        raise ConfigError(f"JUDGE_MODE={mode} needs the claude CLI (Claude Code) on PATH")
    return LayerConfig(
        layer="JUDGE",
        mode=mode,
        base_url="",
        model=model,
        api_key="",
        timeout_secs=_number(env, "JUDGE_TIMEOUT_SECS", JUDGE_DEFAULT_TIMEOUT_SECS, 1.0),
        max_tokens=int(_number(env, "JUDGE_MAX_TOKENS", JUDGE_DEFAULT_MAX_TOKENS, 256)),
        context_tokens=0,
        max_input_chars=int(
            _number(env, "JUDGE_MAX_INPUT_CHARS", JUDGE_DEFAULT_MAX_INPUT_CHARS, 1000)
        ),
        structured_output=False,  # the judge sends no schema
        cli_path=path,
        cli_version=cli_judge.cli_version(path),
    )

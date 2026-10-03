"""Provider configuration for the answer evaluation.

Two layers, each in the repository's ``{LAYER}_MODE`` / ``_BASE_URL`` /
``_MODEL`` / ``_API_KEY`` shape and read independently:

- ``INFERENCE_*``: the answering model under test, as the server reads
  it (``anthropic`` or ``openai``; the evaluation needs one).
- ``JUDGE_*``: the grader (``anthropic``, ``openai`` or ``none``, the
  default). It never reads the answerer's variables or key file, so the
  judge cannot silently inherit the answerer's provider or credential,
  and there is no fallback between modes.

An enabled layer needs a non-empty model and key; an empty base URL
means the SDK's documented default (Anthropic API, OpenAI proper), as
for the server's layers. The key comes from ``<secrets_dir>/<layer>
_api_key.txt``, which must be mode 600, or, for local development only,
the ``{LAYER}_API_KEY`` environment variable. Keys are never logged,
written to a report or taken as a command argument.
"""

import os
import stat
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src.lib.inference import (
    DEFAULT_COMPLETE_TIMEOUT_SECS,
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    InferenceClient,
)

ENABLED_MODES = frozenset({"anthropic", "openai"})
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

    def endpoint_kind(self) -> str:
        """``host-local``, ``remote`` or ``sdk-default``: the only form of
        the endpoint a report records (never the URL)."""
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
            "max_tokens": self.max_tokens,
            "timeout_secs": self.timeout_secs,
        }
        if self.layer == "INFERENCE":
            out["context_tokens"] = self.context_tokens
        else:
            out["max_input_chars"] = self.max_input_chars
            out["retries"] = 0
            out["concurrency"] = 1
        return out

    def client(self) -> InferenceClient:
        return InferenceClient.create(
            mode=self.mode,
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            max_tokens=self.max_tokens,
            timeout_secs=self.timeout_secs,
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

    ``INFERENCE`` must be enabled; ``JUDGE`` defaults to ``none``.
    """
    default_mode = "anthropic" if layer == "INFERENCE" else "none"
    mode = env.get(f"{layer}_MODE", default_mode).strip().lower()
    if mode == "none":
        if layer == "INFERENCE":
            raise ConfigError("the answer evaluation needs INFERENCE_MODE=anthropic or openai")
        return None
    if mode not in ENABLED_MODES:
        raise ConfigError(f"{layer}_MODE must be one of: anthropic, none, openai")
    base_url = env.get(f"{layer}_BASE_URL", "").strip()
    if base_url and "@" in urllib.parse.urlsplit(base_url).netloc:
        raise ConfigError(f"{layer}_BASE_URL must not embed credentials (user:pass@host)")
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
        max_tokens = int(_number(env, "INFERENCE_MAX_TOKENS", DEFAULT_MAX_TOKENS, 1))
        context = int(_number(env, "INFERENCE_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS, 1))
        max_input = 0
    else:
        timeout = _number(env, "JUDGE_TIMEOUT_SECS", JUDGE_DEFAULT_TIMEOUT_SECS, 1.0)
        max_tokens = int(_number(env, "JUDGE_MAX_TOKENS", JUDGE_DEFAULT_MAX_TOKENS, 256))
        context = 0
        max_input = int(_number(env, "JUDGE_MAX_INPUT_CHARS", JUDGE_DEFAULT_MAX_INPUT_CHARS, 1000))
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
    )

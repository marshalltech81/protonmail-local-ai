"""Inference client for the MCP server.

``INFERENCE_MODE`` selects the protocol/SDK:

- ``anthropic`` (default): Anthropic-compatible Messages API via the
  official ``anthropic`` SDK. Unlocks prompt caching, typed tool use,
  streaming, and future API surface (extended thinking, batch, token
  counting) without hand-rolling the wire format.
- ``openai``: OpenAI-compatible chat completions via the official
  ``openai`` SDK. Operator-supplied compat servers (LM Studio, vLLM,
  ``mlx_lm.server``, DeepInfra, OpenRouter, etc.) target the OpenAI
  SDK as their reference client by design, so pointing the SDK at
  them via ``base_url`` is the supported path.
- ``none``: layer disabled. ``main.py`` does not instantiate
  ``InferenceClient`` and the intelligence tool group is not
  registered.

Required-vars validation lives in ``main.py``. There is no fallback
between modes: a misconfigured ``anthropic`` install fails at
startup rather than silently routing to OpenAI.

Each backend imports its SDK lazily inside ``__init__`` so a deployment
that picks one mode never imports the other's SDK — no cold-start cost,
no transitive dependency surface, and no exposure to a future
import-time issue in an SDK the operator isn't using.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("mcp.inference")

# Steady-state ceiling for one completion. Qwen3 in thinking mode can
# run ~1-2 minutes for a long answer; Anthropic Messages calls usually
# return faster but reasoning-heavy prompts can stretch. 300 s catches
# truly stuck calls without false-positiving on slow-but-progressing
# inference. Operators on slow networks can override via
# ``INFERENCE_TIMEOUT_SECS``; resolution happens in ``main.py`` so the
# library code stays env-free for tests.
DEFAULT_COMPLETE_TIMEOUT_SECS = 300.0

# Default ``max_tokens``. The Anthropic Messages API requires the
# field; the OpenAI Chat Completions API accepts it too (most
# OpenAI-compatible servers — vLLM, mlx_lm.server, LM Studio,
# DeepInfra — honor it). 1024 fits brief summaries and per-thread
# extraction; raise for detailed summaries on long threads. Operator
# overrides via ``INFERENCE_MAX_TOKENS``.
DEFAULT_MAX_TOKENS = 1024


class _Backend(Protocol):
    """Structural contract every inference backend satisfies.

    Defined as a ``Protocol`` (not an inheritance base) so future
    backends and test fakes can stay duck-typed without depending on
    any specific SDK.

    ``base_url`` is the wire endpoint after the SDK resolved its
    fallback chain (so the empty-string operator input becomes the
    SDK default literal, not ``""``). ``InferenceClient`` re-exposes
    it so ``main.py`` can log the resolved URL alongside the embed
    and rerank lines — important in a privacy-sensitive deployment
    where the operator needs the startup log to name exactly where
    retrieved email excerpts are being sent.
    """

    base_url: str

    async def complete(self, system: str, user: str) -> str: ...


class _OpenAIBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int,
        timeout_secs: float,
    ) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self.max_tokens = max_tokens
        # ``api_key`` is required (non-empty) — startup validation in
        # ``main.py`` rejects an empty value before reaching this
        # constructor. The key is the explicit-intent signal: an
        # operator with a real ``sk-...`` in
        # ``.secrets/inference_api_key.txt`` has unambiguously chosen
        # their provider, so we trust them to also have set
        # ``base_url`` to the right place (or to have left it empty
        # because they want the SDK default, which is OpenAI proper).
        # For unauthenticated host-side servers (LM Studio, vLLM,
        # ``mlx_lm.server``) the operator supplies any placeholder
        # string; compat servers ignore the bearer header. Keeping the
        # substitution out of this constructor means the audit trail
        # of "what did we send as the credential" is exactly what the
        # operator wrote — no silent rewrite to a literal that could
        # surface in a misconfigured remote provider's request log.
        #
        # ``base_url`` may be empty: an empty value omits the kwarg so
        # the SDK's documented fallback chain fires
        # (``OPENAI_BASE_URL`` env → ``https://api.openai.com/v1``
        # literal). Symmetric with the ``_AnthropicBackend`` empty-URL
        # path and with ``EmbedClient``. Passing an empty string through
        # would defeat the fallback because the SDK only treats
        # ``None`` as "missing." The required ``INFERENCE_API_KEY``
        # upstream is what guards against an accidental ship-to-OpenAI
        # from a forgotten env var.
        #
        # ``max_retries=0`` disables SDK-internal retries so
        # ``timeout_secs`` is the honest wall-clock ceiling for one
        # ``complete()`` call. Default SDK posture (2 attempts +
        # exponential backoff) would silently turn a documented
        # ``INFERENCE_TIMEOUT_SECS=300`` into a ~15 min worst-case hang
        # — hostile to operators tuning the ceiling and to the calling
        # agent, which loses context long before the SDK gives up. On a
        # transient 5xx the tool surfaces a clean error and the agent
        # (or user) can re-invoke. Parity with ``OpenAIEmbedder`` in the
        # indexer, which also pins ``max_retries=0`` (it owns retries
        # above via tenacity; mcp-server has no higher retry layer and
        # deliberately doesn't add one).
        if base_url:
            self.client = AsyncOpenAI(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
            )
        else:
            self.client = AsyncOpenAI(
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
            )
        # After the SDK resolves its fallback chain, read the URL back
        # so ``self.base_url`` always reflects the wire endpoint —
        # useful for log lines that name what the backend is actually
        # talking to.
        self.base_url = str(self.client.base_url).rstrip("/")

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self.max_tokens,
            stream=False,
        )
        # OpenAI-compatible servers occasionally return empty
        # ``choices`` (provider error states, content-filter trips) or a
        # ``message.content`` of ``None``/``""`` (tool-call-only deltas,
        # length-truncated responses). Returning the empty string would
        # surface to the caller as a silent blank answer — the
        # intelligence tools then pass that straight through to the
        # agent, which has no signal that the provider failed. Raise so
        # the operator-facing log line names the failure mode. The
        # message contains no prompt or response content, so logging
        # the exception cannot leak user data.
        if not resp.choices:
            raise RuntimeError("Inference provider returned no choices (mode=openai)")
        content = resp.choices[0].message.content
        if not content:
            raise RuntimeError("Inference provider returned empty content (mode=openai)")
        return content


class _AnthropicBackend:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int,
        timeout_secs: float,
    ) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self.max_tokens = max_tokens
        # The Anthropic SDK appends ``/v1/messages`` to ``base_url``
        # itself. An operator carrying over the pre-collapse
        # ``INFERENCE_ANTHROPIC_BASE_URL=https://api.anthropic.com/v1``
        # would produce a request to ``.../v1/v1/messages`` — every
        # intelligence tool 404s with an opaque SDK error. Reject the
        # ``/v1`` suffix at construction so the operator gets a clear
        # migration message instead of a runtime mystery. Stripping
        # silently would hide the misconfiguration; rejecting forces
        # the operator to confirm they meant the SDK base, not a
        # versioned path.
        stripped = base_url.rstrip("/") if base_url else ""
        if stripped.endswith("/v1"):
            raise ValueError(
                "INFERENCE_BASE_URL must not end with '/v1' when "
                "INFERENCE_MODE=anthropic — the Anthropic SDK appends "
                "'/v1/messages' itself. Drop the trailing '/v1' "
                "(e.g. use 'https://api.anthropic.com', or leave the "
                "var empty to use the SDK default)."
            )
        # Pass ``base_url`` only when explicitly set so the SDK's real
        # default URL is used when the operator left the env var empty
        # (the documented contract for INFERENCE_MODE=anthropic).
        # Passing an empty string would override the SDK default with a
        # malformed URL. ``max_retries=0`` for the same reason as
        # ``_OpenAIBackend``: keep ``timeout_secs`` the honest
        # wall-clock ceiling.
        if stripped:
            self.client = AsyncAnthropic(
                base_url=stripped,
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
            )
        else:
            self.client = AsyncAnthropic(
                api_key=api_key,
                timeout=timeout_secs,
                max_retries=0,
            )
        # After the SDK resolves its fallback chain, read the URL back
        # so ``self.base_url`` always reflects the wire endpoint — the
        # same pattern used by ``_OpenAIBackend``, ``EmbedClient``,
        # and ``OpenAIEmbedder``. Useful for diagnostic log lines that
        # name what the backend is actually talking to.
        self.base_url = str(self.client.base_url).rstrip("/")

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # The Messages API returns a list of content blocks. Concatenate
        # every text block so a future model that emits multiple text
        # blocks (or thinking + text) lands the full answer rather than
        # silently dropping all but the first. Non-text blocks
        # (``tool_use``, ``thinking``) are skipped by the type check.
        # ``getattr`` with a default makes the lookup total over the
        # union of block types without requiring an isinstance ladder
        # for every Anthropic block subclass.
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and getattr(block, "type", None) == "text":
                parts.append(text)
        result = "".join(parts)
        # An empty result means the response contained no text blocks
        # at all (empty ``content``, or only ``tool_use`` / ``thinking``
        # blocks). Returning "" would let the caller pass a silent blank
        # answer to the agent; raise so the failure surfaces with a
        # clear, sanitized error (no prompt/response content) instead.
        # Structured callers that expected JSON get a RuntimeError here
        # rather than a JSONDecodeError two layers down.
        if not result:
            raise RuntimeError("Inference provider returned no text blocks (mode=anthropic)")
        return result


class InferenceClient:
    """Mode-dispatching inference client.

    Instantiate with ``InferenceClient.create(mode, base_url, model,
    api_key)``; the factory raises if the mode is unknown so all
    branches are total. ``mode="none"`` is handled in ``main.py`` —
    this class is only constructed for an active mode.
    """

    def __init__(self, backend: _Backend, mode: str) -> None:
        self._backend = backend
        self.mode = mode
        # Re-expose the backend's resolved wire endpoint so the startup
        # log line in ``main.py`` can name the exact URL retrieved email
        # excerpts are sent to. Mirrors ``EmbedClient.base_url`` — the
        # empty-string operator input is replaced with the SDK's
        # resolved default ("https://api.anthropic.com" /
        # "https://api.openai.com/v1") rather than logged as "SDK default."
        self.base_url = backend.base_url

    @classmethod
    def create(
        cls,
        *,
        mode: str,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_secs: float = DEFAULT_COMPLETE_TIMEOUT_SECS,
    ) -> InferenceClient:
        if mode == "openai":
            return cls(
                _OpenAIBackend(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    max_tokens=max_tokens,
                    timeout_secs=timeout_secs,
                ),
                mode,
            )
        if mode == "anthropic":
            return cls(
                _AnthropicBackend(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    max_tokens=max_tokens,
                    timeout_secs=timeout_secs,
                ),
                mode,
            )
        raise ValueError(f"InferenceClient: unsupported mode {mode!r}")

    async def complete(self, system: str, user: str) -> str:
        return await self._backend.complete(system, user)

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
    """

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

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        # ``api_key`` is required (non-empty) — startup validation in
        # ``main.py`` rejects an empty value before reaching this
        # constructor. For unauthenticated host-side servers (LM Studio,
        # vLLM, ``mlx_lm.server``) the operator supplies any placeholder
        # string in the secret file; the SDK sends it as a bearer token
        # and compat servers ignore it. Keeping the substitution out of
        # this constructor means the audit trail of "what did we send as
        # the credential" is exactly what the operator wrote — no
        # silent rewrite to a literal that could surface in a remote
        # provider's request log if ``INFERENCE_BASE_URL`` were
        # misconfigured.
        #
        # SDK default retry posture (2 attempts with exponential backoff)
        # is kept on the mcp-server side because the query path is a
        # single user-visible request — silently absorbing one transient
        # 5xx prevents a tool-call error the calling agent may not retry.
        # The indexer is structurally different (batch embed loops, custom
        # 4xx-fast / 5xx-retry classification) and owns retries via
        # tenacity there.
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=api_key,
            timeout=timeout_secs,
        )

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

        self.base_url = base_url.rstrip("/") if base_url else ""
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
        if self.base_url.endswith("/v1"):
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
        # malformed URL. SDK default retries (2 attempts, exponential
        # backoff) are kept — see ``_OpenAIBackend`` for the rationale.
        if self.base_url:
            self.client = AsyncAnthropic(
                base_url=self.base_url,
                api_key=api_key,
                timeout=timeout_secs,
            )
        else:
            self.client = AsyncAnthropic(
                api_key=api_key,
                timeout=timeout_secs,
            )

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

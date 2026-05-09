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

import logging

log = logging.getLogger("mcp.inference")

# Steady-state ceiling for one completion. Qwen3 in thinking mode can
# run ~1-2 minutes for a long answer; Anthropic Messages calls usually
# return faster but reasoning-heavy prompts can stretch. 300 s catches
# truly stuck calls without false-positiving on slow-but-progressing
# inference.
_COMPLETE_TIMEOUT_SECS = 300.0
_DEFAULT_MAX_TOKENS = 1024


class _OpenAIBackend:
    def __init__(self, *, base_url: str, model: str, api_key: str) -> None:
        from openai import AsyncOpenAI

        self.model = model
        # ``api_key`` must be a non-empty string for the SDK to construct
        # cleanly; for unauthenticated host-side servers we pass a
        # placeholder. The Authorization header still goes out, but compat
        # servers ignore it. Production providers reject it as expected.
        self.client = AsyncOpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key or "unauthenticated",
            timeout=_COMPLETE_TIMEOUT_SECS,
        )

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=False,
        )
        content = resp.choices[0].message.content or ""
        return content


class _AnthropicBackend:
    def __init__(self, *, base_url: str, model: str, api_key: str) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self.client = AsyncAnthropic(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=_COMPLETE_TIMEOUT_SECS,
        )

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # The Messages API returns a list of content blocks. The first
        # text block is the answer; tool-use blocks are not used here.
        # ``getattr`` with a default makes the lookup total over the
        # union of block types without requiring an isinstance ladder
        # for every Anthropic block subclass.
        for block in resp.content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and getattr(block, "type", None) == "text":
                return text
        return ""


class InferenceClient:
    """Mode-dispatching inference client.

    Instantiate with ``InferenceClient.create(mode, base_url, model,
    api_key)``; the factory raises if the mode is unknown so all
    branches are total. ``mode="none"`` is handled in ``main.py`` —
    this class is only constructed for an active mode.
    """

    def __init__(self, backend: object, mode: str) -> None:
        self._backend = backend
        self.mode = mode

    @classmethod
    def create(cls, *, mode: str, base_url: str, model: str, api_key: str) -> InferenceClient:
        if mode == "openai":
            return cls(_OpenAIBackend(base_url=base_url, model=model, api_key=api_key), mode)
        if mode == "anthropic":
            return cls(_AnthropicBackend(base_url=base_url, model=model, api_key=api_key), mode)
        raise ValueError(f"InferenceClient: unsupported mode {mode!r}")

    async def complete(self, system: str, user: str) -> str:
        return await self._backend.complete(system, user)  # type: ignore[attr-defined]

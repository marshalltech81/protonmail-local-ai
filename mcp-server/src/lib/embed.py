"""Embedding client for the MCP server.

Calls an OpenAI-compatible ``/v1/embeddings`` endpoint via the official
``openai`` SDK with a custom ``base_url`` — the same wire format the
indexer uses, so query vectors and indexed vectors come from the same
provider + model.

``EMBED_MODE`` selects the wire shape:

- ``openai``: this client. Operator-supplied compat servers (LM Studio,
  vLLM, ``mlx_lm.server``, TEI, DeepInfra, OpenRouter, etc.) target
  the OpenAI SDK as their reference client by design, so pointing the
  SDK at them via ``base_url`` is the supported path.
- ``none``: layer disabled. ``main.py`` does not instantiate
  ``EmbedClient`` and search tools refuse semantic / hybrid modes.

The ``openai`` SDK is imported inside ``__init__`` so deployments with
``EMBED_MODE=none`` never pay the import cost — and never depend on
the SDK installing cleanly.
"""

import logging

log = logging.getLogger("mcp.embed")

# Per-call HTTP deadline for embed. A single short string through
# Qwen3-Embedding-8B runs sub-second steady-state; cold-start (first
# call after model load) can take a few seconds. 60 s is generous
# headroom while still bounding a stuck call (per the AGENTS.md rule
# that outbound async HTTP calls must not rely solely on a client-level
# default). Operators on slow networks can override via
# ``EMBED_TIMEOUT_SECS``; resolution happens in ``main.py`` so the
# library code stays env-free for tests.
DEFAULT_EMBED_TIMEOUT_SECS = 60.0


class EmbedClient:
    """Async OpenAI-SDK-backed ``/v1/embeddings`` client."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_secs: float = DEFAULT_EMBED_TIMEOUT_SECS,
    ) -> None:
        # Lazy import: only paid when EMBED_MODE!=none.
        from openai import AsyncOpenAI

        self.base_url = base_url.rstrip("/")
        self.model = model
        # ``api_key`` must be a non-empty string for the SDK to construct
        # cleanly; for unauthenticated host-side servers we pass a
        # placeholder. The Authorization header still goes out, but
        # compat servers that don't require auth ignore it.
        # ``max_retries=0`` matches the indexer's "no implicit SDK
        # retry" posture: the calling agent retries at the tool-
        # invocation layer, so a transient embed failure surfaces
        # cleanly instead of being doubled by built-in SDK backoff.
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=api_key or "unauthenticated",
            timeout=timeout_secs,
            max_retries=0,
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search."""
        resp = await self.client.embeddings.create(
            model=self.model,
            input=text,
        )
        return list(resp.data[0].embedding)

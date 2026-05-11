"""Embedding client for the MCP server.

Calls an OpenAI-compatible ``/v1/embeddings`` endpoint via the official
``openai`` SDK with a custom ``base_url`` — the same wire format the
indexer uses, so query vectors and indexed vectors come from the same
provider + model.

``EMBED_MODE=openai`` is the only valid mode. Operator-supplied compat
servers (LM Studio, vLLM, ``mlx_lm.server``, TEI, DeepInfra, OpenRouter,
etc.) target the OpenAI SDK as their reference client by design, so
pointing the SDK at them via ``base_url`` is the supported path.
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
        from openai import AsyncOpenAI

        self.base_url = base_url.rstrip("/")
        self.model = model
        # ``api_key`` is required (non-empty) — startup validation in
        # ``main.py`` rejects an empty value before reaching here. For
        # unauthenticated host-side servers (LM Studio, vLLM,
        # ``mlx_lm.server``, TEI) the operator supplies any placeholder
        # string in the secret file; the SDK sends it as a bearer token
        # and compat servers ignore it. Keeping the substitution out of
        # this constructor means the credential we actually send is
        # exactly what the operator wrote — no silent rewrite to a
        # literal that could surface in a misconfigured remote
        # provider's request log.
        #
        # SDK default retry posture (2 attempts with exponential backoff)
        # is kept on the mcp-server side because the query path is a
        # single user-visible embed call — silently absorbing one
        # transient 5xx prevents a tool-call error the calling agent
        # may not retry. The indexer side runs custom retry logic via
        # tenacity (``indexer/src/embedder.py``) because its batched
        # embed loop benefits from explicit 4xx-fast / 5xx-retry
        # classification across many texts per call.
        self.client = AsyncOpenAI(
            base_url=self.base_url,
            api_key=api_key,
            timeout=timeout_secs,
        )

    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search."""
        resp = await self.client.embeddings.create(
            model=self.model,
            input=text,
        )
        return list(resp.data[0].embedding)


async def embed_query(client, text: str, expected_dim: int | None) -> list[float]:
    """Embed ``text`` and validate the returned vector matches ``expected_dim``.

    The sqlite-vec ``MATCH`` operator raises an ``OperationalError`` when a
    query vector's dimension doesn't match the indexed vectors, and the
    DB read helpers catch ``(sqlite3.Error, ValueError)`` so they can
    distinguish a missing vec table from a real error. Without this
    boundary check, a misconfigured ``EMBED_MODEL`` whose output dim
    differs from what the indexer wrote silently degrades semantic /
    hybrid search to keyword-only — the operator sees "no results"
    instead of a clear "your embedder is wrong" signal.

    ``expected_dim`` is read from ``Database.get_embedding_dim()`` at
    startup. ``None`` means the vec table doesn't exist yet (fresh
    install pre-indexer-run), in which case there's nothing to compare
    against and we pass the vector through; the DB layer will surface
    the missing-table case naturally.

    Raises ``ValueError`` on mismatch with a message naming the
    operator-controllable knobs (``EMBED_BASE_URL`` / ``EMBED_MODEL``)
    so the fix path is obvious from the log line.
    """
    vector = await client.embed(text)
    if expected_dim is not None and len(vector) != expected_dim:
        raise ValueError(
            f"Embedding dimension mismatch: provider returned {len(vector)}, "
            f"index expects {expected_dim}. Check that EMBED_BASE_URL="
            f"{client.base_url!r} and EMBED_MODEL={client.model!r} match "
            "the embedder the indexer used."
        )
    return vector

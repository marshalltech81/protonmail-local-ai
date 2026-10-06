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
import math

from . import timings
from .security import ProviderResponseError, same_origin_request_hook

log = logging.getLogger("mcp.embed")

# Per-operation HTTP timeout for embed: each connect, read or write
# must make progress within it. It is not a total-call deadline — a
# provider that streams a response in small fragments can keep one call
# alive past it (a total deadline is tracked by #287). A single short
# string through Qwen3-Embedding-8B runs sub-second steady-state;
# cold-start (first call after model load) can take a few seconds. 60 s
# is generous headroom while still catching a stalled call (per the
# AGENTS.md rule that outbound async HTTP calls must not rely solely on
# a client-level default). Operators on slow networks can override via
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
        api_key: str,
        timeout_secs: float = DEFAULT_EMBED_TIMEOUT_SECS,
    ) -> None:
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient

        if not base_url.strip():
            raise ValueError(
                "EmbedClient needs a resolved base URL; startup resolves "
                "EMBED_BASE_URL (or `default`) before building it."
            )
        self.model = model
        # ``api_key`` is required (non-empty) — startup validation in
        # ``main.py`` rejects an empty value before reaching here.
        # For unauthenticated host-side servers (LM Studio, vLLM,
        # ``mlx_lm.server``, TEI) the operator supplies any placeholder
        # string in the secret file; compat servers ignore the bearer
        # header. Keeping the substitution out of this constructor
        # means the credential actually sent is exactly what the
        # operator wrote — no silent rewrite that could surface in a
        # misconfigured remote provider's request log.
        #
        # ``base_url`` must be non-empty and is always passed: left
        # out, the SDK would pick the endpoint itself (``OPENAI_BASE_URL``
        # env, then OpenAI proper), and the body is sent before a
        # provider checks the key (#750, #846). ``main.py`` resolves
        # ``EMBED_BASE_URL`` (``default`` included) before this runs, so
        # an empty value is a caller bug. The error never echoes a
        # value.
        #
        # ``max_retries=0`` disables SDK-internal retries so one
        # ``embed()`` call makes one request and ``timeout_secs`` is
        # not multiplied by retries. It is a per-operation HTTP timeout
        # (each connect, read or write must make progress within it),
        # not a total-call deadline: a provider that streams a response
        # in small fragments can keep the call alive past it (#287).
        # Default SDK posture (2 retries + exponential backoff) would
        # silently stack two more such timeouts plus backoff on every
        # failure — hostile to operators tuning ``EMBED_TIMEOUT_SECS``. On a transient 5xx the query tool surfaces a
        # clean error and the agent (or user) can re-invoke. Parity
        # with ``OpenAIEmbedder`` in the indexer, which also pins
        # ``max_retries=0`` (it owns retries above via tenacity;
        # mcp-server has no higher retry layer and deliberately doesn't
        # add one).
        #
        # The SDK re-sends the body on a redirect; the request hook
        # refuses a hop off the resolved endpoint's origin (#340).
        same_origin_only = same_origin_request_hook(
            lambda: self.client.base_url, "Embed provider", log
        )
        http_client = DefaultAsyncHttpxClient(event_hooks={"request": [same_origin_only]})
        self.client = AsyncOpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=timeout_secs,
            max_retries=0,
            http_client=http_client,
        )
        # Read the URL back from the SDK so ``self.base_url`` reflects
        # the wire endpoint; ``embed_query`` surfaces it in the
        # dim-mismatch error.
        self.base_url = str(self.client.base_url).rstrip("/")

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.close()

    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search."""
        resp = await self.client.embeddings.create(
            model=self.model,
            input=text,
        )
        # Hard-validate response shape before unwrapping. A buggy or
        # not-quite-compatible provider that returns ``data=[]`` would
        # otherwise trip ``IndexError: list index out of range`` on
        # ``resp.data[0]`` — actionable nowhere. Mirror the indexer's
        # batch-cardinality check (``embedder._embed_one_batch``) and
        # surface the operator-controllable knobs (base URL + model)
        # so the fix path is obvious from the log line. The query
        # text is deliberately omitted: it is user input that must
        # not land in logs or tracebacks (mailbox content can flow
        # through query strings).
        if len(resp.data) != 1:
            raise ProviderResponseError(
                f"embedder returned {len(resp.data)} vectors for 1 input "
                f"({self.base_url}, model={self.model!r})"
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
    startup. ``None`` means the helper found no declared dim (a missing
    or unrecognised vec table), in which case there's nothing to compare
    against and we pass the vector through. The server does not start
    on a fresh install the indexer has not built: the embedder identity
    startup check fails closed first.

    Raises ``ProviderResponseError`` on mismatch with a message naming the
    operator-controllable knobs (``EMBED_BASE_URL`` / ``EMBED_MODEL``)
    so the fix path is obvious from the log line.
    """
    with timings.stage("query_embedding"):
        vector = await client.embed(text)
    if expected_dim is not None and len(vector) != expected_dim:
        raise ProviderResponseError(
            f"Embedding dimension mismatch: provider returned {len(vector)}, "
            f"index expects {expected_dim}. Check that EMBED_BASE_URL="
            f"{client.base_url!r} and EMBED_MODEL={client.model!r} match "
            "the embedder the indexer used."
        )
    # A NaN query vector gets a NULL distance to every stored row, so
    # semantic search would silently return nothing (#232).
    if not all(math.isfinite(x) for x in vector):
        raise ProviderResponseError(
            f"Embedding provider returned non-finite values. Check EMBED_BASE_URL="
            f"{client.base_url!r} and EMBED_MODEL={client.model!r}."
        )
    # A zero query vector is the same L2 distance from every stored unit
    # vector, so semantic search would return an arbitrary order (#304).
    if not any(vector):
        raise ProviderResponseError(
            f"Embedding provider returned an all-zero vector. Check EMBED_BASE_URL="
            f"{client.base_url!r} and EMBED_MODEL={client.model!r}."
        )
    return vector

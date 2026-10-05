"""Reranker client for the MCP server.

The hybrid_search RRF stage produces a candidate set ordered by lane
fusion. The reranker re-scores those candidates against the query
using a cross-encoder-style relevance score and returns a sharper
top-K. The cutoff is the caller's ``limit``, passed through
``rerank(..., top_n=limit)``. The candidate count fed in is
``RERANK_CANDIDATES``.

``RERANK_MODE`` selects the provider:

- ``cohere``: Cohere's hosted rerank API via the official ``cohere``
  SDK. ``RERANK_BASE_URL`` is required: ``default`` for the SDK
  default (``https://api.cohere.com``), or a URL for proxies /
  gateways (#750).
- ``none``: rerank disabled. ``main.py`` does not instantiate this
  client and ``hybrid_search`` skips the rerank stage.

Failure handling is best-effort: if the rerank call errors or
returns malformed output, ``rerank()`` returns an empty list and the
caller falls back to the original RRF ordering. Preserves search
results during a rerank outage instead of failing the whole query.

The ``cohere`` SDK is imported inside ``__init__`` so deployments with
``RERANK_MODE=none`` never pay the import cost — and never depend on
the SDK installing cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .security import safe_provider_exception_text

log = logging.getLogger("mcp.reranker")

# Cohere's SDK default is 300s — too long for a synchronous worker
# thread inside ``hybrid_search``. A stalled rerank request would pin
# the pool slot and hang user-visible RAG tools instead of degrading
# cleanly to RRF order. 60s is well above the typical Cohere rerank
# latency (~1–5s) and matches the embed default; operators can tune
# via ``RERANK_TIMEOUT_SECS``. It is a per-operation HTTP timeout (each
# connect, read or write must make progress within it), not a
# total-call deadline: a response streamed in small fragments can keep
# one call alive past it (a total deadline is tracked by #287).
DEFAULT_RERANK_TIMEOUT_SECS = 60.0


@dataclass
class RerankConfig:
    base_url: str
    model: str
    api_key: str
    candidates: int
    timeout_secs: float = DEFAULT_RERANK_TIMEOUT_SECS


class RerankerBackend(Protocol):
    """Minimal contract every reranker implementation satisfies.

    ``candidates`` is the number of RRF results to feed into the
    reranker. The caller passes the cutoff as ``top_n`` on every call
    (``hybrid_search`` passes its ``limit``), so the reranker never
    caps below the caller's request.
    """

    candidates: int

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[tuple[int, float]]:
        """Return ``[(orig_index, score), ...]`` sorted descending by
        score, truncated to ``top_n``.
        Empty list signals failure — caller falls back to the original
        document order, as it does for an out-of-range or repeated
        index."""
        ...


class CohereReranker:
    """Cohere ``rerank`` client using the official SDK.

    Same shape as the other three SDK-using clients (``EmbedClient``,
    ``OpenAIEmbedder``, ``_OpenAIBackend``): ``api_key`` and ``model``
    are required upstream; an empty ``base_url`` means "use the SDK
    default" (``https://api.cohere.com``), which ``main.py`` passes only
    for an explicit ``RERANK_BASE_URL=default`` (#750). Set a URL for
    proxies, gateways, or EU region overrides.
    """

    # ``RERANK_MODE`` value, reported on the per-call timing line.
    mode = "cohere"

    def __init__(self, config: RerankConfig):
        import cohere

        self.config = config
        self.candidates = config.candidates
        # Pass ``base_url`` only when explicitly set — passing an empty
        # string would override the SDK default with a malformed URL.
        # ``timeout`` is always passed: the SDK default (300s) is longer
        # than we want a hybrid_search worker thread to wait on a
        # stalled Cohere call.
        #
        # ``max_retries=0`` disables SDK-internal retries so one
        # ``rerank()`` call makes one request and ``timeout_secs`` (a
        # per-operation HTTP timeout, not a total-call deadline) is not
        # multiplied by retries. The SDK default (2 retries +
        # exponential backoff on 5xx/429 and connection errors) would
        # turn one 503 into three requests and stack three
        # ``RERANK_TIMEOUT_SECS`` timeouts before the RRF fallback
        # (#483). Parity with
        # the inference and embed clients, which also pin
        # ``max_retries=0``; rerank is an optional stage with a
        # fallback, so a retry is the wrong trade.
        if config.base_url:
            self.client = cohere.ClientV2(
                api_key=config.api_key,
                base_url=config.base_url.rstrip("/"),
                timeout=config.timeout_secs,
                max_retries=0,
            )
        else:
            self.client = cohere.ClientV2(
                api_key=config.api_key,
                timeout=config.timeout_secs,
                max_retries=0,
            )
        # Note: ``EmbedClient`` / ``_OpenAIBackend`` / ``OpenAIEmbedder``
        # read back ``self.client.base_url`` after construction so the
        # field reflects the SDK's resolved URL. The Cohere SDK only
        # exposes the resolved URL via ``_client_wrapper.get_base_url()``
        # (private API across SDK versions), so this client doesn't
        # mirror that field. ``main.py`` resolves the endpoint the way
        # the SDK does (configured URL, then ``CO_API_URL``, then
        # ``https://api.cohere.com``) for its log line and privacy
        # warning, without reaching into SDK internals.

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        # Clamp to the candidate count: Cohere rejects top_n > len(documents)
        # with a 400, which would otherwise propagate as a generic rerank
        # failure and silently degrade to RRF. The caller's intent is
        # "give me up to ``top_n``" — when fewer candidates are
        # available, return what we have.
        effective_top_n = min(top_n, len(documents))
        try:
            resp = self.client.rerank(
                model=self.config.model,
                query=query,
                documents=documents,
                top_n=effective_top_n,
            )
            results: list[tuple[int, float]] = []
            for item in resp.results:
                index, score = item.index, item.relevance_score
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not isinstance(score, (int, float))
                    or isinstance(score, bool)
                ):
                    # Never quote the values: they can echo a passage.
                    raise TypeError("rerank result has a non-numeric index or score")
                results.append((index, float(score)))
            return results
        except Exception as exc:
            # Best-effort: log and signal "no rerank available" so the
            # caller can degrade to RRF order rather than fail the query.
            # Every documented failure here can echo the documents
            # (email-chunk text) we just sent: a status error's response
            # body, or a malformed 200 field quoted by a parse or
            # conversion error (#224). Log status errors as ``type +
            # status`` and everything else as its type alone.
            if isinstance(getattr(exc, "status_code", None), int):
                safe_exc = safe_provider_exception_text(exc, [self.config.api_key])
            else:
                safe_exc = type(exc).__name__
            log.warning("rerank failed (%s); falling back to RRF order", safe_exc)
            return []

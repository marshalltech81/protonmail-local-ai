"""Reranker client for the MCP server.

The hybrid_search RRF stage produces a candidate set ordered by lane
fusion. The reranker re-scores those candidates against the query
using a cross-encoder-style relevance score and returns a sharper
top-K. The cutoff is the caller's ``limit`` (passed through
``rerank(..., top_n=limit)``); ``RERANK_TOP_N`` is only the default
applied when a caller doesn't specify one. The candidate count fed
in is ``RERANK_CANDIDATES``.

``RERANK_MODE`` selects the provider:

- ``cohere``: Cohere's hosted rerank API via the official ``cohere``
  SDK. ``RERANK_BASE_URL`` overrides the SDK default for proxies /
  gateways; leave empty to hit the SDK default
  (``https://api.cohere.com``).
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
# via ``RERANK_TIMEOUT_SECS``.
DEFAULT_RERANK_TIMEOUT_SECS = 60.0


@dataclass
class RerankConfig:
    base_url: str
    model: str
    api_key: str
    candidates: int
    top_n: int
    timeout_secs: float = DEFAULT_RERANK_TIMEOUT_SECS


class RerankerBackend(Protocol):
    """Minimal contract every reranker implementation satisfies.

    ``candidates`` is the number of RRF results to feed into the
    reranker. ``top_n`` is the *default* cutoff used when a caller
    doesn't specify one; callers that already know how many results
    they need (e.g. ``hybrid_search(limit=20)``) override it via
    ``rerank(..., top_n=20)`` so the reranker never silently caps
    below the caller's request.
    """

    candidates: int
    top_n: int

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """Return ``[(orig_index, score), ...]`` sorted descending by
        score, truncated to ``top_n`` (or ``self.top_n`` when omitted).
        Empty list signals failure — caller falls back to the original
        document order."""
        ...


class CohereReranker:
    """Cohere ``rerank`` client using the official SDK.

    The base URL is forwarded only when the operator explicitly sets
    ``RERANK_BASE_URL`` (proxies, gateways, EU region overrides). An
    empty value means "use the SDK default" — the cleanest path for
    the standard public endpoint.
    """

    def __init__(self, config: RerankConfig):
        import cohere

        self.config = config
        self.candidates = config.candidates
        self.top_n = config.top_n
        # Pass ``base_url`` only when explicitly set — passing an empty
        # string would override the SDK default with a malformed URL.
        # ``timeout`` is always passed: the SDK default (300s) is longer
        # than we want a hybrid_search worker thread to wait on a
        # stalled Cohere call.
        if config.base_url:
            self.client = cohere.ClientV2(
                api_key=config.api_key,
                base_url=config.base_url.rstrip("/"),
                timeout=config.timeout_secs,
            )
        else:
            self.client = cohere.ClientV2(
                api_key=config.api_key,
                timeout=config.timeout_secs,
            )

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        effective_top_n = top_n if top_n is not None else self.top_n
        # Clamp to the candidate count: Cohere rejects top_n > len(documents)
        # with a 400, which would otherwise propagate as a generic rerank
        # failure and silently degrade to RRF. The caller's intent is
        # "give me up to ``effective_top_n``" — when fewer candidates are
        # available, return what we have.
        effective_top_n = min(effective_top_n, len(documents))
        try:
            resp = self.client.rerank(
                model=self.config.model,
                query=query,
                documents=documents,
                top_n=effective_top_n,
            )
            return [(int(item.index), float(item.relevance_score)) for item in resp.results]
        except Exception as exc:
            # Best-effort: log and signal "no rerank available" so the
            # caller can degrade to RRF order rather than fail the query.
            # ``safe_provider_exception_text`` trims Cohere SDK status
            # errors to ``type + status`` so the response body — which
            # can echo the documents (email-chunk text) we just sent —
            # never lands in operator logs or downstream callers.
            # Connection / timeout failures fall through to the standard
            # secret-redacting formatter and keep diagnostic detail.
            safe_exc = safe_provider_exception_text(exc, [self.config.api_key])
            log.warning("rerank failed (%s); falling back to RRF order", safe_exc)
            return []

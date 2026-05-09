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

log = logging.getLogger("mcp.reranker")


@dataclass
class RerankConfig:
    base_url: str
    model: str
    api_key: str
    candidates: int
    top_n: int


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
        if config.base_url:
            self.client = cohere.ClientV2(
                api_key=config.api_key,
                base_url=config.base_url.rstrip("/"),
            )
        else:
            self.client = cohere.ClientV2(api_key=config.api_key)

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        effective_top_n = top_n if top_n is not None else self.top_n
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
            log.warning("rerank failed (%s); falling back to RRF order", exc)
            return []

"""Reranker client for the host-side ``mlx-service`` (Qwen3-Reranker-4B).

The hybrid_search RRF stage produces a candidate set ordered by lane
fusion. The reranker re-scores those candidates against the query
using a cross-encoder-style yes/no logit comparison, returning a
sharper top-K. The cutoff is the caller's ``limit`` (passed through
``rerank(..., top_n=limit)``); ``RERANK_TOP_N`` is only the default
applied when a caller doesn't specify one. The candidate count fed
in is ``RERANK_CANDIDATES``.

Failure handling is best-effort: if the rerank service errors or
returns malformed output, ``rerank()`` returns an empty list and the
caller is expected to fall back to the original RRF ordering. This
preserves search results during a rerank outage instead of failing
the whole query — the cost is a quality regression, not a hard error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger("mcp.reranker")


@dataclass
class RerankConfig:
    base_url: str
    candidates: int
    top_n: int
    timeout_seconds: float = 120.0


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


class MlxReranker:
    """HTTP client for ``mlx-service`` ``/rerank`` (Qwen3-Reranker-4B)."""

    def __init__(self, config: RerankConfig):
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.candidates = config.candidates
        self.top_n = config.top_n
        self.client = httpx.Client(timeout=config.timeout_seconds)

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
            r = self.client.post(
                f"{self.base_url}/rerank",
                json={
                    "query": query,
                    "documents": documents,
                    "top_n": effective_top_n,
                },
            )
            r.raise_for_status()
            payload = r.json()
            results = payload.get("results", [])
            return [(int(item["index"]), float(item["score"])) for item in results]
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            # Best-effort: log and signal "no rerank available" so the
            # caller can degrade to RRF order rather than fail the query.
            log.warning("rerank failed (%s); falling back to RRF order", exc)
            return []

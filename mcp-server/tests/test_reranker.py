"""Tests for ``src.lib.reranker.CohereReranker``.

The reranker wraps the official ``cohere`` SDK. Tests monkey-patch
``client.rerank`` so the public contract (``[(orig_index, score), ...]``
sorted descending, empty list on failure) is exercised without
hitting Cohere's hosted API.
"""

from types import SimpleNamespace

from src.lib.reranker import CohereReranker, RerankConfig


def _make_reranker(top_n: int = 5, candidates: int = 50) -> CohereReranker:
    return CohereReranker(
        RerankConfig(
            base_url="",
            model="rerank-v4.0-pro",
            api_key="ck-test",  # pragma: allowlist secret
            candidates=candidates,
            top_n=top_n,
        )
    )


def _result(index: int, score: float) -> SimpleNamespace:
    """Match the SDK's ``RerankResponseResultsItem`` shape with the
    only two fields ``CohereReranker.rerank`` reads."""
    return SimpleNamespace(index=index, relevance_score=score)


class TestRerank:
    def test_returns_indexed_scores_in_response_order(self):
        r = _make_reranker()
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                results=[_result(2, 0.9), _result(0, 0.4), _result(1, 0.1)],
            )

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        out = r.rerank("q", ["a", "b", "c"])
        assert out == [(2, 0.9), (0, 0.4), (1, 0.1)]
        # Caller-supplied ``top_n`` defaults to ``self.top_n`` when not
        # provided — the reranker must never silently cap below the
        # caller's requested limit.
        assert captured["top_n"] == 5
        assert captured["query"] == "q"
        assert captured["documents"] == ["a", "b", "c"]

    def test_caller_top_n_overrides_default(self):
        r = _make_reranker(top_n=5)
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(results=[])

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        r.rerank("q", ["a"], top_n=20)
        assert captured["top_n"] == 20

    def test_empty_documents_short_circuits_without_calling_sdk(self):
        r = _make_reranker()
        called = {"n": 0}

        def fake_rerank(**_kwargs):
            called["n"] += 1
            return SimpleNamespace(results=[])

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        assert r.rerank("q", []) == []
        assert called["n"] == 0

    def test_sdk_exception_returns_empty_for_graceful_degradation(self):
        # Reranker failures must not abort the search query — the
        # caller falls back to the original RRF order. Pinning this
        # behavior prevents a Cohere outage from taking down search.
        r = _make_reranker()

        def fake_rerank(**_kwargs):
            raise RuntimeError("simulated cohere outage")

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        assert r.rerank("q", ["a", "b"]) == []

    def test_malformed_score_returns_empty(self):
        # If the SDK ever returns a score that can't be coerced to
        # float, fall back to RRF order rather than propagating a
        # ValueError up through hybrid_search.
        r = _make_reranker()

        def fake_rerank(**_kwargs):
            return SimpleNamespace(
                results=[SimpleNamespace(index=0, relevance_score="not-a-number")],
            )

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        assert r.rerank("q", ["a"]) == []

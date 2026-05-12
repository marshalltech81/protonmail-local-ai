"""Tests for ``src.lib.reranker.CohereReranker``.

The reranker wraps the official ``cohere`` SDK. Tests monkey-patch
``client.rerank`` so the public contract (``[(orig_index, score), ...]``
sorted descending, empty list on failure) is exercised without
hitting Cohere's hosted API.
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.lib.reranker import (
    DEFAULT_RERANK_TIMEOUT_SECS,
    CohereReranker,
    RerankConfig,
)


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
        # Five documents so the default ``top_n=5`` flows through
        # unclamped — the clamp behavior has its own dedicated test.
        out = r.rerank("q", ["a", "b", "c", "d", "e"])
        assert out == [(2, 0.9), (0, 0.4), (1, 0.1)]
        # Caller-supplied ``top_n`` defaults to ``self.top_n`` when not
        # provided — the reranker must never silently cap below the
        # caller's requested limit when the candidate set supports it.
        assert captured["top_n"] == 5
        assert captured["query"] == "q"
        assert captured["documents"] == ["a", "b", "c", "d", "e"]

    def test_top_n_clamped_to_document_count(self):
        # Cohere rejects ``top_n > len(documents)`` with a 400, which
        # would otherwise propagate as a generic rerank failure and
        # silently degrade to RRF. The reranker clamps before the call
        # so the caller's "give me up to N" intent is honored against
        # smaller candidate sets.
        r = _make_reranker(top_n=20)
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(results=[_result(0, 0.5), _result(1, 0.3)])

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        r.rerank("q", ["a", "b"])
        assert captured["top_n"] == 2

        # Same clamp applies when the caller explicitly overrides top_n.
        captured.clear()
        r.rerank("q", ["a", "b"], top_n=50)
        assert captured["top_n"] == 2

    def test_caller_top_n_overrides_default(self):
        r = _make_reranker(top_n=5)
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(results=[])

        # Provide enough documents that ``top_n=20`` survives the
        # ``min(top_n, len(documents))`` clamp — this test is about
        # the caller-override path, not the clamp.
        documents = [f"doc-{i}" for i in range(20)]
        r.client.rerank = fake_rerank  # type: ignore[assignment]
        r.rerank("q", documents, top_n=20)
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

    def test_empty_base_url_omits_kwarg_so_sdk_default_applies(self):
        # ``RERANK_BASE_URL=""`` means "use the SDK default"
        # (``https://api.cohere.com``). The required non-empty
        # ``RERANK_API_KEY`` upstream is the explicit-intent signal —
        # an operator with a real Cohere key has unambiguously chosen
        # their provider, so we trust the documented SDK fallback.
        # Symmetric with how ``EmbedClient``, ``OpenAIEmbedder``, and
        # ``_OpenAIBackend`` treat empty base URLs.
        #
        # The base_url kwarg must be GENUINELY ABSENT from the SDK
        # constructor call — passing an empty string would defeat the
        # SDK's fallback chain because the SDK only treats ``None``
        # as "missing."
        with patch("cohere.ClientV2") as mock_client:
            CohereReranker(
                RerankConfig(
                    base_url="",
                    model="rerank-v4.0-pro",
                    api_key="ck-test",  # pragma: allowlist secret
                    candidates=20,
                    top_n=5,
                    timeout_secs=42.5,
                )
            )
            mock_client.assert_called_once()
            assert "base_url" not in mock_client.call_args.kwargs

    def test_timeout_is_passed_to_sdk_client(self):
        # A stalled Cohere request must not be allowed to pin the
        # hybrid_search worker thread on the SDK's 300s default —
        # ``RerankConfig.timeout_secs`` flows through to ClientV2 so a
        # bounded deadline triggers and the rerank stage degrades to
        # RRF order on timeout.
        with patch("cohere.ClientV2") as mock_client:
            CohereReranker(
                RerankConfig(
                    base_url="",
                    model="rerank-v4.0-pro",
                    api_key="ck-test",  # pragma: allowlist secret
                    candidates=20,
                    top_n=5,
                    timeout_secs=42.5,
                )
            )
            mock_client.assert_called_once()
            assert mock_client.call_args.kwargs["timeout"] == 42.5

        with patch("cohere.ClientV2") as mock_client:
            CohereReranker(
                RerankConfig(
                    base_url="https://gateway.example/v1",
                    model="rerank-v4.0-pro",
                    api_key="ck-test",  # pragma: allowlist secret
                    candidates=20,
                    top_n=5,
                    timeout_secs=15.0,
                )
            )
            assert mock_client.call_args.kwargs["timeout"] == 15.0
            assert mock_client.call_args.kwargs["base_url"] == "https://gateway.example/v1"

    def test_default_timeout_is_below_sdk_default(self):
        # Pin the default so a regression that drops timeout passthrough
        # (and falls back to the SDK's 300s) trips this test rather
        # than reaching production. 60s is well above typical Cohere
        # latency but tight enough to keep a stalled call from holding
        # a worker pool slot for minutes.
        assert DEFAULT_RERANK_TIMEOUT_SECS == 60.0

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

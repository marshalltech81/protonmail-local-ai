"""Tests for src.lib.reranker — HTTP client for mlx-service /rerank.

Uses httpx.MockTransport to avoid any real network or service
dependency. Failure paths are exercised explicitly because the rerank
stage is best-effort (failure must degrade silently to RRF order, not
fail the whole search).
"""

from __future__ import annotations

import httpx
from src.lib.reranker import MlxReranker, RerankConfig


def _install_mock(reranker: MlxReranker, handler) -> None:
    reranker.client.close()
    reranker.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=120.0)


def _make_reranker(top_n: int = 5, candidates: int = 50) -> MlxReranker:
    return MlxReranker(
        RerankConfig(
            base_url="http://host.docker.internal:8001",
            candidates=candidates,
            top_n=top_n,
        )
    )


class TestRerankSuccess:
    def test_returns_index_score_pairs_in_service_order(self):
        r = _make_reranker(top_n=5)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/rerank"
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 2, "score": 0.91},
                        {"index": 0, "score": 0.42},
                        {"index": 1, "score": 0.05},
                    ]
                },
            )

        _install_mock(r, handler)
        out = r.rerank("query", ["a", "b", "c"])
        assert out == [(2, 0.91), (0, 0.42), (1, 0.05)]

    def test_passes_top_n_in_request_body(self):
        r = _make_reranker(top_n=3)
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"results": []})

        _install_mock(r, handler)
        r.rerank("q", ["a", "b", "c", "d"])
        assert captured["top_n"] == 3
        assert captured["query"] == "q"
        assert captured["documents"] == ["a", "b", "c", "d"]

    def test_caller_top_n_override_wins_over_default(self):
        # Reranker default top_n=5, but caller asks for 20 → service
        # must see top_n=20 in the request body. This is the wiring
        # behind the ``extract_from_emails(limit=20)`` recall fix.
        r = _make_reranker(top_n=5)
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"results": []})

        _install_mock(r, handler)
        r.rerank("q", ["a", "b", "c"], top_n=20)
        assert captured["top_n"] == 20

    def test_empty_documents_short_circuits_without_http_call(self):
        r = _make_reranker()
        called = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"results": []})

        _install_mock(r, handler)
        assert r.rerank("q", []) == []
        assert called["n"] == 0


class TestRerankFailure:
    def test_http_error_returns_empty_list(self):
        r = _make_reranker()

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "model crashed"})

        _install_mock(r, handler)
        # Must not raise — the contract is "best effort, [] means fall
        # back to RRF order".
        assert r.rerank("q", ["a", "b"]) == []

    def test_connect_error_returns_empty_list(self):
        r = _make_reranker()

        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("service down")

        _install_mock(r, handler)
        assert r.rerank("q", ["a", "b"]) == []

    def test_malformed_response_returns_empty_list(self):
        r = _make_reranker()

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"score": "not-a-number"}]})

        _install_mock(r, handler)
        assert r.rerank("q", ["a", "b"]) == []

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


def _make_reranker(candidates: int = 50) -> CohereReranker:
    return CohereReranker(
        RerankConfig(
            base_url="",
            model="rerank-v4.0-pro",
            api_key="ck-test",  # pragma: allowlist secret
            candidates=candidates,
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
        # Five documents so ``top_n=5`` flows through unclamped — the
        # clamp behavior has its own dedicated test.
        out = r.rerank("q", ["a", "b", "c", "d", "e"], top_n=5)
        assert out == [(2, 0.9), (0, 0.4), (1, 0.1)]
        assert captured["top_n"] == 5
        assert captured["query"] == "q"
        assert captured["documents"] == ["a", "b", "c", "d", "e"]

    def test_top_n_clamped_to_document_count(self):
        # Cohere rejects ``top_n > len(documents)`` with a 400, which
        # would otherwise propagate as a generic rerank failure and
        # silently degrade to RRF. The reranker clamps before the call
        # so the caller's "give me up to N" intent is honored against
        # smaller candidate sets.
        r = _make_reranker()
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(results=[_result(0, 0.5), _result(1, 0.3)])

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        r.rerank("q", ["a", "b"], top_n=50)
        assert captured["top_n"] == 2

    def test_caller_top_n_is_forwarded(self):
        r = _make_reranker()
        captured: dict = {}

        def fake_rerank(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(results=[])

        # Provide enough documents that ``top_n=20`` survives the
        # ``min(top_n, len(documents))`` clamp — this test is about
        # the caller's cutoff reaching the SDK, not the clamp.
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
        assert r.rerank("q", [], top_n=5) == []
        assert called["n"] == 0

    def test_sdk_exception_returns_empty_for_graceful_degradation(self):
        # Reranker failures must not abort the search query — the
        # caller falls back to the original RRF order. Pinning this
        # behavior prevents a Cohere outage from taking down search.
        r = _make_reranker()

        def fake_rerank(**_kwargs):
            raise RuntimeError("simulated cohere outage")

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        assert r.rerank("q", ["a", "b"], top_n=5) == []

    def test_empty_base_url_omits_kwarg_so_sdk_default_applies(self):
        # An empty ``base_url`` is what ``main.py`` passes for an
        # explicit ``RERANK_BASE_URL=default`` (#750): "use the SDK
        # default" (``https://api.cohere.com``). Symmetric with how ``EmbedClient``, ``OpenAIEmbedder``, and
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
                    timeout_secs=42.5,
                )
            )
            mock_client.assert_called_once()
            assert "base_url" not in mock_client.call_args.kwargs

    def test_timeout_is_passed_to_sdk_client(self):
        # A stalled Cohere request must not be allowed to pin the
        # hybrid_search worker thread on the SDK's 300s default —
        # ``RerankConfig.timeout_secs`` flows through to ClientV2 so a
        # stalled call times out (per-operation, not a total deadline)
        # and the rerank stage degrades to RRF order.
        with patch("cohere.ClientV2") as mock_client:
            CohereReranker(
                RerankConfig(
                    base_url="",
                    model="rerank-v4.0-pro",
                    api_key="ck-test",  # pragma: allowlist secret
                    candidates=20,
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
        assert r.rerank("q", ["a"], top_n=5) == []

    def test_malformed_fields_never_reach_logs(self, caplog):
        # A 200 response whose fields echo a submitted passage must not
        # put that text in the log via a conversion error (#224).
        import logging

        r = _make_reranker()
        marker = "SYNTHETIC_PRIVATE_MAIL"
        for bad in (
            SimpleNamespace(index=marker, relevance_score=0.9),
            SimpleNamespace(index=0, relevance_score=marker),
        ):

            def fake_rerank(_bad=bad, **_kwargs):
                return SimpleNamespace(results=[_bad])

            r.client.rerank = fake_rerank  # type: ignore[assignment]
            with caplog.at_level(logging.DEBUG):
                assert r.rerank("q", [marker], top_n=5) == []
        assert "rerank failed" in caplog.text
        assert marker not in caplog.text

    def test_unexpected_exception_logs_type_only(self, caplog):
        # SDK response parsing (pydantic) errors quote the input value;
        # only status errors and the exception type are safe to log.
        import logging

        r = _make_reranker()

        def fake_rerank(**_kwargs):
            raise ValueError("input_value='SYNTHETIC_PRIVATE_MAIL'")

        r.client.rerank = fake_rerank  # type: ignore[assignment]
        with caplog.at_level(logging.DEBUG):
            assert r.rerank("q", ["a"], top_n=5) == []
        assert "ValueError" in caplog.text
        assert "SYNTHETIC_PRIVATE_MAIL" not in caplog.text


class TestRerankNoRetries:
    def test_503_is_requested_once_and_falls_back(self, caplog):
        # The SDK retries a 5xx twice by default, so one rerank call
        # could take three times ``RERANK_TIMEOUT_SECS`` plus backoff
        # before the RRF fallback (#483). Drive the real SDK request
        # path against a loopback server so the count proves the SDK
        # itself did not retry.
        import logging
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        hits = {"n": 0}

        class _Unavailable(BaseHTTPRequestHandler):
            def do_POST(self):
                hits["n"] += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = b'{"message": "SYNTHETIC_PRIVATE_MAIL"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Unavailable)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            r = CohereReranker(
                RerankConfig(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    model="rerank-v4.0-pro",
                    api_key="ck-test",  # pragma: allowlist secret
                    candidates=20,
                    timeout_secs=5.0,
                )
            )
            with caplog.at_level(logging.DEBUG):
                assert r.rerank("q", ["SYNTHETIC_PRIVATE_MAIL"], top_n=5) == []
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert hits["n"] == 1
        assert "503" in caplog.text
        assert "SYNTHETIC_PRIVATE_MAIL" not in caplog.text

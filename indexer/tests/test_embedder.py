"""Tests for src.embedder — MLX HTTP client for embeddings.

Uses httpx.MockTransport to avoid any real network or service dependency.
"""

from unittest.mock import patch

import httpx
import pytest
from src.embedder import MlxEmbedder


def _install_mock(embedder, handler) -> None:
    """Swap an embedder's httpx client for one backed by a mock transport."""
    embedder.client.close()
    embedder.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)


class TestMlxEmbedder:
    def test_returns_embedding_vector_on_success(self):
        emb = MlxEmbedder("http://host.docker.internal:8001")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/embed"
            assert request.method == "POST"
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

        _install_mock(emb, handler)
        assert emb.embed("hello") == [0.1, 0.2, 0.3]

    def test_base_url_trailing_slash_is_stripped(self):
        emb = MlxEmbedder("http://host.docker.internal:8001/")
        assert emb.base_url == "http://host.docker.internal:8001"

    def test_retries_on_transient_error_then_succeeds(self):
        emb = MlxEmbedder("http://host.docker.internal:8001")
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "warming"})
            return httpx.Response(200, json={"embedding": [9.0]})

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            assert emb.embed("hello") == [9.0]
        assert calls["n"] == 2

    def test_wait_for_ready_health_then_warm(self):
        emb = MlxEmbedder("http://host.docker.internal:8001")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/embed":
                return httpx.Response(200, json={"embedding": [0.0]})
            raise AssertionError(f"unexpected {request.url}")

        _install_mock(emb, handler)
        emb.wait_for_ready(timeout=5)
        assert seen == ["GET /health", "POST /embed"]

    def test_wait_for_ready_times_out_when_health_never_responds(self):
        emb = MlxEmbedder("http://host.docker.internal:8001")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _install_mock(emb, handler)
        with patch("src.embedder.time.sleep", lambda _: None):
            with pytest.raises(RuntimeError, match="mlx-service did not become ready"):
                emb.wait_for_ready(timeout=0)

    def test_wait_for_ready_propagates_warmup_failure(self):
        emb = MlxEmbedder("http://host.docker.internal:8001")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(500, json={"error": "model load failed"})

        _install_mock(emb, handler)
        with pytest.raises(httpx.HTTPStatusError):
            emb.wait_for_ready(timeout=5)

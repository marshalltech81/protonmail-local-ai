"""Tests for src.embedder — Ollama HTTP client for embeddings.

Uses httpx.MockTransport to avoid any real network or Ollama dependency.
"""

from unittest.mock import patch

import httpx
import pytest
from src.embedder import Embedder, _model_matches


def _install_mock(embedder: Embedder, handler) -> None:
    """Swap the embedder's httpx client for one backed by a mock transport."""
    embedder.client.close()
    embedder.client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)


class TestEmbed:
    def test_returns_embedding_vector_on_success(self):
        emb = Embedder("http://ollama:11434", "test-model")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/embeddings"
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})

        _install_mock(emb, handler)
        assert emb.embed("hello") == [0.1, 0.2, 0.3]

    def test_retries_on_transient_error_then_succeeds(self):
        emb = Embedder("http://ollama:11434", "test-model")
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "not ready"})
            return httpx.Response(200, json={"embedding": [1.0]})

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            result = emb.embed("retry please")
        assert result == [1.0]
        assert calls["n"] == 2

    def test_raises_after_exhausting_retries(self):
        emb = Embedder("http://ollama:11434", "test-model")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        _install_mock(emb, handler)
        # tenacity wraps the final exception in RetryError around httpx.HTTPStatusError
        with pytest.raises(Exception):
            emb.embed("fail")

    def test_host_trailing_slash_is_stripped(self):
        emb = Embedder("http://ollama:11434/", "test-model")
        assert emb.host == "http://ollama:11434"


class TestWaitForReady:
    def test_returns_when_model_already_pulled(self):
        emb = Embedder("http://ollama:11434", "nomic-embed")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json={"models": [{"name": "nomic-embed:latest"}]})

        _install_mock(emb, handler)
        emb.wait_for_ready(timeout=5)

    def test_pulls_model_when_missing(self):
        emb = Embedder("http://ollama:11434", "mxbai")
        calls = {"tags": 0, "pull": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                calls["tags"] += 1
                return httpx.Response(200, json={"models": [{"name": "other-model:latest"}]})
            if request.url.path == "/api/pull":
                calls["pull"] += 1
                return httpx.Response(200, text='{"status":"pulling"}\n{"status":"success"}\n')
            raise AssertionError(f"unexpected request to {request.url}")

        _install_mock(emb, handler)
        emb.wait_for_ready(timeout=5)
        assert calls["tags"] == 1
        assert calls["pull"] == 1

    def test_times_out_when_ollama_never_responds(self):
        emb = Embedder("http://ollama:11434", "m")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _install_mock(emb, handler)
        # Make sleep a no-op so the test runs quickly.
        with patch("src.embedder.time.sleep", lambda _: None):
            with pytest.raises(RuntimeError, match="Ollama did not become ready"):
                emb.wait_for_ready(timeout=0)

    def test_does_not_match_prefix_model(self):
        # Prior substring matching would have treated ``llama3`` as
        # present whenever ``llama3.2`` was pulled, silently returning
        # ready with the wrong model. Exact matching forces a pull.
        emb = Embedder("http://ollama:11434", "llama3")
        calls = {"tags": 0, "pull": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                calls["tags"] += 1
                return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
            if request.url.path == "/api/pull":
                calls["pull"] += 1
                return httpx.Response(200, text='{"status":"success"}\n')
            raise AssertionError(f"unexpected request to {request.url}")

        _install_mock(emb, handler)
        emb.wait_for_ready(timeout=5)
        assert calls["pull"] == 1

    def test_pull_raises_on_http_error(self):
        # A failed pull (e.g. unknown model name) must surface as an
        # exception, not be silently logged as a successful ready state.
        emb = Embedder("http://ollama:11434", "nonexistent")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": []})
            if request.url.path == "/api/pull":
                return httpx.Response(404, json={"error": "not found"})
            raise AssertionError(f"unexpected request to {request.url}")

        _install_mock(emb, handler)
        with pytest.raises(httpx.HTTPStatusError):
            emb.wait_for_ready(timeout=5)


class TestModelMatches:
    def test_exact_match(self):
        assert _model_matches("nomic-embed-text", "nomic-embed-text")

    def test_latest_suffix_match(self):
        assert _model_matches("nomic-embed-text", "nomic-embed-text:latest")

    def test_prefix_is_not_match(self):
        assert not _model_matches("llama3", "llama3.2:latest")

    def test_different_tag_is_not_match(self):
        assert not _model_matches("nomic-embed-text:v1", "nomic-embed-text:v2")

    def test_configured_with_tag_requires_exact(self):
        assert _model_matches("llama3:8b", "llama3:8b")
        assert not _model_matches("llama3:8b", "llama3:8b:latest")

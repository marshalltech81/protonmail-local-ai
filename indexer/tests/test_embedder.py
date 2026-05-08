"""Tests for src.embedder — OpenAI-compatible HTTP client for embeddings.

Uses httpx.MockTransport to avoid any real network or service dependency.
"""

from unittest.mock import patch

import httpx
import pytest
from src.embedder import OpenAIEmbedder

OPENAI_DATA_KEY = "data"


def _install_mock(embedder, handler) -> None:
    """Swap an embedder's httpx client for one backed by a mock transport.

    Preserves the headers configured at construction time (notably the
    Authorization header) so tests of auth behavior are accurate.
    """
    headers = dict(embedder.client.headers)
    embedder.client.close()
    embedder.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=60.0,
        headers=headers,
    )


def _embed_response(vectors: list[list[float]], reverse_order: bool = False) -> dict:
    indices = list(range(len(vectors)))
    if reverse_order:
        indices.reverse()
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v, "index": idx}
            for v, idx in zip(vectors, indices)
        ],
        "model": "test-model",
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


class TestOpenAIEmbedder:
    def test_embed_returns_vector_on_success(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/embeddings"
            assert request.method == "POST"
            return httpx.Response(200, json=_embed_response([[0.1, 0.2, 0.3]]))

        _install_mock(emb, handler)
        assert emb.embed("hello") == [0.1, 0.2, 0.3]

    def test_base_url_trailing_slash_is_stripped(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1/", "test-model")
        assert emb.base_url == "http://host.docker.internal:8001/v1"

    def test_request_body_carries_model_id(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "configured-model")
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_embed_response([[1.0]]))

        _install_mock(emb, handler)
        emb.embed("x")
        assert seen["body"] == {"model": "configured-model", "input": ["x"]}

    def test_retries_on_5xx_then_succeeds(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "warming"})
            return httpx.Response(200, json=_embed_response([[9.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            assert emb.embed("hello") == [9.0]
        assert calls["n"] == 2

    def test_embed_batch_returns_vectors_in_input_order(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            # Provider returns data in canonical input order.
            return httpx.Response(200, json=_embed_response([[1.0], [2.0], [3.0]]))

        _install_mock(emb, handler)
        assert emb.embed_batch(["a", "b", "c"]) == [[1.0], [2.0], [3.0]]

    def test_embed_batch_sorts_data_by_index_when_provider_reorders(self):
        # If a future provider returns ``data`` in non-canonical order,
        # the client must use the ``index`` field to realign vectors
        # with input positions. Without this, vectors silently end up
        # paired with the wrong source texts in the indexer.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            # Vectors v0,v1,v2 returned with indices 2,1,0 (reversed).
            return httpx.Response(
                200, json=_embed_response([[1.0], [2.0], [3.0]], reverse_order=True)
            )

        _install_mock(emb, handler)
        # After sorting by index: v at idx=0 is [3.0], idx=1 is [2.0], idx=2 is [1.0].
        assert emb.embed_batch(["a", "b", "c"]) == [[3.0], [2.0], [1.0]]

    def test_embed_batch_chunks_at_batch_size_boundary(self):
        # 5 inputs at batch_size=2 must produce 3 HTTP calls
        # (sizes 2, 2, 1) and concatenate results in input order.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model", batch_size=2)
        seen_batches: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            body = json.loads(request.content)
            inputs = body["input"]
            seen_batches.append(inputs)
            vectors = [[float(ord(s))] for s in inputs]
            return httpx.Response(200, json=_embed_response(vectors))

        _install_mock(emb, handler)
        result = emb.embed_batch(["a", "b", "c", "d", "e"])
        assert seen_batches == [["a", "b"], ["c", "d"], ["e"]]
        assert result == [[97.0], [98.0], [99.0], [100.0], [101.0]]

    def test_embed_batch_empty_returns_empty_without_http(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_embed_response([]))

        _install_mock(emb, handler)
        assert emb.embed_batch([]) == []
        assert calls["n"] == 0

    def test_authorization_header_set_when_api_key_provided(self):
        emb = OpenAIEmbedder(
            "https://api.deepinfra.com/v1/openai",
            "Qwen/Qwen3-Embedding-8B",
            api_key="sk-test-123",  # pragma: allowlist secret
        )
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=_embed_response([[0.0]]))

        _install_mock(emb, handler)
        emb.embed("x")
        assert seen["auth"] == "Bearer sk-test-123"

    def test_authorization_header_omitted_when_api_key_empty(self):
        # Local mlx-service does not authenticate; sending a bare
        # ``Authorization: Bearer`` (no token) would be a malformed
        # header. The client must omit the header entirely.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        seen: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=_embed_response([[0.0]]))

        _install_mock(emb, handler)
        emb.embed("x")
        assert seen["auth"] is None

    def test_wait_for_ready_succeeds_on_first_probe(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json=_embed_response([[0.0]]))

        _install_mock(emb, handler)
        emb.wait_for_ready(timeout=5)
        # One probe call only — no separate /health round-trip.
        assert seen == ["POST /v1/embeddings"]

    def test_wait_for_ready_retries_on_connect_error_then_succeeds(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json=_embed_response([[0.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.time.sleep", lambda _: None):
            emb.wait_for_ready(timeout=30)
        assert calls["n"] == 2

    def test_wait_for_ready_times_out_when_never_responds(self):
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _install_mock(emb, handler)
        with patch("src.embedder.time.sleep", lambda _: None):
            with patch.dict("os.environ", {"EMBED_WARMUP_TIMEOUT_SECS": "0"}):
                with pytest.raises(RuntimeError, match="did not become ready"):
                    emb.wait_for_ready(timeout=0)

    def test_wait_for_ready_fails_fast_on_4xx_auth_error(self):
        # 401/403/404/422 won't recover by retrying; surface immediately
        # so the operator fixes config rather than waiting out the
        # warmup deadline.
        emb = OpenAIEmbedder(
            "https://api.example.com/v1",
            "wrong-model",
            api_key="bad-key",  # pragma: allowlist secret
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad auth"})

        _install_mock(emb, handler)
        with pytest.raises(httpx.HTTPStatusError):
            emb.wait_for_ready(timeout=30)

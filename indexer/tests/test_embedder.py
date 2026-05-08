"""Tests for src.embedder — OpenAI-compatible HTTP client for embeddings.

Uses httpx.MockTransport to avoid any real network or service dependency.
"""

from unittest.mock import patch

import httpx
import pytest
from src.embedder import OpenAIEmbedder, _is_transient_embed_error

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

    def test_embed_batch_raises_on_duplicate_indices(self):
        # A misbehaving provider that returns the same ``index`` twice
        # must surface as a loud RuntimeError rather than silently
        # attaching the wrong vector to a chunk. The indexer would
        # otherwise commit mis-aligned vectors that survive every later
        # restart.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [1.0], "index": 0},
                        {"object": "embedding", "embedding": [2.0], "index": 0},
                        {"object": "embedding", "embedding": [3.0], "index": 2},
                    ],
                    "model": "test-model",
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            with pytest.raises(RuntimeError, match="non-contiguous or duplicate"):
                emb.embed_batch(["a", "b", "c"])

    def test_embed_batch_raises_on_missing_index(self):
        # A provider that drops a ``data`` entry (returning N-1 vectors
        # for N inputs) must also fail loud. Same rationale as duplicate
        # indices: silent zip-misalignment is a worse outcome than an
        # exception that triggers the queue retry path.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [1.0], "index": 0},
                        {"object": "embedding", "embedding": [3.0], "index": 2},
                    ],
                    "model": "test-model",
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            with pytest.raises(RuntimeError, match=r"returned 2 vectors for 3 inputs"):
                emb.embed_batch(["a", "b", "c"])

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

    def test_wait_for_ready_connect_deadline_independent_of_warmup_timeout(self):
        # Regression: the connect-phase deadline must use ``timeout``,
        # not ``max(timeout, EMBED_WARMUP_TIMEOUT_SECS)``. The earlier
        # ``max()`` form let a default 600s warmup ceiling extend the
        # connect-phase budget — a missing service would hang for 10
        # minutes instead of failing in ~timeout. Drive a deterministic
        # virtual clock so the assertion cannot itself wait out 600s
        # if the regression returns.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        handler_calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            handler_calls["n"] += 1
            raise httpx.ConnectError("refused")

        _install_mock(emb, handler)

        # Each time.time() call advances 1 virtual second.
        virtual_now = [1000.0]

        def fake_time() -> float:
            virtual_now[0] += 1.0
            return virtual_now[0]

        with patch("src.embedder.time.sleep", lambda _: None):
            with patch("src.embedder.time.time", fake_time):
                with patch.dict("os.environ", {"EMBED_WARMUP_TIMEOUT_SECS": "600"}):
                    with pytest.raises(RuntimeError, match="did not become ready"):
                        emb.wait_for_ready(timeout=2)

        # With the fix (deadline = start + 2 virtual seconds): at most a
        # couple of attempts fit in the budget. With the regression
        # (deadline = start + 600 virtual seconds): hundreds of attempts.
        assert handler_calls["n"] <= 3, (
            f"connect deadline should respect timeout=2 regardless of "
            f"EMBED_WARMUP_TIMEOUT_SECS=600, got {handler_calls['n']} attempts"
        )

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


class TestRetryPredicate:
    """``_is_transient_embed_error`` decides whether tenacity retries.

    The predicate exists to keep ``_embed_one_batch`` from retrying its
    own integrity-check ``RuntimeError`` (deterministic; retry burns
    latency before the same failure resurfaces) while still retrying
    transport-level failures (connection drop, 5xx, read timeout).
    """

    def test_retries_connect_error(self):
        assert _is_transient_embed_error(httpx.ConnectError("conn refused"))

    def test_retries_read_timeout(self):
        assert _is_transient_embed_error(httpx.ReadTimeout("read timed out"))

    def test_retries_remote_protocol_error(self):
        assert _is_transient_embed_error(httpx.RemoteProtocolError("proto"))

    def test_retries_5xx_http_status_error(self):
        response = httpx.Response(503, request=httpx.Request("POST", "http://x/"))
        exc = httpx.HTTPStatusError(
            "Service Unavailable", request=response.request, response=response
        )
        assert _is_transient_embed_error(exc)

    def test_does_not_retry_4xx_http_status_error(self):
        response = httpx.Response(401, request=httpx.Request("POST", "http://x/"))
        exc = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)
        assert not _is_transient_embed_error(exc)

    def test_does_not_retry_runtime_error(self):
        # Self-raised integrity-check failures (malformed batch, missing
        # indices) are deterministic. Retrying just delays the same
        # exception surfacing.
        assert not _is_transient_embed_error(RuntimeError("bad data"))

    def test_does_not_retry_value_error(self):
        # Any non-transport exception class falls through to ``False``.
        assert not _is_transient_embed_error(ValueError("nope"))

    def test_runtime_error_is_not_retried_at_call_site(self):
        """End-to-end: ``embed_batch`` raises a RuntimeError on a
        malformed provider response and the handler is invoked exactly
        once — proving tenacity does not retry the integrity check."""
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "embedding": [1.0], "index": 0},
                    ],
                    "model": "test-model",
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )

        _install_mock(emb, handler)
        with pytest.raises(RuntimeError):
            emb.embed_batch(["a", "b"])  # asks for 2, provider returns 1
        assert calls["n"] == 1, "RuntimeError must not be retried"

    def test_5xx_is_retried_at_call_site(self):
        """End-to-end: a 5xx on the first call followed by a clean 200
        succeeds via the tenacity retry."""
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, json=_embed_response([[1.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            result = emb.embed_batch(["a"])
        assert result == [[1.0]]
        assert calls["n"] == 2, "5xx HTTPStatusError must be retried"

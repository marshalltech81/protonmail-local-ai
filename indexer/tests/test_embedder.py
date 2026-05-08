"""Tests for src.embedder — OpenAI-compatible HTTP client for embeddings.

Uses httpx.MockTransport to avoid any real network or service dependency.
"""

from unittest.mock import patch

import httpx
import pytest
from src.chunker import l2_normalize
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
        # Use a vector that's already L2-unit-norm (``[1, 0, 0]``) so
        # the embedder's defensive normalization step is a no-op for
        # this test. Other tests below explicitly verify the
        # normalization behavior.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/embeddings"
            assert request.method == "POST"
            return httpx.Response(200, json=_embed_response([[1.0, 0.0, 0.0]]))

        _install_mock(emb, handler)
        assert emb.embed("hello") == [1.0, 0.0, 0.0]

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
            return httpx.Response(200, json=_embed_response([[1.0, 0.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            assert emb.embed("hello") == [1.0, 0.0]
        assert calls["n"] == 2

    def test_embed_batch_returns_vectors_in_input_order(self):
        # Distinct unit-norm canonical-basis vectors keep both the
        # order check and the L2-normalization invariant — the new
        # post-fetch normalization is a no-op against unit-norm
        # input, so this test still pins ordering without colliding
        # with the normalization behavior.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            # Provider returns data in canonical input order.
            return httpx.Response(
                200,
                json=_embed_response([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
            )

        _install_mock(emb, handler)
        assert emb.embed_batch(["a", "b", "c"]) == [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]

    def test_embed_batch_sorts_data_by_index_when_provider_reorders(self):
        # If a future provider returns ``data`` in non-canonical order,
        # the client must use the ``index`` field to realign vectors
        # with input positions. Without this, vectors silently end up
        # paired with the wrong source texts in the indexer.
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            # Three distinct unit-norm vectors returned with indices
            # 2,1,0 (reversed). After sorting by index, each lands at
            # its source position regardless of provider order.
            return httpx.Response(
                200,
                json=_embed_response(
                    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    reverse_order=True,
                ),
            )

        _install_mock(emb, handler)
        assert emb.embed_batch(["a", "b", "c"]) == [
            [0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
        ]

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
            # Per-input scaled unit vector along the x-axis: each input
            # gets a 1-dim ``[1.0]`` (already unit-norm) so the test
            # locks ordering + concatenation without colliding with
            # the embedder's defensive L2 normalization.
            vectors = [[1.0] for _ in inputs]
            return httpx.Response(200, json=_embed_response(vectors))

        _install_mock(emb, handler)
        result = emb.embed_batch(["a", "b", "c", "d", "e"])
        assert seen_batches == [["a", "b"], ["c", "d"], ["e"]]
        assert result == [[1.0], [1.0], [1.0], [1.0], [1.0]]

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

    def test_wait_for_ready_retries_on_connect_timeout(self):
        """Regression for the timeout-class gap: the previous explicit
        allowlist (``ConnectError`` / ``ReadTimeout`` /
        ``RemoteProtocolError``) missed the entire ``TimeoutException``
        hierarchy — ``ConnectTimeout`` / ``WriteTimeout`` /
        ``PoolTimeout`` would propagate uncaught and crash startup
        instead of triggering a retry. ``wait_for_ready`` now
        delegates to ``_is_transient_embed_error`` so its retry
        classification matches ``_embed_one_batch`` exactly.
        """
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectTimeout("first probe timed out")
            return httpx.Response(200, json=_embed_response([[0.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.time.sleep", lambda _: None):
            emb.wait_for_ready(timeout=30)
        assert calls["n"] == 2

    def test_wait_for_ready_fails_fast_on_unsupported_protocol(self):
        """Counterpart to the timeout-class test: an
        ``httpx.UnsupportedProtocol`` (raised when ``base_url`` lacks
        a scheme — e.g. ``EMBED_BASE_URL=host.docker.internal:8001/v1``
        instead of ``http://...``) must propagate immediately rather
        than retry until the connect deadline. The earlier shape
        retried every ``TransportError`` subclass, so a misconfigured
        URL would silently retry for ``timeout`` seconds before the
        operator saw the actionable error.
        """
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.UnsupportedProtocol("bad URL scheme")

        _install_mock(emb, handler)
        with patch("src.embedder.time.sleep", lambda _: None):
            with pytest.raises(httpx.UnsupportedProtocol):
                emb.wait_for_ready(timeout=30)
        assert calls["n"] == 1, (
            "UnsupportedProtocol must NOT retry — it's a deterministic "
            "config error and the operator needs the failure surfaced fast"
        )

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

    def test_connect_timeout_is_retried_at_call_site(self):
        """End-to-end: a ConnectTimeout on the first call followed by a
        clean 200 succeeds via the tenacity retry. The earlier predicate
        only listed ``ConnectError`` / ``ReadTimeout`` /
        ``RemoteProtocolError`` and missed the timeout subclasses
        (``ConnectTimeout`` / ``WriteTimeout`` / ``PoolTimeout``), making
        a common transient provider hiccup fail the whole embed batch
        immediately. ``httpx.TransportError`` is the right base class
        to catch all of them."""
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectTimeout("first call timed out")
            return httpx.Response(200, json=_embed_response([[1.0]]))

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            result = emb.embed_batch(["a"])
        assert result == [[1.0]]
        assert calls["n"] == 2, "ConnectTimeout must be retried"

    def test_connect_timeout_is_classified_as_transient(self):
        # Direct predicate test for the exact subclass that motivated
        # the switch from the explicit allowlist to ``TransportError``.
        assert _is_transient_embed_error(httpx.ConnectTimeout("connect timeout"))

    def test_write_timeout_is_classified_as_transient(self):
        assert _is_transient_embed_error(httpx.WriteTimeout("write timeout"))

    def test_pool_timeout_is_classified_as_transient(self):
        assert _is_transient_embed_error(httpx.PoolTimeout("pool exhausted"))

    def test_unsupported_protocol_is_NOT_transient(self):
        # ``base_url`` lacks scheme (e.g. ``host.docker.internal:8001``);
        # a deterministic config error, not a transient outage.
        # Retrying just delays the actionable failure.
        assert not _is_transient_embed_error(httpx.UnsupportedProtocol("no scheme"))

    def test_local_protocol_error_is_NOT_transient(self):
        # Client built a malformed request (HTTP/2 framing bug, illegal
        # header value); almost always a code or config issue.
        assert not _is_transient_embed_error(httpx.LocalProtocolError("bad request"))

    def test_unsupported_protocol_at_call_site_is_not_retried(self):
        """End-to-end: ``embed_batch`` against an UnsupportedProtocol
        must propagate after a single attempt. Regression guard for
        the ``base_url`` typo case where the previous predicate
        retried the whole TransportError family indiscriminately."""
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.UnsupportedProtocol("bad URL scheme")

        _install_mock(emb, handler)
        with patch("src.embedder.wait_exponential", lambda **_: lambda *_: 0):
            with pytest.raises(httpx.UnsupportedProtocol):
                emb.embed_batch(["a"])
        assert calls["n"] == 1, "UnsupportedProtocol must not retry at runtime either"


class TestL2Normalize:
    """Pure-function behavior of ``l2_normalize`` (defined in
    ``src.chunker``). The embedder client uses this defensively on raw
    provider output; the DB write boundary in ``src.database`` uses it
    on thread vectors derived from ``mean_vector``. Both rely on the
    same idempotence + zero-vector-preserved guarantees verified here."""

    def test_zero_vector_is_returned_unchanged(self):
        # The seed-vector logic intentionally writes a zero placeholder
        # for genuinely-new threads; dividing by zero would NaN-poison
        # storage. Preserving it keeps the three-case priority chain
        # intact.
        result = l2_normalize([0.0, 0.0, 0.0])
        assert result == [0.0, 0.0, 0.0]

    def test_already_unit_norm_short_circuits(self):
        # Avoid float churn on already-normalized inputs. The function
        # must return the SAME list object (identity preserved) when
        # the input is within tolerance of unit norm.
        v = [1.0, 0.0, 0.0]
        result = l2_normalize(v)
        assert result is v, "unit-norm input must skip the divide branch"

    def test_non_unit_vector_is_normalized(self):
        # Classic 3-4-5 right triangle: norm = 5, unit vector = (3/5, 4/5).
        result = l2_normalize([3.0, 4.0])
        assert result[0] == pytest.approx(0.6)
        assert result[1] == pytest.approx(0.8)
        # And the result IS unit-norm.
        norm_sq = sum(x * x for x in result)
        assert norm_sq == pytest.approx(1.0)

    def test_embed_batch_returns_normalized_vectors(self):
        """End-to-end: a provider that returns non-normalized vectors
        is corrected at the embedder client so storage stays uniform."""
        emb = OpenAIEmbedder("http://host.docker.internal:8001/v1", "test-model")

        def handler(_request: httpx.Request) -> httpx.Response:
            # Magnitude-3 vectors along each axis. After normalization
            # each becomes the corresponding unit basis vector.
            return httpx.Response(
                200,
                json=_embed_response([[3.0, 0.0, 0.0], [0.0, 3.0, 0.0]]),
            )

        _install_mock(emb, handler)
        result = emb.embed_batch(["a", "b"])
        assert result[0] == pytest.approx([1.0, 0.0, 0.0])
        assert result[1] == pytest.approx([0.0, 1.0, 0.0])

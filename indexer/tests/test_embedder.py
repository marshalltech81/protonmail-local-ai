"""Tests for ``src.embedder.OpenAIEmbedder``.

The embedder wraps the official ``openai`` SDK with a custom
``base_url``. Tests monkey-patch ``client.embeddings.create`` (and the
``with_options`` proxy used by ``wait_for_ready``) so behavior is
deterministic without hitting a live provider.
"""

import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from src.chunker import l2_normalize
from src.embedder import OpenAIEmbedder, _float_env, _is_transient_embed_error


def _embed_response(vectors: list[list[float]], reverse_order: bool = False) -> SimpleNamespace:
    indices = list(range(len(vectors)))
    if reverse_order:
        indices.reverse()
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=v, index=idx) for v, idx in zip(vectors, indices)],
    )


def _make_embedder(**overrides) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        overrides.pop("base_url", "http://host.docker.internal:8001/v1"),
        overrides.pop("model", "test-model"),
        **overrides,
    )


def _patch_create(embedder: OpenAIEmbedder, fn) -> None:
    """Install a fake ``embeddings.create`` on the embedder's SDK client."""
    embedder.client.embeddings.create = fn  # type: ignore[assignment]


def _patch_warmup(embedder: OpenAIEmbedder, fn) -> None:
    """Install a fake warmup path. ``wait_for_ready`` calls
    ``client.with_options(timeout=...).embeddings.create(...)``; the
    real SDK returns a new client from ``with_options``, so we stub
    it to return an object exposing the same fake create."""
    fake_client = SimpleNamespace(embeddings=SimpleNamespace(create=fn))
    embedder.client.with_options = lambda **_kwargs: fake_client  # type: ignore[assignment]


def _api_status_error(status_code: int) -> APIStatusError:
    """Build an APIStatusError with a real httpx response object so the
    SDK exception's ``status_code`` attribute resolves correctly."""
    return APIStatusError(
        message=f"{status_code} error",
        response=httpx.Response(status_code, request=httpx.Request("POST", "http://x")),
        body=None,
    )


class TestFloatEnv:
    """``_float_env`` falls back to the default on non-finite values.

    The indexer's float parser uses a fall-back-with-warning policy
    rather than raising (a typo in a tunable timeout must not crash
    the indexer), but ``float("nan")`` and ``float("inf")`` parse
    cleanly and would otherwise reach the SDK and break per-call
    deadlines. Treat them as malformed input and warn-fall-back.
    """

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("FAKE_FLOAT_VAR", raising=False)
        assert _float_env("FAKE_FLOAT_VAR", default=30.0) == 30.0

    def test_valid_finite_value_parses(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "45.0")
        assert _float_env("FAKE_FLOAT_VAR", default=30.0) == 45.0

    def test_malformed_string_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "not-a-number")
        with caplog.at_level("WARNING", logger="indexer.embedder"):
            assert _float_env("FAKE_FLOAT_VAR", default=30.0) == 30.0
        assert any("FAKE_FLOAT_VAR" in r.message for r in caplog.records)

    def test_nan_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "nan")
        with caplog.at_level("WARNING", logger="indexer.embedder"):
            assert _float_env("FAKE_FLOAT_VAR", default=30.0) == 30.0
        assert any("FAKE_FLOAT_VAR" in r.message for r in caplog.records)

    def test_positive_infinity_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "inf")
        with caplog.at_level("WARNING", logger="indexer.embedder"):
            assert _float_env("FAKE_FLOAT_VAR", default=30.0) == 30.0
        assert any("FAKE_FLOAT_VAR" in r.message for r in caplog.records)

    def test_below_minimum_falls_back(self, monkeypatch, caplog):
        # ``0`` and negative values parse cleanly but would reach the
        # OpenAI SDK as a per-call deadline of 0/negative and either
        # fail oddly or short-circuit warmup. Treat them as malformed
        # so a misconfigured EMBED_WARMUP_TIMEOUT_SECS=0 falls back to
        # the documented default rather than breaking startup.
        for raw in ("0", "0.5", "-1"):
            monkeypatch.setenv("FAKE_FLOAT_VAR", raw)
            caplog.clear()
            with caplog.at_level("WARNING", logger="indexer.embedder"):
                assert _float_env("FAKE_FLOAT_VAR", default=30.0, minimum=1.0) == 30.0
            assert any("FAKE_FLOAT_VAR" in r.message for r in caplog.records)

    def test_above_minimum_returns_parsed_value(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "1.0")
        assert _float_env("FAKE_FLOAT_VAR", default=30.0, minimum=1.0) == 1.0


class TestOpenAIEmbedder:
    def test_base_url_trailing_slash_is_stripped(self):
        emb = _make_embedder(base_url="http://x:8001/v1/")
        assert emb.base_url == "http://x:8001/v1"

    def test_embed_returns_vector_on_success(self):
        emb = _make_embedder()
        captured: dict = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return _embed_response([[1.0, 0.0, 0.0]])

        _patch_create(emb, fake_create)
        assert emb.embed("hello") == [1.0, 0.0, 0.0]
        assert captured["model"] == "test-model"
        assert captured["input"] == ["hello"]

    def test_embed_batch_returns_vectors_in_input_order(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[1.0, 0.0], [0.0, 1.0]])

        _patch_create(emb, fake_create)
        assert emb.embed_batch(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]

    def test_embed_batch_sorts_data_by_index_when_provider_reorders(self):
        # Some compat servers can return ``data`` reordered. The
        # defensive sort by ``index`` keeps vectors aligned with
        # their source texts.
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[1.0, 0.0], [0.0, 1.0]], reverse_order=True)

        _patch_create(emb, fake_create)
        # Inputs ["a", "b"] correspond to indices 0/1; the response
        # carries reversed indices [1, 0] so the data list, after the
        # defensive sort, must end up as [vector for idx=0, vector for idx=1].
        assert emb.embed_batch(["a", "b"]) == [[0.0, 1.0], [1.0, 0.0]]

    def test_embed_batch_raises_on_duplicate_indices(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1.0], index=0),
                    SimpleNamespace(embedding=[0.0], index=0),
                ],
            )

        _patch_create(emb, fake_create)
        with pytest.raises(RuntimeError, match="non-contiguous"):
            emb.embed_batch(["a", "b"])

    def test_embed_batch_raises_on_missing_index(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[1.0], index=0),
                    SimpleNamespace(embedding=[0.0], index=2),
                ],
            )

        _patch_create(emb, fake_create)
        with pytest.raises(RuntimeError, match="non-contiguous"):
            emb.embed_batch(["a", "b"])

    def test_embed_batch_raises_on_count_mismatch(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        with pytest.raises(RuntimeError, match="vectors for"):
            emb.embed_batch(["a", "b"])

    def test_embed_batch_chunks_at_batch_size_boundary(self):
        emb = _make_embedder(batch_size=2)
        calls: list[list[str]] = []

        def fake_create(**kwargs):
            calls.append(list(kwargs["input"]))
            n = len(kwargs["input"])
            return _embed_response([[1.0]] * n)

        _patch_create(emb, fake_create)
        out = emb.embed_batch(["a", "b", "c"])
        assert len(out) == 3
        assert calls == [["a", "b"], ["c"]]

    def test_embed_batch_empty_returns_empty_without_calling_sdk(self):
        emb = _make_embedder()
        called = {"n": 0}

        def fake_create(**_kwargs):
            called["n"] += 1
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        assert emb.embed_batch([]) == []
        assert called["n"] == 0

    def test_retries_on_5xx_then_succeeds(self):
        emb = _make_embedder()
        attempts = {"n": 0}

        def fake_create(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise _api_status_error(500)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        # Disable backoff sleep so retry is fast.
        emb._embed_one_batch.retry.wait = lambda *_args, **_kwargs: 0  # type: ignore[attr-defined]
        assert emb.embed_batch(["x"]) == [[1.0]]
        assert attempts["n"] == 2


class TestRetryPredicate:
    def test_retries_5xx_status_error(self):
        assert _is_transient_embed_error(_api_status_error(500)) is True
        assert _is_transient_embed_error(_api_status_error(503)) is True

    def test_does_not_retry_4xx_status_error(self):
        # 4xx is auth/quota/model-id config — retrying buys nothing.
        assert _is_transient_embed_error(_api_status_error(401)) is False
        assert _is_transient_embed_error(_api_status_error(400)) is False

    def test_retries_connection_error(self):
        # APIConnectionError requires a Request to construct.
        req = httpx.Request("POST", "http://x")
        exc = APIConnectionError(request=req)
        assert _is_transient_embed_error(exc) is True

    def test_retries_timeout_error(self):
        req = httpx.Request("POST", "http://x")
        exc = APITimeoutError(request=req)
        assert _is_transient_embed_error(exc) is True

    def test_does_not_retry_runtime_error(self):
        # Our own integrity-check RuntimeError must not retry — the
        # provider returned a malformed batch and a retry would produce
        # the same shape.
        assert _is_transient_embed_error(RuntimeError("integrity")) is False


class TestWaitForReady:
    def test_succeeds_on_first_probe(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[0.0]])

        _patch_warmup(emb, fake_create)
        emb.wait_for_ready(timeout=2)

    def test_retries_on_connect_error_then_succeeds(self, monkeypatch):
        # Skip the embedder's 3 s sleep between probes so the test runs fast.
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        emb = _make_embedder()
        attempts = {"n": 0}
        req = httpx.Request("POST", "http://x")

        def fake_create(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise APIConnectionError(request=req)
            return _embed_response([[0.0]])

        _patch_warmup(emb, fake_create)
        emb.wait_for_ready(timeout=5)
        assert attempts["n"] == 2

    def test_fails_fast_on_4xx(self):
        # 4xx is config error — surface immediately so the operator
        # fixes ``EMBED_MODEL`` / ``EMBED_API_KEY`` rather than waiting
        # out the connect deadline.
        emb = _make_embedder()

        def fake_create(**_kwargs):
            raise _api_status_error(401)

        _patch_warmup(emb, fake_create)
        with pytest.raises(APIStatusError):
            emb.wait_for_ready(timeout=5)

    def test_times_out_when_never_responds(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        emb = _make_embedder()
        req = httpx.Request("POST", "http://x")

        def fake_create(**_kwargs):
            raise APIConnectionError(request=req)

        _patch_warmup(emb, fake_create)
        with pytest.raises(RuntimeError, match="did not become ready"):
            emb.wait_for_ready(timeout=1)


class TestL2Normalize:
    def test_embed_batch_returns_normalized_vectors(self):
        # Provider returns a non-unit-norm vector; the embedder
        # normalizes at the boundary so storage invariants hold
        # regardless of provider behavior.
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[3.0, 4.0]])

        _patch_create(emb, fake_create)
        out = emb.embed_batch(["x"])
        assert out == [l2_normalize([3.0, 4.0])]

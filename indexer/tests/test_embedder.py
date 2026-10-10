"""Tests for ``src.embedder.OpenAIEmbedder``.

The embedder wraps the official ``openai`` SDK with a custom
``base_url``. Tests monkey-patch ``client.embeddings.create`` (and the
``with_options`` proxy used by ``wait_for_ready``) so behavior is
deterministic without hitting a live provider.
"""

import logging
import threading
import time
import traceback
from concurrent.futures import ALL_COMPLETED, wait
from types import SimpleNamespace

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from src.chunker import l2_normalize
from src.embedder import (
    EMBED_FAILURE_CONFIGURATION,
    EMBED_FAILURE_INFRASTRUCTURE,
    EMBED_FAILURE_REJECTED_INPUT,
    EMBED_FAILURE_UNCERTAIN,
    EmbedResponseError,
    OpenAIEmbedder,
    _float_env,
    _is_transient_embed_error,
    classify_embed_failure,
    scrub_embed_error,
)


def _embed_response(vectors: list[list[float]], reverse_order: bool = False) -> SimpleNamespace:
    indices = list(range(len(vectors)))
    if reverse_order:
        indices.reverse()
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=v, index=idx) for v, idx in zip(vectors, indices)],
    )


def _make_embedder(**overrides) -> OpenAIEmbedder:
    # ``api_key`` is required (non-empty) in production — startup
    # validation in ``main._validate_embed_config`` rejects an empty
    # value before the embedder is constructed. Tests supply an explicit
    # placeholder so the SDK's "non-empty string" check doesn't
    # masquerade as a constructor bug. Operators pointing at an
    # unauthenticated host-side server supply the same shape of value
    # in ``.secrets/embed_api_key.txt``.
    overrides.setdefault("api_key", "placeholder")
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
    """Build an APIStatusError with a real httpx2 response object so the
    SDK exception's ``status_code`` attribute resolves correctly."""
    return APIStatusError(
        message=f"{status_code} error",
        response=httpx2.Response(status_code, request=httpx2.Request("POST", "http://x")),
        body=None,
    )


class TestFloatEnv:
    """``_float_env`` falls back to the default on non-finite values.

    The indexer's float parser uses a fall-back-with-warning policy
    rather than raising (a typo in a tunable timeout must not crash
    the indexer), but ``float("nan")`` and ``float("inf")`` parse
    cleanly and would otherwise reach the SDK and break its HTTP
    timeouts. Treat them as malformed input and warn-fall-back.
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
        # OpenAI SDK as an HTTP timeout of 0/negative and either
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

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_base_url_is_rejected(self, value):
        """#846: startup resolves ``EMBED_BASE_URL`` (``default`` included)
        before building the embedder, so an empty URL here is a caller
        bug. It must fail closed rather than let the SDK pick an endpoint
        (``OPENAI_BASE_URL``, then OpenAI proper). Fixed text, no key."""
        marker = "SYNTHETIC_EMBED_KEY"  # pragma: allowlist secret
        with pytest.raises(ValueError) as exc:
            _make_embedder(base_url=value, api_key=marker)
        assert str(exc.value) == (
            "OpenAIEmbedder needs a resolved base URL; startup resolves "
            "EMBED_BASE_URL (or `default`) before building it."
        )
        assert marker not in str(exc.value)

    def test_base_url_is_passed_explicitly_to_the_sdk(self, monkeypatch):
        from openai import OpenAI as real_openai

        calls: list[dict] = []

        def recording_openai(**kwargs):
            calls.append(kwargs)
            return real_openai(**kwargs)

        monkeypatch.setattr("src.embedder.OpenAI", recording_openai)
        _make_embedder(base_url="https://api.openai.com/v1/")
        [kwargs] = calls
        assert kwargs["base_url"] == "https://api.openai.com/v1"

    def test_ambient_openai_base_url_cannot_redirect(self, monkeypatch, caplog):
        """#339/#846: a stray ``OPENAI_BASE_URL``, even one carrying a
        credential, must neither change the endpoint nor reach a log."""
        marker = "SYNTHETIC_URL_CREDENTIAL"
        monkeypatch.setenv("OPENAI_BASE_URL", f"https://user:{marker}@provider.invalid/v1")
        with caplog.at_level("DEBUG"):
            emb = _make_embedder(base_url="https://api.openai.com/v1")
        assert emb.base_url == "https://api.openai.com/v1"
        assert marker not in caplog.text

    def test_endpoint_with_userinfo_is_rejected(self, caplog):
        """The resolved URL reaches the startup log and error messages, so
        a ``user:pass@host`` endpoint is refused without echoing it."""
        marker = "SYNTHETIC_URL_CREDENTIAL"
        with caplog.at_level("DEBUG"), pytest.raises(ValueError, match="credentials") as exc:
            _make_embedder(base_url=f"https://user:{marker}@provider.invalid/v1")
        assert marker not in str(exc.value)
        assert marker not in caplog.text

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

    def test_malformed_indices_are_not_quoted_in_the_error(self):
        # A 200 response whose ``index`` echoes submitted text must not
        # carry it into logs or ``indexing_jobs.last_error`` (#224).
        emb = _make_embedder()
        marker = "SYNTHETIC_PRIVATE_MAIL"
        for indices in ([marker], [0, 0], [0, 5]):

            def fake_create(_indices=indices, **_kwargs):
                return SimpleNamespace(
                    data=[SimpleNamespace(embedding=[1.0], index=i) for i in _indices],
                )

            _patch_create(emb, fake_create)
            with pytest.raises(EmbedResponseError) as exc:
                emb.embed_batch(["x"] * len(indices))
            scrubbed = scrub_embed_error(exc.value)
            assert "EmbedResponseError" in scrubbed
            assert marker not in scrubbed
            assert "5" not in str(exc.value)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_vector_values_are_rejected(self, bad):
        # sqlite-vec stores NaN, and the row then breaks semantic
        # search; the job must not be marked indexed (#232).
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[1.0, 0.0], [0.0, bad]])

        _patch_create(emb, fake_create)
        with pytest.raises(EmbedResponseError, match="non-finite"):
            emb.embed_batch(["a", "b"])

    def test_all_zero_vector_is_rejected(self):
        # A zero vector passes the finiteness check and l2_normalize keeps
        # it (internal placeholders use zeros), so without this check a
        # provider returning zeros would settle the job with an unusable
        # semantic index (#304).
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[1.0, 0.0], [0.0, -0.0]])

        _patch_create(emb, fake_create)
        with pytest.raises(EmbedResponseError, match="all-zero") as exc:
            emb.embed_batch(["a", "b"])
        assert classify_embed_failure(exc.value) == EMBED_FAILURE_UNCERTAIN
        assert not _is_transient_embed_error(exc.value)

    def test_nonzero_vectors_still_pass(self):
        emb = _make_embedder()

        def fake_create(**_kwargs):
            return _embed_response([[0.0, 2.0], [1e-30, 0.0]])

        _patch_create(emb, fake_create)
        assert emb.embed_batch(["a", "b"]) == [[0.0, 1.0], [1.0, 0.0]]

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

    def test_on_batch_complete_fires_once_per_internal_batch(self):
        # A large cross-message embed splits internally at
        # ``batch_size``; the callback is the indexer's hook for
        # refreshing the health-file heartbeat between internal
        # batches so a slow cloud embedder cannot age the health
        # file past HEALTH_MAX_AGE_SECONDS during a single call.
        emb = _make_embedder(batch_size=2)

        def fake_create(**kwargs):
            n = len(kwargs["input"])
            return _embed_response([[1.0]] * n)

        _patch_create(emb, fake_create)
        ticks = {"n": 0}

        def on_tick():
            ticks["n"] += 1

        emb.embed_batch(["a", "b", "c", "d", "e"], on_batch_complete=on_tick)
        # 5 inputs / batch_size=2 → 3 internal batches.
        assert ticks["n"] == 3

    def test_on_batch_complete_not_fired_on_failure(self):
        # If ``_embed_one_batch`` exhausts its retries the callback
        # must NOT fire — touching the health file for a failed embed
        # would mask an actual outage from the watchdog.
        emb = _make_embedder(batch_size=2)

        def fake_create(**_kwargs):
            raise _api_status_error(500)

        _patch_create(emb, fake_create)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        ticks = {"n": 0}

        def on_tick():
            ticks["n"] += 1

        with pytest.raises(APIStatusError):
            emb.embed_batch(["a", "b"], on_batch_complete=on_tick)
        assert ticks["n"] == 0

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


class TestEmbedConcurrency:
    """``concurrency`` > 1 keeps that many requests of one ``embed_batch``
    call in flight (#713); 1 keeps the sequential path."""

    @staticmethod
    def _echo_create(calls: list[list[str]] | None = None, delay: float = 0.0):
        # One distinct vector per text (``float(text)``) so a misaligned
        # reassembly shows up in the output.
        def fake_create(**kwargs):
            if calls is not None:
                calls.append(list(kwargs["input"]))
            if delay:
                time.sleep(delay)
            return _embed_response([[float(t), 1.0] for t in kwargs["input"]])

        return fake_create

    def test_rejects_concurrency_below_one(self):
        with pytest.raises(ValueError, match="concurrency"):
            _make_embedder(concurrency=0)

    def test_default_concurrency_sends_requests_on_the_calling_thread(self):
        emb = _make_embedder(batch_size=1)
        threads: list[int] = []

        def fake_create(**kwargs):
            threads.append(threading.get_ident())
            return _embed_response([[1.0]] * len(kwargs["input"]))

        _patch_create(emb, fake_create)
        emb.embed_batch(["1", "2", "3"])
        assert threads == [threading.get_ident()] * 3

    def test_requests_overlap_up_to_concurrency(self):
        # Two requests meet at a barrier: a sequential path would time it
        # out. The peak in-flight count shows the pool never exceeds the
        # limit across six requests.
        emb = _make_embedder(batch_size=1, concurrency=2)
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        state = {"now": 0, "peak": 0, "calls": 0}

        def fake_create(**kwargs):
            with lock:
                state["now"] += 1
                state["calls"] += 1
                state["peak"] = max(state["peak"], state["now"])
                first_two = state["calls"] <= 2
            if first_two:
                barrier.wait()
            time.sleep(0.01)
            with lock:
                state["now"] -= 1
            return _embed_response([[float(t), 1.0] for t in kwargs["input"]])

        _patch_create(emb, fake_create)
        texts = [str(i) for i in range(6)]
        out = emb.embed_batch(texts)
        assert state["calls"] == 6
        assert state["peak"] == 2
        assert out == [l2_normalize([float(t), 1.0]) for t in texts]

    def test_reassembles_vectors_in_input_order_when_requests_finish_out_of_order(
        self, monkeypatch
    ):
        # The first request returns only once the other two requests'
        # futures are done (#758), so completion order really is 3, 5, 1:
        # an event set inside the fake would fire before those futures
        # complete. The patched pool keeps the futures it hands out.
        import src.embedder as embedder_module

        emb = _make_embedder(batch_size=2, concurrency=3)
        real_pool = embedder_module.ThreadPoolExecutor

        class _KeepFutures(real_pool):
            futures: list = []

            def submit(self, fn, /, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                type(self).futures.append(future)
                return future

        monkeypatch.setattr(embedder_module, "ThreadPoolExecutor", _KeepFutures)
        others_done_first: list[bool] = []

        def fake_create(**kwargs):
            if kwargs["input"][0] == "1":
                deadline = time.monotonic() + 5
                while len(_KeepFutures.futures) < 3 and time.monotonic() < deadline:
                    time.sleep(0.001)
                done, _ = wait(_KeepFutures.futures[1:3], timeout=5)
                others_done_first.append(len(done) == 2)
            return _embed_response([[float(t), 1.0] for t in kwargs["input"]])

        _patch_create(emb, fake_create)
        texts = [str(i) for i in range(1, 7)]
        assert emb.embed_batch(texts) == [l2_normalize([float(t), 1.0]) for t in texts]
        assert others_done_first == [True]

    def test_on_batch_complete_fires_per_request_on_the_calling_thread(self):
        # The indexer's callback touches SQLite (``queue.note_progress``),
        # so it must not run on a pool thread.
        emb = _make_embedder(batch_size=2, concurrency=3)
        _patch_create(emb, self._echo_create())
        ticks: list[int] = []
        emb.embed_batch(
            [str(i) for i in range(5)],
            on_batch_complete=lambda: ticks.append(threading.get_ident()),
        )
        assert ticks == [threading.get_ident()] * 3

    def test_first_failure_raises_and_submits_no_further_requests(self):
        # Requests are submitted only as earlier ones finish, so after the
        # first failure nothing new starts: of six requests at concurrency
        # 2, only the two submitted up front ever run.
        emb = _make_embedder(batch_size=1, concurrency=2)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        calls: list[str] = []
        lock = threading.Lock()
        second_started = threading.Event()

        def fake_create(**kwargs):
            with lock:
                calls.append(kwargs["input"][0])
            if kwargs["input"][0] == "0":
                # Fail only once both up-front requests are in flight.
                assert second_started.wait(timeout=5)
                raise _api_status_error(400)
            second_started.set()
            time.sleep(0.05)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        ticks = {"n": 0}

        def on_tick():
            ticks["n"] += 1

        with pytest.raises(APIStatusError):
            emb.embed_batch([str(i) for i in range(6)], on_batch_complete=on_tick)
        assert sorted(calls) == ["0", "1"]
        # The failed request never ticks; the one still in flight is
        # waited for but its result is discarded.
        assert ticks["n"] <= 1

    def test_failure_finished_alongside_a_reported_success_stops_new_requests(self, monkeypatch):
        # Review round 1: ``wait`` can report one finished request while
        # another has already failed. The failure must be seen before the
        # freed slot is refilled. The patched ``wait`` reproduces that
        # interleaving: it waits for both requests but reports only the
        # successful one.
        import src.embedder as embedder_module

        real_wait = embedder_module.wait
        first_call = [True]

        def wait_reporting_only_success(fs, return_when=None):
            if not first_call[0]:
                return real_wait(fs, return_when=return_when)
            first_call[0] = False
            done, _ = real_wait(fs, return_when=ALL_COMPLETED)
            succeeded = {f for f in done if f.exception() is None}
            return succeeded, set(fs) - succeeded

        monkeypatch.setattr(embedder_module, "wait", wait_reporting_only_success)
        emb = _make_embedder(batch_size=1, concurrency=2)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        calls: list[str] = []
        lock = threading.Lock()

        second_started = threading.Event()

        def fake_create(**kwargs):
            with lock:
                calls.append(kwargs["input"][0])
            if kwargs["input"][0] == "1":
                second_started.set()
                raise _api_status_error(400)
            # Finish only once both requests are in flight.
            assert second_started.wait(timeout=5)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        with pytest.raises(APIStatusError):
            emb.embed_batch([str(i) for i in range(4)])
        assert sorted(calls) == ["0", "1"]

    def test_failure_during_the_progress_callback_stops_new_requests(self):
        # Review round 2: a request that fails while ``on_batch_complete``
        # runs (the indexer's writes SQLite) must be seen before the
        # freed slot is refilled. Request "1" fails only once the
        # callback for request "0" has started, and the callback returns
        # only after that failure is recorded.
        emb = _make_embedder(batch_size=1, concurrency=2)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        callback_started = threading.Event()
        second_started = threading.Event()
        calls: list[str] = []
        lock = threading.Lock()
        failed: list = []

        def fake_create(**kwargs):
            text = kwargs["input"][0]
            with lock:
                calls.append(text)
            if text == "1":
                second_started.set()
                assert callback_started.wait(timeout=5)
                raise _api_status_error(400)
            # Finish only once both requests are in flight.
            assert second_started.wait(timeout=5)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        original = emb._embed_one_batch

        def tracked(chunk):
            try:
                return original(chunk)
            except Exception:
                failed.append(chunk)
                raise

        emb._embed_one_batch = tracked  # type: ignore[method-assign]

        def on_tick():
            if not callback_started.is_set():
                callback_started.set()
                deadline = time.monotonic() + 5
                while not failed and time.monotonic() < deadline:
                    time.sleep(0.001)
                # Let the pool record the exception on the future.
                time.sleep(0.05)

        with pytest.raises(APIStatusError):
            emb.embed_batch([str(i) for i in range(4)], on_batch_complete=on_tick)
        assert sorted(calls) == ["0", "1"]

    def test_failure_between_the_last_check_and_submit_sends_no_request(self, monkeypatch):
        """#720: a request that fails after the calling thread's last
        check and before the next submit must not let that next request
        reach the provider. The patched pool's third ``submit`` lets
        request "0" fail and waits until its future is done, which is
        after the failure is recorded, before submitting request "2"."""
        import src.embedder as embedder_module

        emb = _make_embedder(batch_size=1, concurrency=2)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        calls: list[str] = []
        lock = threading.Lock()
        release_first = threading.Event()

        def fake_create(**kwargs):
            text = kwargs["input"][0]
            with lock:
                calls.append(text)
            if text == "0":
                assert release_first.wait(timeout=5)
                raise _api_status_error(400)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)

        real_pool = embedder_module.ThreadPoolExecutor

        class _FailBeforeThirdSubmit(real_pool):
            submits = 0
            futures: list = []

            def submit(self, fn, /, *args, **kwargs):
                type(self).submits += 1
                if type(self).submits == 3:
                    release_first.set()
                    # Done only once the worker has recorded the failure
                    # and re-raised it: no timing assumption.
                    done, _ = wait([type(self).futures[0]], timeout=5)
                    assert done
                future = super().submit(fn, *args, **kwargs)
                type(self).futures.append(future)
                return future

        monkeypatch.setattr(embedder_module, "ThreadPoolExecutor", _FailBeforeThirdSubmit)
        with pytest.raises(APIStatusError):
            emb.embed_batch([str(i) for i in range(4)])
        assert _FailBeforeThirdSubmit.submits == 3
        assert sorted(calls) == ["0", "1"]

    def test_a_retry_after_another_request_failed_is_not_sent(self):
        """Codex round 2 on #749: the failure check runs right before
        every provider call, retries included. Request "1" hits a
        transient error and backs off; while it waits, request "0"
        fails. When "1" wakes, its retry must not reach the provider."""
        emb = _make_embedder(batch_size=1, concurrency=2)
        req = httpx2.Request("POST", "http://x")
        calls: list[str] = []
        lock = threading.Lock()
        first_attempt_of_1 = threading.Event()
        zero_failed = threading.Event()

        def fake_create(**kwargs):
            text = kwargs["input"][0]
            with lock:
                calls.append(text)
            if text == "0":
                assert first_attempt_of_1.wait(timeout=5)
                raise _api_status_error(400)
            first_attempt_of_1.set()
            raise APIConnectionError(request=req)

        _patch_create(emb, fake_create)

        def backoff_until_zero_failed(retry_state):
            # Request "1" sleeps here; "0" fails meanwhile.
            assert zero_failed.wait(timeout=5)
            return 0

        emb._embed_one_batch.retry.wait = backoff_until_zero_failed  # type: ignore[attr-defined]
        original = emb._embed_one_batch

        def tracked(chunk):
            try:
                return original(chunk)
            except APIStatusError:
                zero_failed.set()
                raise

        emb._embed_one_batch = tracked  # type: ignore[method-assign]
        with pytest.raises(APIStatusError):
            emb.embed_batch(["0", "1"])
        assert sorted(calls) == ["0", "1"]

    def test_each_concurrent_request_keeps_its_own_retry(self):
        emb = _make_embedder(batch_size=1, concurrency=2)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def fake_create(**kwargs):
            text = kwargs["input"][0]
            with lock:
                attempts[text] = attempts.get(text, 0) + 1
                first = attempts[text] == 1
            if text == "2" and first:
                raise _api_status_error(429)
            return _embed_response([[float(text), 1.0]])

        _patch_create(emb, fake_create)
        texts = ["1", "2", "3"]
        assert emb.embed_batch(texts) == [l2_normalize([float(t), 1.0]) for t in texts]
        assert attempts == {"1": 1, "2": 2, "3": 1}


class TestRetryPredicate:
    def test_retries_5xx_status_error(self):
        assert _is_transient_embed_error(_api_status_error(500)) is True
        assert _is_transient_embed_error(_api_status_error(503)) is True

    def test_does_not_retry_4xx_status_error(self):
        # 4xx is auth/quota/model-id config — retrying buys nothing.
        assert _is_transient_embed_error(_api_status_error(401)) is False
        assert _is_transient_embed_error(_api_status_error(400)) is False

    def test_retries_rate_limit_and_request_timeout(self):
        # 429 and 408 are the two 4xx a later attempt can fix: the
        # provider is throttling or timed out, not rejecting the
        # request. Treating them as config errors would stall indexing
        # as "operator action required" during an ordinary rate limit.
        assert _is_transient_embed_error(_api_status_error(429)) is True
        assert _is_transient_embed_error(_api_status_error(408)) is True
        assert _is_transient_embed_error(_api_status_error(403)) is False
        assert _is_transient_embed_error(_api_status_error(404)) is False
        assert _is_transient_embed_error(_api_status_error(422)) is False

    def test_retries_connection_error(self):
        # APIConnectionError requires a Request to construct.
        req = httpx2.Request("POST", "http://x")
        exc = APIConnectionError(request=req)
        assert _is_transient_embed_error(exc) is True

    def test_retries_timeout_error(self):
        req = httpx2.Request("POST", "http://x")
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
        req = httpx2.Request("POST", "http://x")

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
        with pytest.raises(RuntimeError, match="status=401"):
            emb.wait_for_ready(timeout=5)

    def test_times_out_when_never_responds(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        emb = _make_embedder()
        req = httpx2.Request("POST", "http://x")

        def fake_create(**_kwargs):
            raise APIConnectionError(request=req)

        _patch_warmup(emb, fake_create)
        with pytest.raises(RuntimeError, match="did not become ready"):
            emb.wait_for_ready(timeout=1)

    def test_uses_fast_probe_interval_initially_then_slow(self, monkeypatch):
        # Fast initial probes catch a remote provider that responds in
        # <1 s without burning ~3 s of startup latency on every cold
        # start; once the fast budget is spent we back off to the slow
        # interval so a multi-minute host warmup doesn't spam logs.
        #
        # Patch BOTH ``time.sleep`` and ``time.monotonic``: the deadline
        # check in ``wait_for_ready`` reads ``time.monotonic`` and
        # would otherwise depend on real wall-clock time advancing
        # during the loop, making the test flaky on heavily loaded CI
        # runners (12+ probes taking >10s real time would trip the
        # deadline check before the side-effect raise).
        sleeps: list[float] = []
        clock = {"t": 1000.0}

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock["t"] += s

        monkeypatch.setattr(time, "sleep", fake_sleep)
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        emb = _make_embedder()
        req = httpx2.Request("POST", "http://x")
        # Always fail with a transient error so the loop keeps probing
        # until the side-effect-raised fatal error stops it. Counter
        # raises after enough probes to exercise both cadence bands.
        attempts = {"n": 0}

        def fake_create(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] > emb._FAST_PROBE_COUNT + 2:
                raise APIStatusError(
                    message="fatal",
                    response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")),
                    body=None,
                )
            raise APIConnectionError(request=req)

        _patch_warmup(emb, fake_create)
        # Timeout chosen well above the simulated 11s of probe sleeps
        # (10 fast x 0.5s + 2 slow x 3.0s = 11s) so the deadline check
        # never fires under the patched clock.
        with pytest.raises(RuntimeError, match="status=400"):
            emb.wait_for_ready(timeout=60)
        # First FAST_PROBE_COUNT sleeps should use the fast interval;
        # subsequent sleeps should use the slow interval.
        assert (
            sleeps[: emb._FAST_PROBE_COUNT]
            == [emb._FAST_PROBE_INTERVAL_SECS] * emb._FAST_PROBE_COUNT
        )
        assert sleeps[emb._FAST_PROBE_COUNT :] == [emb._SLOW_PROBE_INTERVAL_SECS] * (
            len(sleeps) - emb._FAST_PROBE_COUNT
        )


_MARKER = "MARKER-686"


def _marked_status_error(status_code: int) -> APIStatusError:
    """A status error whose provider body and message carry ``_MARKER``,
    as the SDK builds one from a JSON error response (#686)."""
    body = {"error": {"message": f"provider text {_MARKER}", "type": "invalid_request_error"}}
    return APIStatusError(
        message=f"Error code: {status_code} - {body}",
        response=httpx2.Response(
            status_code, json=body, request=httpx2.Request("POST", "http://x")
        ),
        body=body,
    )


def _assert_scrubbed(exc: BaseException, caplog, status_code: int) -> None:
    rendered = "".join(traceback.format_exception(exc))
    for text in (str(exc), repr(exc), rendered, caplog.text):
        assert _MARKER not in text
    assert f"APIStatusError: status={status_code}" in str(exc)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ or exc.__context__ is None


class TestWaitForReadyScrubsProviderText:
    """``wait_for_ready`` is the one startup embed call that reached the
    container log without ``scrub_embed_error``: a non-transient status
    error propagated with the provider's response body, and the deadline
    error embedded the last error's repr (#686)."""

    def test_non_transient_status_error_keeps_only_type_and_status(self, caplog):
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        calls = {"n": 0}

        def fake_create(**_kwargs):
            calls["n"] += 1
            raise _marked_status_error(402)

        _patch_warmup(emb, fake_create)
        with pytest.raises(RuntimeError) as exc_info:
            emb.wait_for_ready(timeout=5)
        assert calls["n"] == 1
        _assert_scrubbed(exc_info.value, caplog, 402)
        assert "API key" in str(exc_info.value)

    def test_deadline_error_keeps_only_type_and_status(self, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        clock = {"t": 1000.0}

        def fake_sleep(s: float) -> None:
            clock["t"] += s

        monkeypatch.setattr(time, "sleep", fake_sleep)
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        emb = _make_embedder()
        calls = {"n": 0}

        def fake_create(**_kwargs):
            calls["n"] += 1
            raise _marked_status_error(503)

        _patch_warmup(emb, fake_create)
        with pytest.raises(RuntimeError, match="did not become ready") as exc_info:
            emb.wait_for_ready(timeout=2)
        assert calls["n"] >= 2
        _assert_scrubbed(exc_info.value, caplog, 503)


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

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_rejects_non_finite_vectors(self, bad):
        # The DB write boundary normalizes vectors from any backend, so
        # this is where a fake or future backend's NaN is stopped (#232).
        with pytest.raises(ValueError, match="non-finite"):
            l2_normalize([bad, 0.0])

    def test_keeps_zero_placeholder(self):
        assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


class TestClassifyEmbedFailure:
    """Attribution policy for queue accounting — who is at fault —
    deliberately separate from the retry predicate above, which only
    asks whether re-sending the same request could succeed."""

    def test_transport_and_throttling_are_infrastructure(self):
        req = httpx2.Request("POST", "http://x")
        for exc in (
            APIConnectionError(request=req),
            APITimeoutError(request=req),
            _api_status_error(429),
            _api_status_error(408),
        ):
            assert classify_embed_failure(exc) == EMBED_FAILURE_INFRASTRUCTURE

    def test_auth_and_model_errors_are_configuration(self):
        for code in (401, 403, 404):
            assert classify_embed_failure(_api_status_error(code)) == EMBED_FAILURE_CONFIGURATION

    def test_request_rejections_are_rejected_input(self):
        for code in (400, 413, 422):
            assert classify_embed_failure(_api_status_error(code)) == EMBED_FAILURE_REJECTED_INPUT

    def test_server_errors_and_unknowns_are_uncertain(self):
        assert classify_embed_failure(_api_status_error(500)) == EMBED_FAILURE_UNCERTAIN
        assert classify_embed_failure(_api_status_error(503)) == EMBED_FAILURE_UNCERTAIN
        assert classify_embed_failure(_api_status_error(409)) == EMBED_FAILURE_UNCERTAIN
        assert classify_embed_failure(RuntimeError("integrity")) == EMBED_FAILURE_UNCERTAIN


class TestScrubEmbedError:
    def test_status_error_keeps_type_and_status_only(self):
        assert scrub_embed_error(_api_status_error(400)) == "APIStatusError: status=400"

    def test_connection_error_keeps_detail(self):
        exc = APIConnectionError(request=httpx2.Request("POST", "http://x"))
        assert "Connection error" in scrub_embed_error(exc)

    def test_other_exceptions_keep_type_only(self):
        # SDK response parsing (pydantic) and vector conversion errors
        # can quote response values that echo the submitted text.
        scrubbed = scrub_embed_error(ValueError("input_value='SYNTHETIC_PRIVATE_MAIL'"))
        assert scrubbed == "ValueError"


_CHUNK_MARKER = "SYNTHETIC_CHUNK"


class TestRedirectPolicy:
    """#340: the OpenAI SDK re-sends the request body on a 307/308, so a
    redirecting embed endpoint must not receive chunk text at another
    origin. Only the HTTP transport is replaced."""

    def _embedder_with_redirect(self, location: str):
        seen: list = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx2.Response(307, headers={"location": location})
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.6, 0.8]}],
                    "model": "synthetic",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )

        emb = _make_embedder(base_url="http://host.docker.internal:8001/v1")
        http_client = emb.client._client
        http_client._transport = httpx2.MockTransport(handler)
        http_client._mounts = {}
        return emb, seen

    def test_cross_origin_redirect_is_not_followed(self, caplog, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        emb, seen = self._embedder_with_redirect("https://different-origin.invalid/v1/embeddings")
        with pytest.raises(Exception) as err:
            emb.embed(_CHUNK_MARKER)
        assert seen
        assert {r.url.host for r in seen} == {"host.docker.internal"}
        assert "redirected to a different origin" in caplog.text
        assert "different-origin.invalid" not in caplog.text
        assert _CHUNK_MARKER not in str(err.value)
        assert _CHUNK_MARKER not in caplog.text

    def test_same_origin_redirect_is_followed(self):
        emb, seen = self._embedder_with_redirect(
            "http://host.docker.internal:8001/v1/embeddings?moved=1"
        )
        assert emb.embed(_CHUNK_MARKER) == pytest.approx([0.6, 0.8])
        assert len(seen) == 2
        assert _CHUNK_MARKER in seen[1].content.decode()


def main_log() -> logging.Logger:
    return logging.getLogger("indexer.test_filler")


class TestRetryLogging:
    """#873: each retry of an embed request is logged, so a rate limit
    that slows indexing leaves a trace even when the retry succeeds."""

    _MARKER = "SYNTHETIC_RETRY_MARKER"

    def _status_error(self, status_code: int) -> APIStatusError:
        # The provider's body and message echo the submitted text.
        return APIStatusError(
            message=f"{status_code} {self._MARKER}",
            response=httpx2.Response(status_code, request=httpx2.Request("POST", "http://x")),
            body={"error": self._MARKER},
        )

    def test_each_retry_logs_attempt_and_scrubbed_error(self, caplog):
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        attempts = {"n": 0}

        def fake_create(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise self._status_error(429)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        assert emb.embed_batch([self._MARKER]) == [[1.0]]

        lines = [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("embed retry")
        ]
        assert lines == [
            (logging.INFO, "embed retry attempt=2/3 after APIStatusError: status=429"),
            (logging.WARNING, "embed retry attempt=3/3 after APIStatusError: status=429"),
        ]
        assert self._MARKER not in caplog.text

    def test_a_request_that_succeeds_first_time_logs_no_retry(self, caplog):
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        _patch_create(emb, lambda **_kw: _embed_response([[1.0]]))
        emb.embed_batch(["x"])
        assert "embed retry" not in caplog.text
        assert "embed request recovered" not in caplog.text

    def test_a_request_that_succeeds_after_retrying_logs_recovery(self, caplog):
        """A retried request that then succeeds logs exactly one recovery
        line, so a retry line is never the last word on that request
        (Codex round 1 on #904)."""
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        attempts = {"n": 0}

        def fake_create(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise self._status_error(503)
            return _embed_response([[1.0]])

        _patch_create(emb, fake_create)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        assert emb.embed_batch([self._MARKER]) == [[1.0]]

        lines = [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith(("embed retry", "embed request recovered"))
        ]
        assert lines == [
            (logging.INFO, "embed retry attempt=2/3 after APIStatusError: status=503"),
            (logging.INFO, "embed request recovered on attempt 2/3"),
        ]
        assert self._MARKER not in caplog.text

    def test_recovery_is_logged_whenever_its_retry_line_was(self, caplog):
        """Codex round 3 on #904: with the shared budget spent by the
        retry line itself, the recovery must still follow it, or the
        retry line is the last word on a request that went through. A
        request whose retry line was withheld has nothing to answer."""
        from src import extractors

        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        attempts = {"n": 0}

        def flaky_once(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] % 2:
                raise self._status_error(503)
            return _embed_response([[1.0]])

        _patch_create(emb, flaky_once)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        # Spend all but one slot; the last slot goes to the retry line.
        for _ in range(extractors._WARNINGS_PER_WINDOW - 1):
            extractors.warn_rate_limited(main_log(), "filler", attachment=False)
        caplog.clear()

        emb.embed_batch(["x"])  # retry logged on the last slot, recovery follows
        emb.embed_batch(["y"])  # retry withheld, so no recovery either

        lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith(("embed retry", "embed request recovered"))
        ]
        assert lines == [
            "embed retry attempt=2/3 after APIStatusError: status=503",
            "embed request recovered on attempt 2/3",
        ]
        assert extractors.drain_suppressed_lines() == 1

    def test_a_logged_retry_does_not_leak_into_the_next_request(self, caplog):
        """The flag a logged retry sets is per request: a request that
        exhausts its retries must not make the next first-try success
        log a recovery."""
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        attempts = {"n": 0}

        def fail_three_then_pass(**_kwargs):
            attempts["n"] += 1
            if attempts["n"] <= 3:
                raise self._status_error(503)
            return _embed_response([[1.0]])

        _patch_create(emb, fail_three_then_pass)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        with pytest.raises(APIStatusError):
            emb.embed_batch(["x"])
        emb.embed_batch(["y"])
        assert "embed request recovered" not in caplog.text

    def test_a_request_that_exhausts_its_retries_logs_no_recovery(self, caplog):
        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()
        _patch_create(emb, lambda **_kw: (_ for _ in ()).throw(self._status_error(503)))
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        with pytest.raises(APIStatusError):
            emb.embed_batch([self._MARKER])
        assert "embed request recovered" not in caplog.text
        assert self._MARKER not in caplog.text

    def test_retry_lines_share_the_rate_limit(self, caplog):
        """A sustained rate limit retries every request; past the shared
        per-window budget the lines are counted, not logged."""
        from src import extractors

        caplog.set_level(logging.DEBUG)
        emb = _make_embedder()

        def fake_create(**_kwargs):
            raise self._status_error(503)

        _patch_create(emb, fake_create)
        emb._embed_one_batch.retry.wait = lambda *_a, **_kw: 0  # type: ignore[attr-defined]
        requests = extractors._WARNINGS_PER_WINDOW  # two retry lines each
        for _ in range(requests):
            with pytest.raises(APIStatusError):
                emb.embed_batch(["x"])

        logged = [r for r in caplog.records if r.getMessage().startswith("embed retry")]
        assert len(logged) == extractors._WARNINGS_PER_WINDOW
        # Codex round 2 on #904: suppressed embed lines are counted apart
        # from the attachment WARNINGs, so they never make the attachments
        # aggregate a WARNING.
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 0
        assert extractors.drain_suppressed_lines() == requests


class TestEmbedRetryAfterSeconds:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [("7", 7.0), ("0", 0.0), ("1.5", 1.5), ("-1", None), ("nan", None), ("soon", None)],
    )
    def test_reads_delta_seconds_only(self, header, expected):
        from src.embedder import embed_retry_after_seconds

        exc = APIStatusError(
            message="429 error",
            response=httpx2.Response(
                429, headers={"Retry-After": header}, request=httpx2.Request("POST", "http://x")
            ),
            body=None,
        )
        assert embed_retry_after_seconds(exc) == expected

    def test_absent_header_and_other_errors_give_none(self):
        from src.embedder import embed_retry_after_seconds

        assert embed_retry_after_seconds(_api_status_error(429)) is None
        assert embed_retry_after_seconds(ValueError("x")) is None


def test_is_rate_limit_error_is_429_only():
    from src.embedder import is_rate_limit_error

    assert is_rate_limit_error(_api_status_error(429)) is True
    assert is_rate_limit_error(_api_status_error(408)) is False
    assert is_rate_limit_error(ValueError("x")) is False

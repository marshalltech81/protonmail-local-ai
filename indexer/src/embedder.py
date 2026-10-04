"""Embedder — generates vector embeddings via an OpenAI-compatible
``/v1/embeddings`` endpoint.

A single client class talks to any provider that speaks the OpenAI
embeddings wire format: a remote provider (DeepInfra, OpenRouter,
etc.) or a host-side server the operator installs themselves
(LM Studio, vLLM, ``mlx_lm.server``, TEI, etc.). The choice of
backend is purely a base-URL + model + API key configuration question
— there is no per-provider code path.

Calls go through the official ``openai`` SDK with a custom ``base_url``;
operator-supplied compat servers target the SDK as their reference
client by design, so pointing the SDK at them via ``base_url=`` is the
supported path.

``OpenAIEmbedder`` implements the ``EmbeddingBackend`` Protocol so
callers in ``main.py``, ``reconciler.py``, and ``attachment_indexing.py``
stay backend-agnostic and tests can substitute a duck-typed fake.
"""

import logging
import math
import os
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
)
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .chunker import l2_normalize

log = logging.getLogger("indexer.embedder")


def _float_env(name: str, default: float, minimum: float = 1.0) -> float:
    """Read a float env var with a graceful fallback.

    An empty / unset / malformed value logs a warning and falls back
    rather than raising at startup. Unlike ``main._int_env`` and the
    queue and reconciler loaders, which reject invalid values (#481),
    this warmup deadline keeps the warn-fall-back policy.

    ``minimum`` defines the lower bound (default 1.0) — values below
    it are treated as malformed and fall back.
    ``EMBED_WARMUP_TIMEOUT_SECS`` and similar per-operation HTTP
    timeouts must be positive: ``0`` or negative values would reach the
    OpenAI SDK timeout path and either fail oddly or make startup
    behavior brittle. Mirrors the
    ``mcp-server/src/main._float_env`` helper's ``minimum`` parameter,
    differing only in the warn-fall-back vs raise policy that each
    service has already settled on.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("invalid %s=%r; falling back to %.1f", name, raw, default)
        return default
    # ``float("nan")`` / ``float("inf")`` parse cleanly but would
    # reach the SDK as an HTTP timeout and break in surprising
    # ways. Same warn-fall-back policy as a malformed string.
    if not math.isfinite(value):
        log.warning("invalid %s=%r; falling back to %.1f", name, raw, default)
        return default
    if value < minimum:
        log.warning(
            "invalid %s=%r (must be >= %.1f); falling back to %.1f",
            name,
            raw,
            minimum,
            default,
        )
        return default
    return value


class EmbedResponseError(RuntimeError):
    """A successful embeddings response failed our integrity checks.

    Messages are fixed text plus counts: response fields can echo the
    submitted email text, so their values are never quoted.
    """


def scrub_embed_error(exc: BaseException) -> str:
    """Render an embedder exception into a log/DB-safe string.

    The OpenAI SDK's ``APIStatusError`` carries the provider's response
    body, which can echo input fragments back on 4xx — for the indexer
    that means email body text from the failing batch can flow into
    ``indexing_jobs.last_error`` and any operator log sink. The repo
    is public and the ``last_error`` row + truncated log line both
    travel further than callers usually expect, so trim
    ``APIStatusError`` down to type + status_code only.

    Connection / timeout errors and our own ``EmbedResponseError``
    carry no email content, so their full ``repr`` is safe to keep.
    Anything else (SDK response parsing, vector conversion) can quote
    response values that echo the input, so only its type is kept.
    """
    if isinstance(exc, APIStatusError):
        return f"{type(exc).__name__}: status={exc.status_code}"
    if isinstance(exc, (APIConnectionError, EmbedResponseError)):
        return repr(exc)
    return type(exc).__name__


def _is_transient_embed_error(exc: BaseException) -> bool:
    """Decide whether tenacity should retry ``exc``.

    Retry transport-level failures and 5xx that a fresh attempt could
    plausibly fix:

    * ``openai.APIConnectionError`` — TCP / DNS / TLS failures the SDK
      surfaces uniformly.
    * ``openai.APITimeoutError`` — read / write / pool timeouts.
    * 5xx ``openai.APIStatusError`` — provider failed to serve a
      well-formed request and might recover.
    * 429 (rate limited) and 408 (request timeout) — the provider is
      throttling or slow, not rejecting the request.

    Do NOT retry — deterministic config errors that retrying only
    delays:

    * Other 4xx ``openai.APIStatusError`` (auth, model id, request
      shape).
    * Our own ``EmbedResponseError`` from index-integrity checks — the
      provider returned a malformed batch and a retry would produce
      the same shape.

    The indexer's outage detection (``main._drain_queue_batched``)
    uses this same predicate to decide whether a failed embedder probe
    is an outage (defer and back off) or a configuration problem
    (operator action required).
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500 or exc.status_code in (408, 429)
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return False


EMBED_FAILURE_INFRASTRUCTURE = "infrastructure"
EMBED_FAILURE_CONFIGURATION = "configuration"
EMBED_FAILURE_REJECTED_INPUT = "rejected_input"
EMBED_FAILURE_UNCERTAIN = "uncertain"


def classify_embed_failure(exc: BaseException) -> str:
    """Attribute an embed failure for queue accounting: whose fault is it?

    Deliberately separate from ``_is_transient_embed_error``, which only
    answers "could re-sending this exact request succeed?". A 429 is
    retryable AND not the message's fault; a 401 is not retryable AND
    still not the message's fault. Only a request rejection is evidence
    against the source itself.

    * ``infrastructure`` — connection / timeout / 408 / 429: the
      provider is unreachable, slow, or throttling.
    * ``configuration`` — 401 / 403 / 404: credentials or model id.
    * ``rejected_input`` — 400 / 413 / 422: the provider refused this
      particular request body.
    * ``uncertain`` — 5xx, our integrity ``EmbedResponseError``, anything
      else: could be the input or the provider; callers must gather
      more evidence (a fresh probe) before charging the message.
    """
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return EMBED_FAILURE_INFRASTRUCTURE
    if isinstance(exc, APIStatusError):
        if exc.status_code in (408, 429):
            return EMBED_FAILURE_INFRASTRUCTURE
        if exc.status_code in (401, 403, 404):
            return EMBED_FAILURE_CONFIGURATION
        if exc.status_code in (400, 413, 422):
            return EMBED_FAILURE_REJECTED_INPUT
    return EMBED_FAILURE_UNCERTAIN


class EmbeddingBackend(Protocol):
    """Structural contract the embedder satisfies.

    Defined as a ``Protocol`` (not an inheritance base) so test fakes
    can stay duck-typed without depending on the OpenAI SDK or any
    concrete backend.
    """

    def wait_for_ready(self, timeout: int = 120) -> None: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(
        self,
        texts: list[str],
        *,
        on_batch_complete: Callable[[], None] | None = None,
    ) -> list[list[float]]: ...


class OpenAIEmbedder:
    """OpenAI-SDK-backed ``/v1/embeddings`` client.

    For an unauthenticated host-side server, ``base_url`` is something
    like ``http://host.docker.internal:8001/v1`` and ``api_key`` is any
    operator-supplied placeholder string (e.g. ``unauthenticated``);
    compat servers ignore the auth header. For DeepInfra, ``base_url``
    is ``https://api.deepinfra.com/v1/openai``. For OpenAI proper,
    ``base_url`` may be left empty so the SDK's documented fallback
    fires (``OPENAI_BASE_URL`` env → ``https://api.openai.com/v1``);
    ``api_key`` (always required, non-empty) is the explicit-intent
    signal that makes empty-base_url unambiguous. The class itself is
    provider-agnostic.

    ``api_key`` must be non-empty — startup validation in
    ``main._validate_embed_config`` enforces that contract before the
    embedder is constructed. ``self.base_url`` is read back from the
    SDK after construction so it always reflects the wire endpoint,
    not the (possibly empty) value the operator typed.

    Project-specific behavior preserved over the bare SDK:

    - Custom retry classification (4xx config errors fail fast; 5xx
      and connection errors retry with exponential backoff).
    - Defensive index-integrity check on the batch response so a
      provider that ever returns reordered / duplicate indices fails
      loudly instead of silently misaligning vectors with chunks.
    - L2 normalization at the boundary so storage invariants hold
      regardless of provider normalization defaults.
    """

    # First-call model warmup may include a model load on a host-side
    # server (and a HuggingFace download on first run). Empirical
    # cold-start observations: Qwen3-Embedding-8B mxfp8 served via
    # ``mlx_lm.server`` ≈ 4 min from a cold HF cache, <30 s warm. Remote
    # providers usually respond in <1 s. The timeout (per-operation,
    # not a total deadline) sits comfortably above the cold-start case; operators on slow links can raise it
    # via ``EMBED_WARMUP_TIMEOUT_SECS``.
    DEFAULT_WARMUP_TIMEOUT_SECS = 600.0

    # Probe cadence for ``wait_for_ready``. Fast initial probes catch
    # a remote provider that responds in <1 s without burning ~3 s of
    # startup latency on every cold start; once the fast budget is
    # spent we back off to the slow interval so a multi-minute host
    # warmup doesn't generate hundreds of log lines.
    _FAST_PROBE_INTERVAL_SECS = 0.5
    _FAST_PROBE_COUNT = 10
    _SLOW_PROBE_INTERVAL_SECS = 3.0

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str,
        batch_size: int = 64,
        concurrency: int = 1,
        request_timeout: float = 120.0,
    ):
        if concurrency < 1:
            raise ValueError("embedder concurrency must be >= 1")
        self.model = model
        self.batch_size = batch_size
        self.concurrency = concurrency

        # ``api_key`` is required (non-empty) — startup validation in
        # ``main._validate_embed_config`` rejects an empty value before
        # this constructor runs. The key is the explicit-intent signal:
        # an operator with a real ``sk-...`` in
        # ``.secrets/embed_api_key.txt`` has unambiguously chosen their
        # provider, so an empty ``base_url`` is interpreted as "I want
        # the SDK default (OpenAI proper)" rather than "I forgot to
        # configure." For unauthenticated host-side servers the
        # operator supplies any placeholder string; compat servers
        # ignore the bearer header. Keeping the substitution out of
        # this constructor means the credential actually sent is
        # exactly what the operator wrote — no silent rewrite to a
        # literal that could surface in a misconfigured remote
        # provider's request log.
        #
        # ``base_url`` may be empty: omit the kwarg so the SDK's
        # documented fallback chain fires (``OPENAI_BASE_URL`` env →
        # ``https://api.openai.com/v1`` literal). The required
        # ``EMBED_API_KEY`` upstream guards against accidental
        # ship-to-OpenAI from a forgotten env var — a typo can't
        # produce a real bearer credential.
        #
        # ``max_retries=0`` because retry policy is owned by the
        # tenacity wrapper below — the SDK's built-in retry would
        # double-up exponential backoff and obscure the 4xx-fast-fail
        # / 5xx-retry classification.
        #
        # The SDK's HTTP client follows redirects and re-sends the body
        # (chunk text) to wherever ``Location`` points (#340). The request
        # hook runs before every hop and refuses one off the resolved
        # endpoint's origin before anything is sent; same-origin redirects
        # still work. The SDK reports the refusal as a connection error.
        def _same_origin_only(request) -> None:
            base = self.client.base_url
            if (request.url.scheme, request.url.host, request.url.port) != (
                base.scheme,
                base.host,
                base.port,
            ):
                log.warning("Embed provider redirected to a different origin; request not sent")
                raise RuntimeError("Embed provider redirected to a different origin")

        http_client = DefaultHttpxClient(event_hooks={"request": [_same_origin_only]})
        if base_url:
            self.client = OpenAI(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                timeout=request_timeout,
                max_retries=0,
                http_client=http_client,
            )
        else:
            self.client = OpenAI(
                api_key=api_key,
                timeout=request_timeout,
                max_retries=0,
                http_client=http_client,
            )
        # After the SDK resolves its fallback chain, read the URL back
        # so logs and error messages name the actual wire endpoint
        # (e.g. the SDK default) rather than the empty string the
        # operator typed.
        self.base_url = str(self.client.base_url).rstrip("/")
        # An empty ``base_url`` lets the SDK read ``OPENAI_BASE_URL``,
        # which bypasses the startup userinfo check on ``EMBED_BASE_URL``
        # (#339). Re-check the resolved endpoint before it reaches a log
        # line or an error message; the message never echoes the URL.
        if "@" in urllib.parse.urlsplit(self.base_url).netloc:
            raise ValueError(
                "EMBED_BASE_URL (or the SDK's OPENAI_BASE_URL) must not embed "
                "credentials (user:pass@host). Put the API key in "
                ".secrets/embed_api_key.txt instead."
            )

    def wait_for_ready(self, timeout: int = 120) -> None:
        """Block until the embedder accepts a real ``/v1/embeddings``
        request — covers a host-side server's startup window plus
        first-call model load, and a single fast probe for remote
        providers.

        Two independent deadlines:

        - ``timeout`` (default 120 s) bounds the **connect-phase** —
          how long we keep retrying transient transport failures
          before declaring the service unreachable. A service that
          isn't bound on the port should fail in ~``timeout`` seconds,
          not 10 minutes.
        - ``EMBED_WARMUP_TIMEOUT_SECS`` (default 600 s) is the HTTP
          timeout on **one warmup request** — once TCP connects, the
          provider can go this long without sending response data
          before the SDK times out, absorbing a multi-minute
          first-time HF model download. It is per-operation (each
          connect, read or write), not a total deadline for the
          request.

        Total wall-clock can exceed ``timeout`` only when a connection
        succeeded but the response is in flight. A 5xx after a long
        warmup wait still surfaces as failure because the connect
        deadline has by then passed.

        Retry classification delegates to ``_is_transient_embed_error``
        — the same predicate ``_embed_one_batch`` uses — so startup
        and runtime share one definition of "transient". 4xx auth /
        model / quota errors fail fast (deterministic config), as does
        any non-SDK exception. 5xx and the SDK's connection / timeout
        families retry until the connect deadline.
        """
        warmup_timeout = _float_env(
            "EMBED_WARMUP_TIMEOUT_SECS",
            self.DEFAULT_WARMUP_TIMEOUT_SECS,
        )
        log.info(
            "Waiting for embedder at %s (model=%s, connect_timeout=%ds, warmup_timeout=%.0fs)...",
            self.base_url,
            self.model,
            timeout,
            warmup_timeout,
        )
        # Monotonic clock: wall-clock jumps (NTP correction, container
        # host suspending) would otherwise let the deadline either fail
        # early or hang well past the configured timeout.
        connect_deadline = time.monotonic() + float(timeout)
        last_err: Exception | None = None
        probe_attempt = 0
        while time.monotonic() < connect_deadline:
            try:
                self.client.with_options(timeout=warmup_timeout).embeddings.create(
                    model=self.model,
                    input="warmup",
                )
                log.info("Embedder ready: %s (%s)", self.base_url, self.model)
                return
            except (APIConnectionError, APITimeoutError, APIStatusError) as e:
                if not _is_transient_embed_error(e):
                    # 4xx config errors and any other non-transient
                    # error surface immediately so the operator fixes
                    # config rather than waiting out the timeout. The
                    # SDK error carries the provider's response body,
                    # so keep only type + status, and ``from None`` so
                    # the traceback does not chain the original (#686).
                    raise RuntimeError(
                        f"embedder at {self.base_url} rejected the warmup request "
                        f"({scrub_embed_error(e)}); check the provider account, "
                        f"API key and EMBED_MODEL"
                    ) from None
                last_err = e
            probe_attempt += 1
            # Fast cadence for the first few probes catches a remote
            # provider that's already up without burning seconds of
            # startup latency; back off to the slow cadence so a
            # multi-minute host warmup doesn't spam logs.
            if probe_attempt <= self._FAST_PROBE_COUNT:
                interval = self._FAST_PROBE_INTERVAL_SECS
            else:
                interval = self._SLOW_PROBE_INTERVAL_SECS
            time.sleep(interval)
        last = scrub_embed_error(last_err) if last_err is not None else "none"
        raise RuntimeError(
            f"embedder at {self.base_url} did not become ready within {timeout}s "
            f"(last error: {last})"
        )

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single input."""
        return self.embed_batch([text])[0]

    def embed_batch(
        self,
        texts: list[str],
        *,
        on_batch_complete: Callable[[], None] | None = None,
    ) -> list[list[float]]:
        """Generate embedding vectors for a list of inputs.

        Splits ``texts`` into ``batch_size`` chunks and issues one
        OpenAI-compatible request per chunk. Per-chunk requests retry
        independently on 5xx / connection errors. Response ``data`` is
        sorted by ``index`` defensively so a future provider that
        reorders the array does not silently misalign vectors with
        their source texts.

        ``on_batch_complete``, when supplied, is invoked after each
        internal batch completes successfully. The indexer wires it to
        ``touch_health_file`` so a large cross-message embed that takes
        longer than the per-iteration health threshold still refreshes
        the heartbeat between internal batches instead of only at the
        outer call boundaries. A failure inside ``_embed_one_batch``
        propagates without invoking the callback.
        """
        if not texts:
            return []
        chunks = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        if self.concurrency == 1 or len(chunks) == 1:
            out: list[list[float]] = []
            for chunk in chunks:
                out.extend(self._embed_one_batch(chunk))
                if on_batch_complete is not None:
                    on_batch_complete()
            return out
        return self._embed_chunks_concurrently(chunks, on_batch_complete)

    def _embed_chunks_concurrently(
        self,
        chunks: list[list[str]],
        on_batch_complete: Callable[[], None] | None,
    ) -> list[list[float]]:
        """Embed ``chunks`` with up to ``concurrency`` requests in flight (#713).

        Only this thread submits requests, and only as earlier ones
        finish, so after the first failure nothing new starts. Requests
        still in flight are waited for (each is bounded by the request
        timeout and its retries) and their results discarded; the
        failure then propagates as on the sequential path.
        ``on_batch_complete`` runs here, not on a pool thread, because
        the indexer's callback writes SQLite.
        """
        results: list[list[list[float]] | None] = [None] * len(chunks)
        pending: dict[Future[list[list[float]]], int] = {}
        next_chunk = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while next_chunk < len(chunks) or pending:
                while next_chunk < len(chunks) and len(pending) < self.concurrency:
                    pending[pool.submit(self._embed_one_batch, chunks[next_chunk])] = next_chunk
                    next_chunk += 1
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    # ``result()`` re-raises the request's exception;
                    # leaving the ``with`` block waits for the rest.
                    results[pending.pop(future)] = future.result()
                    if on_batch_complete is not None:
                        on_batch_complete()
        return [
            vec for chunk_vectors in results if chunk_vectors is not None for vec in chunk_vectors
        ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_transient_embed_error),
        reraise=True,
    )
    def _embed_one_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self.client.embeddings.create(
            model=self.model,
            input=texts,
        )
        data = list(resp.data)
        # Hard-validate the index integrity before zipping vectors
        # back onto chunks. A provider that returns duplicate or
        # missing indices would silently attach the wrong vector to
        # the wrong chunk text — a far worse failure mode than a
        # raised exception, since the index commits and stores
        # mis-aligned vectors that survive every later restart.
        # Error messages never quote response values: they can echo the
        # submitted email text (#224).
        if len(data) != len(texts):
            raise EmbedResponseError(
                f"embedder returned {len(data)} vectors for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        indices = [d.index for d in data]
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in indices) or sorted(
            indices
        ) != list(range(len(texts))):
            raise EmbedResponseError(
                f"embedder returned non-integer, non-contiguous or duplicate indices "
                f"for {len(texts)} inputs ({self.base_url}, model={self.model!r})"
            )
        data.sort(key=lambda d: d.index)
        vectors = [list(d.embedding) for d in data]
        # sqlite-vec stores NaN / inf, and such a row breaks semantic
        # search, so reject the batch rather than commit it (#232).
        if not all(math.isfinite(x) for vec in vectors for x in vec):
            raise EmbedResponseError(
                f"embedder returned non-finite vector values for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        # A zero vector is finite and ``l2_normalize`` keeps it (internal
        # placeholders use zeros), so a provider returning zeros would
        # otherwise settle the job with an unusable semantic index (#304).
        if any(not any(vec) for vec in vectors):
            raise EmbedResponseError(
                f"embedder returned an all-zero vector for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        # Normalize raw provider output here so chunk vectors land
        # unit-normed regardless of provider. The DB write boundary
        # in ``database.py`` also normalizes at ``upsert_thread`` /
        # ``replace_thread_vector`` / ``_rewrite_thread_row``
        # (because ``mean_vector`` of unit chunk vectors generally
        # has norm < 1) and at ``replace_message_chunks`` (because
        # the ``EmbeddingBackend`` contract accepts arbitrary callers
        # — fakes, future non-OpenAI backends — that may not
        # normalize), so the storage invariant — every vector in
        # ``threads_vec`` / ``message_chunks_vec`` is unit-norm —
        # holds end-to-end. ``l2_normalize`` short-circuits
        # already-unit-norm inputs, so this is a no-op against
        # Qwen3-Embedding-8B.
        return [l2_normalize(vec) for vec in vectors]

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
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .chunker import l2_normalize

log = logging.getLogger("indexer.embedder")


def _float_env(name: str, default: float, minimum: float = 1.0) -> float:
    """Read a float env var with a graceful fallback.

    Mirrors ``main._int_env`` and ``reconciler._int`` / ``_pct``: an
    empty / unset / malformed value logs a warning and falls back
    rather than raising at startup. A typo in a tunable knob should
    not crash the indexer.

    ``minimum`` defines the lower bound (default 1.0) — values below
    it are treated as malformed and fall back. ``EMBED_WARMUP_TIMEOUT_SECS``
    and similar per-call deadlines must be positive: ``0`` or negative
    values would reach the OpenAI SDK timeout path and either fail oddly
    or make startup behavior brittle. Mirrors the
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
    # reach the SDK as a per-call deadline and break in surprising
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


def scrub_embed_error(exc: BaseException) -> str:
    """Render an embedder exception into a log/DB-safe string.

    The OpenAI SDK's ``APIStatusError`` carries the provider's response
    body, which can echo input fragments back on 4xx — for the indexer
    that means email body text from the failing batch can flow into
    ``indexing_jobs.last_error`` and any operator log sink. The repo
    is public and the ``last_error`` row + truncated log line both
    travel further than callers usually expect, so trim
    ``APIStatusError`` down to type + status_code only.

    Connection / timeout / our own ``RuntimeError`` (index-integrity
    check) carry no email content, so their full ``repr`` is safe to
    keep.
    """
    if isinstance(exc, APIStatusError):
        return f"{type(exc).__name__}: status={exc.status_code}"
    return repr(exc)


def _is_transient_embed_error(exc: BaseException) -> bool:
    """Decide whether tenacity should retry ``exc``.

    Retry transport-level failures and 5xx that a fresh attempt could
    plausibly fix:

    * ``openai.APIConnectionError`` — TCP / DNS / TLS failures the SDK
      surfaces uniformly.
    * ``openai.APITimeoutError`` — read / write / pool timeouts.
    * 5xx ``openai.APIStatusError`` — provider failed to serve a
      well-formed request and might recover.

    Do NOT retry — deterministic config errors that retrying only
    delays:

    * 4xx ``openai.APIStatusError`` (auth, model id, quota, request
      shape).
    * Our own ``RuntimeError`` from index-integrity checks — the
      provider returned a malformed batch and a retry would produce
      the same shape.
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return False


class EmbeddingBackend(Protocol):
    """Structural contract the embedder satisfies.

    Defined as a ``Protocol`` (not an inheritance base) so test fakes
    can stay duck-typed without depending on the OpenAI SDK or any
    concrete backend.
    """

    def wait_for_ready(self, timeout: int = 120) -> None: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """OpenAI-SDK-backed ``/v1/embeddings`` client.

    For an unauthenticated host-side server, ``base_url`` is something
    like ``http://host.docker.internal:8001/v1`` and ``api_key`` is
    empty (a placeholder is passed to satisfy SDK construction; compat
    servers ignore the auth header). For DeepInfra, ``base_url`` is
    ``https://api.deepinfra.com/v1/openai`` and ``api_key`` comes from
    the ``embed_api_key`` Docker secret. The class itself is
    provider-agnostic.

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
    # providers usually respond in <1 s. The ceiling sits comfortably
    # above the cold-start case; operators on slow links can raise it
    # via ``EMBED_WARMUP_TIMEOUT_SECS``.
    DEFAULT_WARMUP_TIMEOUT_SECS = 600.0

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        batch_size: int = 64,
        request_timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = batch_size
        # The SDK requires a non-empty ``api_key`` to construct cleanly.
        # Unauthenticated host-side servers ignore the resulting
        # Authorization header; the ``"placeholder"`` literal is
        # deliberately self-descriptive so it cannot be mistaken for a
        # real credential if it ever surfaces in a provider's request
        # log (which would happen only if ``EMBED_BASE_URL`` were
        # misconfigured to a remote provider while ``EMBED_API_KEY``
        # was empty). ``max_retries=0`` because retry policy is owned
        # by the tenacity wrapper below — the SDK's built-in retry
        # would double-up exponential backoff and obscure the
        # 4xx-fast-fail / 5xx-retry classification.
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=api_key or "placeholder",
            timeout=request_timeout,
            max_retries=0,
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
        - ``EMBED_WARMUP_TIMEOUT_SECS`` (default 600 s) bounds **one
          successful response** — once TCP connects the request can
          take this long before the SDK times out, absorbing a
          multi-minute first-time HF model download.

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
        connect_deadline = time.time() + float(timeout)
        last_err: Exception | None = None
        while time.time() < connect_deadline:
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
                    # config rather than waiting out the timeout.
                    raise
                last_err = e
            time.sleep(3)
        raise RuntimeError(
            f"embedder at {self.base_url} did not become ready within {timeout}s "
            f"(last error: {last_err!r})"
        )

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for a single input."""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embedding vectors for a list of inputs.

        Splits ``texts`` into ``batch_size`` chunks and issues one
        OpenAI-compatible request per chunk. Per-chunk requests retry
        independently on 5xx / connection errors. Response ``data`` is
        sorted by ``index`` defensively so a future provider that
        reorders the array does not silently misalign vectors with
        their source texts.
        """
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]
            out.extend(self._embed_one_batch(chunk))
        return out

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
        if len(data) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(data)} vectors for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        seen_indices = sorted(d.index for d in data)
        if seen_indices != list(range(len(texts))):
            raise RuntimeError(
                f"embedder returned non-contiguous or duplicate indices "
                f"{seen_indices} for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        data.sort(key=lambda d: d.index)
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
        return [l2_normalize(list(d.embedding)) for d in data]

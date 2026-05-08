"""Embedder — generates vector embeddings via an OpenAI-compatible
``/v1/embeddings`` endpoint.

A single client class talks to any provider that speaks the OpenAI
embeddings wire format: the local mlx-service running on Apple Metal,
DeepInfra, OpenRouter, LM Studio, vLLM, TEI, etc. The choice of
backend is purely a base-URL + model + API key configuration question
— there is no per-provider code path.

``OpenAIEmbedder`` implements the ``EmbeddingBackend`` Protocol so
callers in ``main.py``, ``reconciler.py``, and ``attachment_indexing.py``
stay backend-agnostic and tests can substitute a duck-typed fake.
"""

import logging
import math
import os
import time
from typing import Protocol

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = logging.getLogger("indexer.embedder")


# Tolerance for "vector is already unit-norm". A model that already
# emits L2-normalized output (Qwen3-Embedding-8B does, per its model
# card) lands within float32's relative precision of 1.0; the cheap
# sqrt + compare lets us skip the division entirely when no
# correction is needed. Float32 round-trip noise is well under 1e-6.
_UNIT_NORM_TOLERANCE = 1e-6


def _l2_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm.

    Idempotent: a vector that's already within
    ``_UNIT_NORM_TOLERANCE`` of unit-norm is returned unchanged
    (no division, no float churn). Zero vectors are also returned
    unchanged — dividing by zero would NaN-poison the storage. The
    indexer's seed-vector logic intentionally writes a zero
    placeholder for genuinely-new threads (Phase 1 seed before
    Phase 2 lands the real chunk-mean vector); preserving it through
    this normalization keeps the three-case priority chain working.

    Cost is one O(dim) sum-of-squares plus a sqrt — negligible
    against the embed HTTP round-trip and inputs are already in
    Python list form post-deserialization.
    """
    norm_sq = 0.0
    for x in vec:
        norm_sq += x * x
    if norm_sq <= 0.0:
        return vec
    norm = math.sqrt(norm_sq)
    if abs(norm - 1.0) < _UNIT_NORM_TOLERANCE:
        return vec
    return [x / norm for x in vec]


def _is_transient_embed_error(exc: BaseException) -> bool:
    """Decide whether tenacity should retry ``exc``.

    Retry transport-level failures that a fresh attempt could plausibly
    fix:

    * Most subclasses of ``httpx.TransportError`` — ``TimeoutException``
      (``ConnectTimeout`` / ``ReadTimeout`` / ``WriteTimeout`` /
      ``PoolTimeout``), ``NetworkError`` (``ConnectError`` / ``ReadError``
      / ``WriteError`` / ``CloseError``), ``RemoteProtocolError``,
      and ``ProxyError``. The earlier explicit allowlist
      (``ConnectError``, ``ReadTimeout``, ``RemoteProtocolError``)
      missed ``ConnectTimeout`` / ``WriteTimeout`` / ``PoolTimeout``
      and made the call fail immediately on common transient
      provider hiccups.
    * 5xx ``HTTPStatusError`` — the provider failed to serve a
      well-formed request and might recover.

    Do NOT retry — deterministic config errors that retrying only
    delays:

    * ``httpx.UnsupportedProtocol`` — raised when ``base_url`` lacks
      a scheme (e.g. ``host.docker.internal:8001/v1`` instead of
      ``http://host.docker.internal:8001/v1``). A typo, not a
      transient outage. Retrying buys nothing but startup-timeout
      latency before the operator sees the actionable error.
    * ``httpx.LocalProtocolError`` — raised when the client itself
      builds a malformed request (HTTP/2 framing bug, illegal header
      value). Almost always a code or config issue, not a network
      issue.
    * 4xx ``HTTPStatusError`` (auth, model id, quota, request shape).
    * Our own ``RuntimeError`` from index-integrity checks — the
      provider returned a malformed batch and a retry would produce
      the same shape.
    """
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class EmbeddingBackend(Protocol):
    """Structural contract the embedder satisfies.

    Defined as a ``Protocol`` (not an inheritance base) so test fakes
    can stay duck-typed without depending on httpx or any concrete
    backend.
    """

    def wait_for_ready(self, timeout: int = 120) -> None: ...
    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """OpenAI-compatible ``/v1/embeddings`` HTTP client.

    Wire format:

        POST {base_url}/embeddings
        Authorization: Bearer {api_key}    # omitted if api_key is empty
        Content-Type: application/json

        {"model": "...", "input": "text" | ["t1", "t2", ...]}

        → {"object": "list",
           "data": [{"object": "embedding", "embedding": [...], "index": 0}, ...],
           "model": "...",
           "usage": {...}}

    For the local mlx-service, ``base_url`` is ``http://host.docker.internal:8001/v1``
    and ``api_key`` is empty. For DeepInfra, ``base_url`` is
    ``https://api.deepinfra.com/v1/openai`` and ``api_key`` comes from the
    ``embed_api_key`` Docker secret. The class itself is provider-agnostic.
    """

    # First-call model warmup may include a HuggingFace download for
    # the local mlx-service backend. Empirical cold-start times on a
    # fresh ``~/.cache/huggingface``: Qwen3-Embedding-8B mxfp8 ≈ 4 min.
    # Cached subsequent loads run in <30 s. Cloud providers respond
    # in <1 s. The ceiling sits comfortably above the cold-start
    # observation; operators on slow links can raise it via
    # ``EMBED_WARMUP_TIMEOUT_SECS``.
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
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(timeout=request_timeout, headers=headers)

    def wait_for_ready(self, timeout: int = 120) -> None:
        """Block until the embedder accepts a real ``/v1/embeddings``
        request — covers the local-uvicorn-startup window plus first-call
        HuggingFace download for mlx-service, and a single fast probe
        for cloud providers.

        Two independent deadlines:

        - ``timeout`` (default 120 s) bounds the **connect-phase** —
          how long we keep retrying transient transport failures
          before declaring the service unreachable. A service that
          isn't bound on the port should fail in ~``timeout`` seconds,
          not 10 minutes.
        - ``EMBED_WARMUP_TIMEOUT_SECS`` (default 600 s) bounds **one
          successful response** — once TCP connects the request can
          take this long before httpx times out, absorbing a
          multi-minute first-time HF model download.

        Total wall-clock can exceed ``timeout`` only when a connection
        succeeded but the response is in flight. A 5xx after a long
        warmup wait still surfaces as failure because the connect
        deadline has by then passed.

        Retry classification delegates to ``_is_transient_embed_error``
        — the same predicate ``_embed_one_batch`` uses — so startup and
        runtime share one definition of "transient". 4xx auth / model /
        quota errors fail fast (deterministic config), as does any
        non-httpx exception. 5xx and most of the
        ``httpx.TransportError`` family (connect / read / write / pool
        timeouts, network errors, ``RemoteProtocolError``) retry until
        the connect deadline; deterministic config errors
        (``UnsupportedProtocol``, ``LocalProtocolError``) bypass retry
        because no fresh attempt would change the outcome.
        """
        warmup_timeout = float(
            os.environ.get(
                "EMBED_WARMUP_TIMEOUT_SECS",
                self.DEFAULT_WARMUP_TIMEOUT_SECS,
            )
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
                r = self.client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": "warmup"},
                    timeout=warmup_timeout,
                )
                r.raise_for_status()
                log.info("Embedder ready: %s (%s)", self.base_url, self.model)
                return
            except httpx.HTTPError as e:
                if not _is_transient_embed_error(e):
                    # 4xx config errors and any other non-transient
                    # httpx error surface immediately so the operator
                    # fixes config rather than waiting out the timeout.
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
        r = self.client.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
        )
        r.raise_for_status()
        body = r.json()
        data = list(body["data"])
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
        seen_indices = sorted(d["index"] for d in data)
        if seen_indices != list(range(len(texts))):
            raise RuntimeError(
                f"embedder returned non-contiguous or duplicate indices "
                f"{seen_indices} for {len(texts)} inputs "
                f"({self.base_url}, model={self.model!r})"
            )
        data.sort(key=lambda d: d["index"])
        # Enforce the storage invariant: every vector stored in
        # ``threads_vec`` / ``message_chunks_vec`` is L2-unit-norm.
        # Doing this at the embedder client (not per-caller) keeps the
        # invariant in one place and survives a future provider swap
        # whose output may not be normalized by default. ``_l2_normalize``
        # short-circuits when the input is already unit-norm, so this is
        # a no-op against Qwen3-Embedding-8B (the default mlx-service
        # model) and a corrective step against any provider that emits
        # raw embeddings.
        return [_l2_normalize(d["embedding"]) for d in data]

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
import os
import time
from typing import Protocol

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("indexer.embedder")


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

        ``timeout`` is the connect-phase budget; the per-call request
        timeout is ``EMBED_WARMUP_TIMEOUT_SECS`` (default 600 s) to
        absorb a multi-minute first-time model download. The total
        wall-clock can therefore exceed ``timeout`` once a connection
        succeeds — by design.

        4xx auth/model/quota errors fail fast (won't recover); 5xx and
        connection errors retry until the deadline.
        """
        warmup_timeout = float(
            os.environ.get(
                "EMBED_WARMUP_TIMEOUT_SECS",
                self.DEFAULT_WARMUP_TIMEOUT_SECS,
            )
        )
        log.info(
            "Waiting for embedder at %s (model=%s, warmup_timeout=%.0fs)...",
            self.base_url,
            self.model,
            warmup_timeout,
        )
        deadline = time.time() + max(float(timeout), warmup_timeout)
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                r = self.client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": "warmup"},
                    timeout=warmup_timeout,
                )
                if 400 <= r.status_code < 500:
                    # Auth, model id, request-shape errors won't recover
                    # by retrying. Surface immediately so the operator
                    # fixes config rather than waiting out the timeout.
                    r.raise_for_status()
                r.raise_for_status()
                log.info("Embedder ready: %s (%s)", self.base_url, self.model)
                return
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if 400 <= code < 500:
                    raise
                last_err = e
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
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
        return [d["embedding"] for d in data]

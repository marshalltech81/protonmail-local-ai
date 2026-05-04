"""
Embedder — generates vector embeddings via the host-side MLX service.

``MlxEmbedder`` implements the ``EmbeddingBackend`` Protocol so callers
in ``main.py``, ``reconciler.py``, and ``attachment_indexing.py`` stay
backend-agnostic and tests can substitute a duck-typed fake. Retries
on failure to handle service startup latency.
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


class MlxEmbedder:
    """Talks to the host-side ``mlx-service`` over HTTP.

    The service binds ``127.0.0.1:8001`` on the host and is reached from
    Docker containers via ``host.docker.internal``. Response shape
    matches Ollama's ``/api/embeddings`` for a single string input
    (``{"embedding": [...]}``), so the only difference at this layer is
    the URL and the absence of an Ollama-style model-pull step.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=120.0)

    # First-call model warmup may include a HuggingFace download. Empirical
    # cold-start times on a fresh ``~/.cache/huggingface``: Qwen3-Embedding-8B
    # mxfp8 ≈ 4 min, Qwen3-Reranker-4B mxfp8 ≈ 2 min. Cached subsequent
    # loads run in <30 s. The default warmup ceiling sits comfortably
    # above the cold-start observation; operators on slow links can
    # raise it via ``EMBED_WARMUP_TIMEOUT_SECS``.
    DEFAULT_WARMUP_TIMEOUT_SECS = 600.0

    def wait_for_ready(self, timeout: int = 120) -> None:
        """Block until ``mlx-service`` answers ``/health`` with 200,
        then trigger the lazy model load so the first hot-path embed
        doesn't pay the model-load cost.

        ``timeout`` covers only the health-poll loop (the service
        process is up). The warmup POST has its own, much larger
        deadline because the first call may include a HuggingFace
        download.
        """
        log.info(f"Waiting for mlx-service at {self.base_url}...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.client.get(f"{self.base_url}/health", timeout=5.0)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(3)
        else:
            raise RuntimeError(
                f"mlx-service did not become ready within {timeout}s at {self.base_url}"
            )
        warmup_timeout = float(
            os.environ.get(
                "EMBED_WARMUP_TIMEOUT_SECS",
                self.DEFAULT_WARMUP_TIMEOUT_SECS,
            )
        )
        log.info(
            "mlx-service health OK; warming embedder model (timeout %.0fs, "
            "covers first-time HF download)...",
            warmup_timeout,
        )
        warm = self.client.post(
            f"{self.base_url}/embed", json={"input": "warmup"}, timeout=warmup_timeout
        )
        warm.raise_for_status()
        log.info("mlx-service ready.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        r = self.client.post(
            f"{self.base_url}/embed",
            json={"input": text},
            timeout=60.0,
        )
        r.raise_for_status()
        return r.json()["embedding"]

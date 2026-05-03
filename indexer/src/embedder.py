"""
Embedder — generates vector embeddings via either the local Ollama API
(legacy ``Embedder``) or the host-side MLX service (``MlxEmbedder``).

Both backends implement the same ``EmbeddingBackend`` protocol so the
indexer hot path is backend-agnostic. The factory in ``main.py`` picks
one based on the ``USE_MLX_EMBEDDER`` env flag. Retries on failure to
handle service startup latency.
"""

import logging
import os
import time
from typing import Protocol

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("indexer.embedder")


class EmbeddingBackend(Protocol):
    """Common contract every embedder backend implements.

    Defined as a structural ``Protocol`` rather than an inheritance
    base so the existing ``Embedder`` (Ollama) does not need to grow a
    superclass dependency, and tests can pass plain duck-typed fakes.
    """

    def wait_for_ready(self, timeout: int = 120) -> None: ...
    def embed(self, text: str) -> list[float]: ...


def _model_matches(configured: str, available: str) -> bool:
    """Return True when ``available`` refers to the same model as ``configured``.

    Ollama lists models with an explicit ``:tag`` suffix (``:latest`` by
    default). A bare configured name like ``nomic-embed-text`` should match
    the exact string and also ``nomic-embed-text:latest``. Substring
    matching is avoided because it produced false positives for models
    that share a prefix (e.g. ``llama3`` matching ``llama3.2``).
    """
    if available == configured:
        return True
    if ":" in configured:
        return False
    return available == f"{configured}:latest"


class Embedder:
    def __init__(self, ollama_host: str, model: str):
        self.host = ollama_host.rstrip("/")
        self.model = model
        self.client = httpx.Client(timeout=60.0)

    def wait_for_ready(self, timeout: int = 120):
        """Block until Ollama is available and the model is pulled.

        The readiness loop tolerates connection errors to `/api/tags`
        because Ollama may still be starting. Once the server answers,
        the pull is a definitive step — any error raised by `_pull_model`
        propagates to the caller rather than being swallowed, so a
        missing or misnamed model surfaces as a real failure instead of
        a misleading readiness timeout.
        """
        log.info(f"Waiting for Ollama at {self.host}...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.client.get(f"{self.host}/api/tags")
            except httpx.HTTPError:
                time.sleep(3)
                continue
            if r.status_code != 200:
                time.sleep(3)
                continue
            models = [m["name"] for m in r.json().get("models", [])]
            if any(_model_matches(self.model, m) for m in models):
                log.info(f"Ollama ready. Model '{self.model}' available.")
                return
            log.info(f"Ollama ready but model '{self.model}' not found. Pulling now...")
            self._pull_model()
            return
        raise RuntimeError(f"Ollama did not become ready within {timeout}s. Run: make pull-models")

    def _pull_model(self):
        log.info(f"Pulling model: {self.model}")
        with self.client.stream(
            "POST",
            f"{self.host}/api/pull",
            json={"name": self.model},
            timeout=600.0,
        ) as r:
            # Raise on non-2xx before iterating the body so a 4xx/5xx pull
            # (e.g. unknown model name) is surfaced as a RuntimeError rather
            # than silently logged as a successful "ready" state.
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    log.debug(f"pull: {line}")
        log.info(f"Model '{self.model}' ready.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        r = self.client.post(
            f"{self.host}/api/embeddings",
            json={"model": self.model, "prompt": text},
        )
        r.raise_for_status()
        return r.json()["embedding"]


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
    # raise it via ``MLX_EMBED_WARMUP_TIMEOUT_SECS``.
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
                "MLX_EMBED_WARMUP_TIMEOUT_SECS",
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

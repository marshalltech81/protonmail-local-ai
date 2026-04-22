"""
Embedder — generates vector embeddings via the local Ollama API.
Retries on failure to handle Ollama startup latency.
"""

import logging
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("indexer.embedder")


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

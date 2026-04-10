"""
Embedder — generates vector embeddings via the local Ollama API.
Retries on failure to handle Ollama startup latency.
"""
import logging
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("indexer.embedder")


class Embedder:
    def __init__(self, ollama_host: str, model: str):
        self.host = ollama_host.rstrip("/")
        self.model = model
        self.client = httpx.Client(timeout=60.0)

    def wait_for_ready(self, timeout: int = 120):
        """Block until Ollama is available and the model is pulled."""
        log.info(f"Waiting for Ollama at {self.host}...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.client.get(f"{self.host}/api/tags")
                if r.status_code == 200:
                    models = [m["name"] for m in r.json().get("models", [])]
                    if any(self.model in m for m in models):
                        log.info(f"Ollama ready. Model '{self.model}' available.")
                        return
                    else:
                        log.info(
                            f"Ollama ready but model '{self.model}' not found. "
                            f"Pulling now..."
                        )
                        self._pull_model()
                        return
            except Exception:
                pass
            time.sleep(3)
        raise RuntimeError(
            f"Ollama did not become ready within {timeout}s. "
            f"Run: make pull-models"
        )

    def _pull_model(self):
        log.info(f"Pulling model: {self.model}")
        with self.client.stream(
            "POST",
            f"{self.host}/api/pull",
            json={"name": self.model},
            timeout=600.0,
        ) as r:
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

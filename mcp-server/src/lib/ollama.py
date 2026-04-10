"""
Ollama client for the MCP server.
Used for query embedding (search) and local LLM inference (Q&A, agentic).
"""

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("mcp.ollama")


class OllamaClient:
    def __init__(self, host: str, embed_model: str, llm_model: str):
        self.host = host.rstrip("/")
        self.embed_model = embed_model
        self.llm_model = llm_model
        self.client = httpx.AsyncClient(timeout=120.0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search."""
        r = await self.client.post(
            f"{self.host}/api/embeddings",
            json={"model": self.embed_model, "prompt": text},
        )
        r.raise_for_status()
        return r.json()["embedding"]

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10))
    async def complete(self, system: str, user: str) -> str:
        """Run a completion using the local LLM."""
        r = await self.client.post(
            f"{self.host}/api/chat",
            json={
                "model": self.llm_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

"""
Local-LLM and embedding client for the MCP server.

Two HTTP backends live here, both speaking OpenAI-compatible wire
formats so the same client serves any compliant provider (mlx-service
on Apple Metal, mlx_lm.server, DeepInfra, OpenRouter, LM Studio, vLLM,
TEI, etc.) without per-backend branching:

- **Embed**: query embedding for hybrid search. Posts to
  ``{embed_base_url}/embeddings`` with ``{"model", "input"}`` and reads
  the response under ``data[0].embedding``. Indexer and mcp-server
  must point at the same provider + model so vectors are comparable.
- **Complete**: local LLM inference (Q&A, agentic). Speaks the
  OpenAI-compatible ``/v1/chat/completions`` shape against
  ``llm_base_url``. The local-engine choice is operational config
  (``LLM_BASE_URL`` + ``LLM_MODEL`` env vars), not a code path.
"""

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("mcp.local_llm")

# Per-call HTTP deadlines. Embed and chat have very different latency
# profiles, so they get explicit timeouts (per the AGENTS.md rule that
# outbound async HTTP calls must not rely solely on a client-level
# default).
#
# - Embed: a single short string through Qwen3-Embedding-8B. Steady-
#   state runtime is sub-second; cold-start (first call after
#   mlx-service load) can take a few seconds. 60 s is generous
#   headroom while still bounding a stuck call.
# - Complete: Qwen3 in thinking mode produces reasoning tokens before
#   the final answer. ``mlx_lm.server`` defaults the per-request
#   ``max_tokens`` to 4096 in this project's LaunchAgent config, and
#   on the 32B mxfp8 model that can run ~1-2 minutes for a long
#   answer. 300 s is the steady-state ceiling that catches truly
#   stuck calls without false-positiving on slow-but-progressing
#   inference.
_EMBED_TIMEOUT_SECS = 60.0
_COMPLETE_TIMEOUT_SECS = 300.0


class LocalLLMClient:
    def __init__(
        self,
        embed_base_url: str,
        llm_model: str,
        *,
        llm_base_url: str,
        embed_model: str,
        embed_api_key: str = "",
    ):
        self.embed_base_url = embed_base_url.rstrip("/")
        self.embed_model = embed_model
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url.rstrip("/")
        # Two separate AsyncClients — one per upstream service — so the
        # embedder API key cannot leak to LLM_BASE_URL. A single shared
        # client with a default Authorization header would forward the
        # embed key to the chat-completions provider whenever
        # ``embed_base_url`` and ``llm_base_url`` point at different
        # services (e.g. cloud embedder + local LLM). Splitting the
        # clients also keeps the per-service connection pools and
        # client-level timeout fallbacks independent.
        embed_headers = {"Authorization": f"Bearer {embed_api_key}"} if embed_api_key else {}
        self.embed_client = httpx.AsyncClient(timeout=120.0, headers=embed_headers)
        self.llm_client = httpx.AsyncClient(timeout=120.0)
        # Backwards-compatible alias used by tests that swap a mock
        # transport into the embed client. The chat path uses
        # ``llm_client`` directly.
        self.client = self.embed_client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search via the OpenAI-compatible
        ``/v1/embeddings`` endpoint. Same wire format as the indexer so
        vectors are comparable when both point at the same provider."""
        r = await self.embed_client.post(
            f"{self.embed_base_url}/embeddings",
            json={"model": self.embed_model, "input": text},
            timeout=_EMBED_TIMEOUT_SECS,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10))
    async def complete(self, system: str, user: str) -> str:
        """Run a completion using the local LLM via the OpenAI-compatible
        ``/v1/chat/completions`` endpoint."""
        r = await self.llm_client.post(
            f"{self.llm_base_url}/chat/completions",
            json={
                "model": self.llm_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=_COMPLETE_TIMEOUT_SECS,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

"""
Local-LLM and embedding client for the MCP server.

Two HTTP backends live here:

- **Embed**: query embedding for hybrid search. Posts to
  ``mlx-service`` ``/embed``; the response shape carries the
  vector under ``embedding``.
- **Complete**: local LLM inference (Q&A, agentic). Speaks the
  OpenAI-compatible ``/v1/chat/completions`` shape against
  ``llm_base_url``, so the same client serves Ollama (which exposes
  the OpenAI compat API alongside ``/api/chat``) and ``mlx_lm.server``
  without per-backend branching. The local-engine choice is
  operational config (``LLM_BASE_URL`` + ``LLM_MODEL`` env vars), not
  a code path.
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
        embed_service_url: str,
        llm_model: str,
        *,
        llm_base_url: str,
    ):
        self.embed_service_url = embed_service_url.rstrip("/")
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url.rstrip("/")
        # Client-level timeout is a fallback only; ``embed`` and
        # ``complete`` set per-call deadlines above.
        self.client = httpx.AsyncClient(timeout=120.0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def embed(self, text: str) -> list[float]:
        """Embed a query string for vector search via the
        ``mlx-service`` ``/embed`` endpoint."""
        r = await self.client.post(
            f"{self.embed_service_url}/embed",
            json={"input": text},
            timeout=_EMBED_TIMEOUT_SECS,
        )
        r.raise_for_status()
        return r.json()["embedding"]

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10))
    async def complete(self, system: str, user: str) -> str:
        """Run a completion using the local LLM via the OpenAI-compatible
        ``/v1/chat/completions`` endpoint."""
        r = await self.client.post(
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

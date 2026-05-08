"""Tests for src.lib.local_llm — async OpenAI-compatible embed + chat client.

Uses httpx.MockTransport against the AsyncClient so tests are self-contained
and never touch a real mlx-service / mlx_lm.server / cloud provider or the
network. Async calls are driven with asyncio.run() to keep the dep
footprint minimal (no pytest-asyncio).
"""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from src.lib.local_llm import LocalLLMClient

LLM_BASE = "http://llm:8002/v1"
EMBED_BASE = "http://mlx:8001/v1"
EMBED_MODEL = "mlx-community/Qwen3-Embedding-8B-mxfp8"


def _make(**overrides) -> LocalLLMClient:
    """Construct a ``LocalLLMClient`` with sensible test defaults."""
    return LocalLLMClient(
        embed_base_url=overrides.pop("embed_base_url", EMBED_BASE),
        llm_model=overrides.pop("llm_model", "llm"),
        llm_base_url=overrides.pop("llm_base_url", LLM_BASE),
        embed_model=overrides.pop("embed_model", EMBED_MODEL),
        embed_api_key=overrides.pop("embed_api_key", ""),
        **overrides,
    )


def _install_mock(client: LocalLLMClient, handler, *, llm_handler=None) -> None:
    """Replace the client's AsyncClients with mock-transport-backed versions.

    Preserves construction-time headers on each client (notably
    Authorization on the embed client) so tests that exercise auth
    behavior reflect the real wire format. ``handler`` always backs the
    embed client; ``llm_handler`` (when given) backs the LLM/chat
    client — useful for tests that need to inspect requests against the
    chat endpoint independently of the embed endpoint. When
    ``llm_handler`` is omitted the same handler routes both clients,
    which is what most tests want.
    """
    embed_headers = dict(client.embed_client.headers)
    llm_headers = dict(client.llm_client.headers)
    asyncio.run(client.embed_client.aclose())
    asyncio.run(client.llm_client.aclose())
    client.embed_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=120.0,
        headers=embed_headers,
    )
    client.llm_client = httpx.AsyncClient(
        transport=httpx.MockTransport(llm_handler or handler),
        timeout=120.0,
        headers=llm_headers,
    )
    client.client = client.embed_client


def _openai_embed_response(vectors: list[list[float]]) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vectors)
        ],
        "model": EMBED_MODEL,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def _run(coro):
    return asyncio.run(coro)


def _close(c: LocalLLMClient) -> None:
    """Close both AsyncClients owned by a ``LocalLLMClient``.

    The class now keeps a separate embed client and LLM/chat client so
    the embed API key cannot leak across services; tests must close
    both to avoid lingering open connections in the asyncio loop.
    """
    asyncio.run(c.embed_client.aclose())
    asyncio.run(c.llm_client.aclose())


class TestLocalLLMClientInit:
    def test_strips_trailing_slash_from_embed_base_url(self):
        c = _make(embed_base_url="http://mlx:8001/v1/")
        assert c.embed_base_url == "http://mlx:8001/v1"
        _close(c)

    def test_strips_trailing_slash_from_llm_base_url(self):
        c = _make(llm_base_url="http://llm:8002/v1/")
        assert c.llm_base_url == "http://llm:8002/v1"
        _close(c)

    def test_stores_llm_model(self):
        c = _make(llm_model="llm-y")
        assert c.llm_model == "llm-y"
        _close(c)

    def test_stores_embed_model(self):
        c = _make(embed_model="cohere/embed-v4")
        assert c.embed_model == "cohere/embed-v4"
        _close(c)


class TestEmbed:
    def test_posts_openai_shape_and_returns_embedding(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            # OpenAI-compatible: same wire format as DeepInfra, OpenRouter,
            # mlx-service /v1/embeddings, etc.
            assert str(request.url) == f"{EMBED_BASE}/embeddings"
            import json

            body = json.loads(request.content)
            assert body == {"model": EMBED_MODEL, "input": "q"}
            return httpx.Response(200, json=_openai_embed_response([[0.5, 0.6]]))

        _install_mock(c, handler)
        try:
            assert _run(c.embed("q")) == [0.5, 0.6]
        finally:
            _close(c)

    def test_authorization_header_set_when_api_key_provided(self):
        c = _make(embed_api_key="sk-test")  # pragma: allowlist secret
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json=_openai_embed_response([[0.0]]))

        _install_mock(c, handler)
        try:
            _run(c.embed("q"))
        finally:
            _close(c)
        assert seen["auth"] == "Bearer sk-test"  # pragma: allowlist secret

    def test_authorization_header_omitted_when_api_key_empty(self):
        # Local mlx-service does not authenticate; sending a bare
        # ``Authorization: Bearer`` (no token) would be malformed.
        c = _make()
        seen: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=_openai_embed_response([[0.0]]))

        _install_mock(c, handler)
        try:
            _run(c.embed("q"))
        finally:
            _close(c)
        assert seen["auth"] is None

    def test_retries_on_server_error_then_succeeds(self):
        c = _make()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "warming up"})
            return httpx.Response(200, json=_openai_embed_response([[1.0]]))

        _install_mock(c, handler)
        try:
            with patch("src.lib.local_llm.wait_exponential", lambda **_: lambda *_: 0):
                assert _run(c.embed("retry")) == [1.0]
        finally:
            _close(c)
        assert calls["n"] == 2

    def test_raises_after_retry_exhaustion(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        _install_mock(c, handler)
        try:
            with pytest.raises(Exception):
                _run(c.embed("dead"))
        finally:
            _close(c)


class TestComplete:
    def test_returns_message_content(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            # OpenAI-compatible endpoint: posts to {llm_base_url}/chat/completions.
            assert str(request.url) == f"{LLM_BASE}/chat/completions"
            body = request.content.decode()
            # system and user prompts should be passed through as separate messages
            assert '"role":"system"' in body.replace(" ", "")
            assert '"role":"user"' in body.replace(" ", "")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "hi there"}}]},
            )

        _install_mock(c, handler)
        try:
            assert _run(c.complete("sys", "user")) == "hi there"
        finally:
            _close(c)

    def test_retries_then_raises(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        _install_mock(c, handler)
        try:
            with pytest.raises(Exception):
                _run(c.complete("sys", "user"))
        finally:
            _close(c)

    def test_complete_does_not_carry_embed_api_key(self):
        # Regression for the cross-service auth-header leak: when
        # ``embed_api_key`` is set (cloud embedder), the chat path must
        # not forward that bearer token to ``LLM_BASE_URL`` — which
        # would expose the embedder key to whatever provider is serving
        # chat. Splitting the embed and LLM clients enforces this; this
        # test pins the contract.
        c = _make(embed_api_key="leaky-embed-key")  # pragma: allowlist secret
        seen_llm_auth: dict[str, str | None] = {}

        def embed_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_openai_embed_response([[0.0]]))

        def llm_handler(request: httpx.Request) -> httpx.Response:
            seen_llm_auth["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )

        _install_mock(c, embed_handler, llm_handler=llm_handler)
        try:
            _run(c.complete("sys", "user"))
        finally:
            _close(c)
        assert seen_llm_auth["auth"] is None, (
            "complete() must not send the embed API key to LLM_BASE_URL"
        )

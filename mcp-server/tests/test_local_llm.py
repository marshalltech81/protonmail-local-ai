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

LLM_BASE = "http://llm:11434/v1"
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


def _install_mock(client: LocalLLMClient, handler) -> None:
    """Replace the client's AsyncClient with one backed by a mock transport.

    Preserves construction-time headers (notably Authorization) so tests
    that exercise auth behavior reflect the real wire format.
    """
    headers = dict(client.client.headers)
    asyncio.run(client.client.aclose())
    client.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=120.0,
        headers=headers,
    )


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


class TestLocalLLMClientInit:
    def test_strips_trailing_slash_from_embed_base_url(self):
        c = _make(embed_base_url="http://mlx:8001/v1/")
        assert c.embed_base_url == "http://mlx:8001/v1"
        _run(c.client.aclose())

    def test_strips_trailing_slash_from_llm_base_url(self):
        c = _make(llm_base_url="http://llm:8002/v1/")
        assert c.llm_base_url == "http://llm:8002/v1"
        _run(c.client.aclose())

    def test_stores_llm_model(self):
        c = _make(llm_model="llm-y")
        assert c.llm_model == "llm-y"
        _run(c.client.aclose())

    def test_stores_embed_model(self):
        c = _make(embed_model="cohere/embed-v4")
        assert c.embed_model == "cohere/embed-v4"
        _run(c.client.aclose())


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
            _run(c.client.aclose())

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
            _run(c.client.aclose())
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
            _run(c.client.aclose())
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
            _run(c.client.aclose())
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
            _run(c.client.aclose())


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
            _run(c.client.aclose())

    def test_retries_then_raises(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        _install_mock(c, handler)
        try:
            with pytest.raises(Exception):
                _run(c.complete("sys", "user"))
        finally:
            _run(c.client.aclose())

"""Tests for src.lib.local_llm — async local-LLM and embed client.

Uses httpx.MockTransport against the AsyncClient so tests are self-contained
and never touch a real mlx-service / mlx_lm.server / Ollama process or the
network. Async calls are driven with asyncio.run() to keep the dep
footprint minimal (no pytest-asyncio).
"""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from src.lib.local_llm import LocalLLMClient

LLM_BASE = "http://llm:11434/v1"
EMBED_BASE = "http://mlx:8001"


def _make(**overrides) -> LocalLLMClient:
    """Construct a ``LocalLLMClient`` with sensible test defaults."""
    return LocalLLMClient(
        overrides.pop("embed_service_url", EMBED_BASE),
        overrides.pop("llm_model", "llm"),
        llm_base_url=overrides.pop("llm_base_url", LLM_BASE),
        **overrides,
    )


def _install_mock(client: LocalLLMClient, handler) -> None:
    """Replace the client's AsyncClient with one backed by a mock transport."""
    # Close the existing real client synchronously by running its aclose.
    asyncio.run(client.client.aclose())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=120.0)


def _run(coro):
    return asyncio.run(coro)


class TestLocalLLMClientInit:
    def test_strips_trailing_slash_from_embed_service_url(self):
        c = _make(embed_service_url="http://mlx:8001/")
        assert c.embed_service_url == "http://mlx:8001"
        _run(c.client.aclose())

    def test_strips_trailing_slash_from_llm_base_url(self):
        c = _make(llm_base_url="http://llm:8002/v1/")
        assert c.llm_base_url == "http://llm:8002/v1"
        _run(c.client.aclose())

    def test_stores_llm_model(self):
        c = _make(llm_model="llm-y")
        assert c.llm_model == "llm-y"
        _run(c.client.aclose())


class TestEmbed:
    def test_returns_embedding_on_success(self):
        c = _make()

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == f"{EMBED_BASE}/embed"
            return httpx.Response(200, json={"embedding": [0.5, 0.6]})

        _install_mock(c, handler)
        try:
            assert _run(c.embed("q")) == [0.5, 0.6]
        finally:
            _run(c.client.aclose())

    def test_retries_on_server_error_then_succeeds(self):
        c = _make()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "warming up"})
            return httpx.Response(200, json={"embedding": [1.0]})

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

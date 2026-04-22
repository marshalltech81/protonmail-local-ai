"""Tests for src.lib.ollama — async Ollama client.

Uses httpx.MockTransport against the AsyncClient so tests are self-contained
and never touch a real Ollama process or the network. Async calls are driven
with asyncio.run() to keep the dep footprint minimal (no pytest-asyncio).
"""

import asyncio
from unittest.mock import patch

import httpx
import pytest
from src.lib.ollama import OllamaClient


def _install_mock(client: OllamaClient, handler) -> None:
    """Replace the client's AsyncClient with one backed by a mock transport."""
    asyncio.get_event_loop().run_until_complete(client.client.aclose()) if False else None
    # Close the existing real client synchronously by running its aclose.
    asyncio.run(client.client.aclose())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=120.0)


def _run(coro):
    return asyncio.run(coro)


class TestOllamaClientInit:
    def test_strips_trailing_slash_from_host(self):
        c = OllamaClient("http://ollama:11434/", "embed", "llm")
        assert c.host == "http://ollama:11434"
        _run(c.client.aclose())

    def test_stores_model_names(self):
        c = OllamaClient("http://ollama:11434", "embed-x", "llm-y")
        assert c.embed_model == "embed-x"
        assert c.llm_model == "llm-y"
        _run(c.client.aclose())


class TestEmbed:
    def test_returns_embedding_on_success(self):
        c = OllamaClient("http://ollama:11434", "embed", "llm")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/embeddings"
            return httpx.Response(200, json={"embedding": [0.5, 0.6]})

        _install_mock(c, handler)
        try:
            assert _run(c.embed("q")) == [0.5, 0.6]
        finally:
            _run(c.client.aclose())

    def test_retries_on_server_error_then_succeeds(self):
        c = OllamaClient("http://ollama:11434", "embed", "llm")
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "warming up"})
            return httpx.Response(200, json={"embedding": [1.0]})

        _install_mock(c, handler)
        try:
            with patch("src.lib.ollama.wait_exponential", lambda **_: lambda *_: 0):
                assert _run(c.embed("retry")) == [1.0]
        finally:
            _run(c.client.aclose())
        assert calls["n"] == 2

    def test_raises_after_retry_exhaustion(self):
        c = OllamaClient("http://ollama:11434", "embed", "llm")

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
        c = OllamaClient("http://ollama:11434", "embed", "llm")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/chat"
            body = request.content.decode()
            # system and user prompts should be passed through as separate messages
            assert '"role":"system"' in body.replace(" ", "")
            assert '"role":"user"' in body.replace(" ", "")
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": "hi there"}}
            )

        _install_mock(c, handler)
        try:
            assert _run(c.complete("sys", "user")) == "hi there"
        finally:
            _run(c.client.aclose())

    def test_retries_then_raises(self):
        c = OllamaClient("http://ollama:11434", "embed", "llm")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        _install_mock(c, handler)
        try:
            with pytest.raises(Exception):
                _run(c.complete("sys", "user"))
        finally:
            _run(c.client.aclose())

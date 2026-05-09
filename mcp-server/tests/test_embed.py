"""Tests for ``src.lib.embed.EmbedClient``.

The client wraps the official ``openai`` SDK with a custom ``base_url``
so it works against any OpenAI-compatible embedder. Tests
monkey-patch ``client.embeddings.create`` to drive deterministic
behavior without hitting a live provider.
"""

import asyncio
from types import SimpleNamespace

import pytest
from src.lib.embed import EmbedClient


def _embedding_response(vector: list[float]) -> SimpleNamespace:
    """Build a ``CreateEmbeddingResponse``-shaped namespace.

    The real SDK returns typed objects with ``.data[i].embedding``
    attributes; ``SimpleNamespace`` mimics that surface closely enough
    for ``EmbedClient`` to unwrap without pulling in the SDK's
    construction machinery.
    """
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=vector, index=0)],
    )


class TestConstructor:
    def test_strips_trailing_slash_from_base_url(self):
        c = EmbedClient(base_url="http://x/v1/", model="m")
        assert c.base_url == "http://x/v1"

    def test_stores_model(self):
        c = EmbedClient(base_url="http://x/v1", model="qwen3-embed")
        assert c.model == "qwen3-embed"

    def test_constructs_against_unauthenticated_server(self):
        # An empty api_key must not raise — the SDK requires a string,
        # so the class supplies a placeholder. Compat servers ignore
        # the resulting Authorization header.
        c = EmbedClient(base_url="http://x/v1", model="m", api_key="")
        assert c is not None


class TestEmbed:
    def test_returns_embedding_vector(self):
        c = EmbedClient(base_url="http://x/v1", model="m")

        async def fake_create(**kwargs):
            assert kwargs["model"] == "m"
            assert kwargs["input"] == "query"
            return _embedding_response([0.5, 0.6, 0.7, 0.8])

        c.client.embeddings.create = fake_create  # type: ignore[assignment]
        out = asyncio.run(c.embed("query"))
        assert out == [0.5, 0.6, 0.7, 0.8]

    def test_propagates_sdk_exceptions(self):
        c = EmbedClient(base_url="http://x/v1", model="m")

        class Boom(Exception):
            pass

        async def fake_create(**_kwargs):
            raise Boom("simulated upstream failure")

        c.client.embeddings.create = fake_create  # type: ignore[assignment]
        with pytest.raises(Boom):
            asyncio.run(c.embed("query"))

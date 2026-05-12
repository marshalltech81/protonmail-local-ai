"""Tests for ``src.lib.embed.EmbedClient``.

The client wraps the official ``openai`` SDK with a custom ``base_url``
so it works against any OpenAI-compatible embedder. Tests
monkey-patch ``client.embeddings.create`` to drive deterministic
behavior without hitting a live provider.
"""

import asyncio
from types import SimpleNamespace

import pytest
from src.lib.embed import EmbedClient, embed_query


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
    # ``api_key`` is required (non-empty) in production — startup
    # validation in ``main.py`` rejects an empty value before the
    # constructor runs. Tests always supply an explicit placeholder so
    # the SDK's "must be a non-empty string" check doesn't masquerade
    # as a constructor bug. Operators pointing at an unauthenticated
    # host-side server supply the same shape of value in
    # ``.secrets/embed_api_key.txt``.
    def test_strips_trailing_slash_from_base_url(self):
        c = EmbedClient(base_url="http://x/v1/", model="m", api_key="placeholder")
        assert c.base_url == "http://x/v1"

    def test_stores_model(self):
        c = EmbedClient(base_url="http://x/v1", model="qwen3-embed", api_key="placeholder")
        assert c.model == "qwen3-embed"

    def test_constructor_passes_api_key_through_unchanged(self):
        # The constructor MUST NOT rewrite ``api_key`` (no silent
        # substitution to a literal like ``"unauthenticated"``).
        # Operators supply the exact bearer string they want sent; a
        # silent rewrite would surface in a misconfigured remote
        # provider's request log instead of the operator's value.
        c = EmbedClient(base_url="http://x/v1", model="m", api_key="placeholder")
        # The SDK's stored credential is what we passed in.
        assert c.client.api_key == "placeholder"  # pragma: allowlist secret

    def test_empty_base_url_falls_back_to_sdk_default(self):
        # ``EMBED_BASE_URL=""`` means "use the SDK default" (OpenAI
        # proper). The required non-empty ``EMBED_API_KEY`` upstream
        # is the explicit-intent signal that makes empty-URL
        # unambiguous: an operator with a real ``sk-...`` has
        # unambiguously chosen their provider, so we trust the
        # documented SDK fallback. Symmetric with the indexer's
        # ``OpenAIEmbedder``, the inference ``_OpenAIBackend``, and
        # the existing ``_AnthropicBackend`` empty-URL path.
        c = EmbedClient(base_url="", model="m", api_key="sk-real")  # pragma: allowlist secret
        # After construction the SDK has resolved its fallback chain
        # (``OPENAI_BASE_URL`` env → ``https://api.openai.com/v1``).
        # The trailing ``/`` anchors the hostname boundary so the
        # check can't be satisfied by ``https://api.openai.com.<x>/``
        # (CodeQL ``py/incomplete-url-substring-sanitization``).
        assert c.base_url.startswith("https://api.openai.com/")


class TestEmbed:
    def test_returns_embedding_vector(self):
        c = EmbedClient(base_url="http://x/v1", model="m", api_key="placeholder")

        async def fake_create(**kwargs):
            assert kwargs["model"] == "m"
            assert kwargs["input"] == "query"
            return _embedding_response([0.5, 0.6, 0.7, 0.8])

        c.client.embeddings.create = fake_create  # type: ignore[assignment]
        out = asyncio.run(c.embed("query"))
        assert out == [0.5, 0.6, 0.7, 0.8]

    def test_propagates_sdk_exceptions(self):
        c = EmbedClient(base_url="http://x/v1", model="m", api_key="placeholder")

        class Boom(Exception):
            pass

        async def fake_create(**_kwargs):
            raise Boom("simulated upstream failure")

        c.client.embeddings.create = fake_create  # type: ignore[assignment]
        with pytest.raises(Boom):
            asyncio.run(c.embed("query"))

    def test_raises_actionable_error_on_empty_data(self):
        # A buggy / not-quite-compatible provider that returns
        # ``data=[]`` would otherwise trip ``IndexError: list index
        # out of range`` from a bare ``resp.data[0]`` access. Mirror
        # the indexer's batch-cardinality check: raise ``RuntimeError``
        # naming the operator-controllable knobs (base URL + model),
        # never the query text — the indexer's shape-validation path
        # explicitly omits payload bytes from its error message and
        # the query string is user input that must not land in logs.
        c = EmbedClient(base_url="http://wrong-provider/v1", model="m", api_key="placeholder")

        async def fake_create(**_kwargs):
            return SimpleNamespace(data=[])

        c.client.embeddings.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(c.embed("secret-query-text"))
        msg = str(excinfo.value)
        assert "http://wrong-provider/v1" in msg
        assert "m" in msg
        assert "secret-query-text" not in msg


class _StubEmbed:
    """Lightweight stand-in matching the surface ``embed_query`` reads.

    Both the real ``EmbedClient`` and ``FakeEmbedClient`` in tests
    expose ``.embed(text) -> list[float]`` plus ``.base_url`` /
    ``.model`` attributes. ``embed_query`` reads the latter two to
    name the misconfigured knobs in its error message.
    """

    def __init__(self, vector: list[float], base_url: str = "http://x/v1", model: str = "m"):
        self._vector = vector
        self.base_url = base_url
        self.model = model

    async def embed(self, _text: str) -> list[float]:
        return list(self._vector)


class TestEmbedQueryDimValidation:
    """``embed_query`` is the boundary check that fixes the silent
    wrong-dim degradation. Before this, a misconfigured ``EMBED_MODEL``
    that returned (say) 3072-dim vectors against a 4096-dim index
    raised inside sqlite-vec and got swallowed by the
    ``except (sqlite3.Error, ValueError)`` in ``_chunk_vector_search``
    — so semantic / hybrid search silently returned no results
    instead of telling the operator the embedder was misconfigured.
    """

    def test_returns_vector_unchanged_when_dim_matches(self):
        stub = _StubEmbed([0.1, 0.2, 0.3, 0.4])
        out = asyncio.run(embed_query(stub, "query", expected_dim=4))
        assert out == [0.1, 0.2, 0.3, 0.4]

    def test_skips_check_when_expected_dim_is_none(self):
        # Fresh-install / pre-indexer state: schema doesn't declare a
        # vec table yet, so ``Database.get_embedding_dim()`` returns
        # None and the helper has nothing to compare against. Pass
        # through rather than fail closed — keyword search still works
        # and semantic / hybrid will surface the missing-table error
        # via the DB layer.
        stub = _StubEmbed([0.1, 0.2, 0.3])
        out = asyncio.run(embed_query(stub, "query", expected_dim=None))
        assert out == [0.1, 0.2, 0.3]

    def test_raises_on_dim_mismatch_with_actionable_message(self):
        # The error message must name the knobs the operator can
        # actually change — ``EMBED_BASE_URL`` and ``EMBED_MODEL`` —
        # plus the observed-vs-expected sizes so the operator can
        # confirm which side is wrong.
        stub = _StubEmbed(
            [0.1, 0.2, 0.3],
            base_url="http://wrong-provider/v1",
            model="other-embed-model",
        )
        with pytest.raises(ValueError) as excinfo:
            asyncio.run(embed_query(stub, "query", expected_dim=4))
        msg = str(excinfo.value)
        assert "3" in msg
        assert "4" in msg
        assert "EMBED_BASE_URL" in msg or "http://wrong-provider/v1" in msg
        assert "EMBED_MODEL" in msg or "other-embed-model" in msg

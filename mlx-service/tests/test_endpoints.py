"""Endpoint contract tests for mlx-service.

These tests run on machines without Metal: the heavy model handles
(_embedder, _reranker) are monkey-patched to fakes that return
deterministic vectors and yes/no logits. The goal is to exercise the
HTTP-shape and routing logic, not the model math (which is verified
manually with the curl validation in step 1).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from src import main


class _FakeArray:
    """Minimal stand-in for an mlx.core.array that exposes the bits the
    main module uses on the embedder path: indexing + tolist()."""

    def __init__(self, data: list[float]):
        self._data = data

    def tolist(self) -> list[float]:
        return list(self._data)

    def __getitem__(self, key: int) -> _FakeArray:  # used as out.text_embeds[0]
        # The real path is text_embeds[0]; for fakes we just return self.
        return self


class _FakeEmbedderOutput:
    def __init__(self, dim: int, value: float = 0.1) -> None:
        self.text_embeds = _FakeArray([value] * dim)


class _FakeEmbedderModel:
    def __call__(self, ids: Any) -> _FakeEmbedderOutput:
        return _FakeEmbedderOutput(dim=4096, value=0.01)


class _FakeEmbedderTokenizer:
    def encode(self, text: str, return_tensors: str | None = None) -> Any:
        return text  # opaque — the fake model ignores it.


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Patch the lazy model handles to return fakes so /embed and
    /rerank serve without actually loading any MLX weights."""
    fake_embed_model = _FakeEmbedderModel()
    fake_embed_tok = _FakeEmbedderTokenizer()
    monkeypatch.setattr(main._embedder, "_model", fake_embed_model, raising=False)
    monkeypatch.setattr(main._embedder, "_tokenizer", fake_embed_tok, raising=False)
    # Reranker isn't needed for the embed/health tests; rerank tests set
    # up their own fakes.
    return TestClient(main.app)


def test_health_initial(client: TestClient) -> None:
    # Embedder is already patched-as-loaded by the fixture; reranker is
    # not loaded yet.
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["models"]["embedder"]["loaded"] is True
    assert body["models"]["embedder"]["id"] == main.EMBED_MODEL_ID
    assert body["models"]["reranker"]["id"] == main.RERANK_MODEL_ID
    assert "process_resident_mb" in body
    assert isinstance(body["process_resident_mb"], (int, float))


def test_embed_single_string_matches_ollama_shape(client: TestClient) -> None:
    r = client.post("/embed", json={"input": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert "embedding" in body and "embeddings" not in body
    assert len(body["embedding"]) == 4096


def test_embed_list_returns_batch_shape(client: TestClient) -> None:
    r = client.post("/embed", json={"input": ["a", "b", "c"]})
    assert r.status_code == 200
    body = r.json()
    assert "embeddings" in body and "embedding" not in body
    assert len(body["embeddings"]) == 3
    assert all(len(v) == 4096 for v in body["embeddings"])


def test_embed_empty_list_400(client: TestClient) -> None:
    r = client.post("/embed", json={"input": []})
    assert r.status_code == 400


def test_rerank_orders_by_score_desc_and_respects_top_n(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replace the per-pair scoring function so we can inject deterministic
    # scores without instantiating the reranker model.
    fake_meta = {"yes_id": 0, "no_id": 1, "prefix_ids": [], "suffix_ids": []}
    monkeypatch.setattr(main, "_rerank_metadata", lambda _tok: fake_meta)

    class _Embed:
        def as_linear(self, _x: Any) -> Any:
            return None

    class _ModelDot:
        embed_tokens = _Embed()

    class _FakeConfig:
        tie_word_embeddings = True

    class _FakeRerankerModel:
        model = _ModelDot()
        config = _FakeConfig()

    monkeypatch.setattr(main._reranker, "_model", _FakeRerankerModel(), raising=False)
    monkeypatch.setattr(main._reranker, "_tokenizer", object(), raising=False)

    # Score = position-based: doc index 0 → highest, last doc → lowest.
    fake_scores = [0.9, 0.7, 0.3, 0.1]

    def fake_score(_m, _t, _e, _meta, _q, doc, _ins) -> float:
        idx = ["d0", "d1", "d2", "d3"].index(doc)
        return fake_scores[idx]

    monkeypatch.setattr(main, "_rerank_score", fake_score)

    r = client.post(
        "/rerank",
        json={"query": "q", "documents": ["d0", "d1", "d2", "d3"], "top_n": 2},
    )
    assert r.status_code == 200
    body = r.json()
    results = body["results"]
    assert len(results) == 2
    assert results[0] == {"index": 0, "score": 0.9}
    assert results[1] == {"index": 1, "score": 0.7}


def test_rerank_empty_documents_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = client.post("/rerank", json={"query": "q", "documents": []})
    assert r.status_code == 400


def test_rerank_rejects_model_without_tied_embeddings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lm_head logit recovery via embed_tokens.as_linear() depends
    # on tie_word_embeddings=True. A model swap that breaks this
    # assumption must fail loudly (500) — silently using the wrong
    # projection produces wrong scores in retrieval.
    class _UntiedConfig:
        tie_word_embeddings = False

    class _UntiedModel:
        config = _UntiedConfig()
        model = type("M", (), {"embed_tokens": object()})()

    monkeypatch.setattr(main._reranker, "_model", _UntiedModel(), raising=False)
    monkeypatch.setattr(main._reranker, "_tokenizer", object(), raising=False)

    r = client.post("/rerank", json={"query": "q", "documents": ["d"]})
    assert r.status_code == 500
    assert "tie_word_embeddings" in r.json()["detail"]

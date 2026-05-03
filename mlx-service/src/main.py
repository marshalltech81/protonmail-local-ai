"""mlx-service — host-side FastAPI app for embeddings + reranking on
Apple Metal via MLX.

Endpoints:
    POST /embed   — Ollama /api/embeddings response shape for a single
                    string; batch shape for a list.
    POST /rerank  — Qwen3-Reranker yes/no logit scoring over (query,
                    documents) pairs.
    GET  /health  — model load state + approximate RSS.

Design notes:
    - Both models load lazily on first request and stay resident; the
      service is a single uvicorn worker so the model handles are
      process-global module state.
    - The reranker uses the documented Qwen3-Reranker prompt template
      and recovers vocab logits via the tied input embedding
      (config.tie_word_embeddings=True), since mlx-embeddings exposes
      only the hidden-state head for this architecture. The yes/no
      log-softmax + exp is the model's trained scoring path, not a
      generic LLM yes/no workaround.
"""

from __future__ import annotations

import logging
import math
import os
import resource
import threading
from typing import Any

import mlx.core as mx
from fastapi import FastAPI, HTTPException
from mlx_embeddings import load
from pydantic import BaseModel, Field

log = logging.getLogger("mlx-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


EMBED_MODEL_ID = os.environ.get("MLX_EMBED_MODEL", "mlx-community/Qwen3-Embedding-8B-mxfp8")
RERANK_MODEL_ID = os.environ.get("MLX_RERANK_MODEL", "mlx-community/Qwen3-Reranker-4B-mxfp8")
RERANK_MAX_LENGTH = int(os.environ.get("MLX_RERANK_MAX_LENGTH", "8192"))

# Documented Qwen3-Reranker chat template. The empty <think>...</think>
# block in the assistant prefix tells the model to skip thinking and
# emit yes/no immediately.
RERANK_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the "
    "Query and the Instruct provided. Note that the answer can only be "
    '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
RERANK_DEFAULT_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)


class _ModelHandle:
    """Lazy, thread-safe holder for one (model, tokenizer) pair."""

    def __init__(self, model_id: str, label: str) -> None:
        self.model_id = model_id
        self.label = label
        self._lock = threading.Lock()
        self._model: Any = None
        self._tokenizer: Any = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def get(self) -> tuple[Any, Any]:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    log.info(
                        "loading %s (%s) — first call, may take ~30-60s", self.label, self.model_id
                    )
                    self._model, self._tokenizer = load(self.model_id)
                    log.info("loaded %s", self.label)
        return self._model, self._tokenizer


_embedder = _ModelHandle(EMBED_MODEL_ID, "embedder")
_reranker = _ModelHandle(RERANK_MODEL_ID, "reranker")
# Reranker prefix/suffix token ids and yes/no token ids are derived from
# the reranker tokenizer on first use and cached.
_rerank_meta: dict[str, Any] = {}


def _rerank_metadata(tokenizer: Any) -> dict[str, Any]:
    if not _rerank_meta:
        _rerank_meta["yes_id"] = tokenizer.convert_tokens_to_ids("yes")
        _rerank_meta["no_id"] = tokenizer.convert_tokens_to_ids("no")
        _rerank_meta["prefix_ids"] = tokenizer.encode(RERANK_PREFIX, add_special_tokens=False)
        _rerank_meta["suffix_ids"] = tokenizer.encode(RERANK_SUFFIX, add_special_tokens=False)
        log.info(
            "reranker meta: yes_id=%s no_id=%s prefix=%dt suffix=%dt",
            _rerank_meta["yes_id"],
            _rerank_meta["no_id"],
            len(_rerank_meta["prefix_ids"]),
            len(_rerank_meta["suffix_ids"]),
        )
    return _rerank_meta


def _embed_one(model: Any, tokenizer: Any, text: str) -> list[float]:
    ids = tokenizer.encode(text, return_tensors="mlx")
    out = model(ids)
    vec = out.text_embeds[0]
    mx.eval(vec)
    return vec.tolist()


def _embed_batch(model: Any, tokenizer: Any, texts: list[str]) -> list[list[float]]:
    # Per-item to keep memory and padding behavior predictable; this can
    # be batched later if throughput becomes a bottleneck.
    return [_embed_one(model, tokenizer, t) for t in texts]


def _rerank_score(
    model: Any,
    tokenizer: Any,
    embed_layer: Any,
    meta: dict[str, Any],
    query: str,
    document: str,
    instruction: str,
) -> float:
    body = f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"
    body_ids = tokenizer.encode(body, add_special_tokens=False)
    budget = RERANK_MAX_LENGTH - len(meta["prefix_ids"]) - len(meta["suffix_ids"])
    if len(body_ids) > budget:
        body_ids = body_ids[:budget]
    full_ids = meta["prefix_ids"] + body_ids + meta["suffix_ids"]
    ids = mx.array([full_ids])
    out = model(ids)
    last_hidden = out.last_hidden_state[:, -1, :]
    logits = embed_layer.as_linear(last_hidden)
    yes_logit = logits[0, meta["yes_id"]].item()
    no_logit = logits[0, meta["no_id"]].item()
    # Numerically stable log-softmax over the 2-token {no, yes} subset,
    # take exp of the yes index → P(yes | {yes, no}).
    m = max(yes_logit, no_logit)
    denom = math.log(math.exp(yes_logit - m) + math.exp(no_logit - m)) + m
    return math.exp(yes_logit - denom)


def _resident_mb() -> float:
    """Process resident memory in MiB. macOS reports ru_maxrss in bytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024)


class EmbedRequest(BaseModel):
    input: str | list[str]


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_n: int | None = Field(default=None, ge=1)
    instruction: str | None = None


class RerankResult(BaseModel):
    index: int
    score: float


app = FastAPI(title="mlx-service", version="0.1.0")


@app.post("/embed")
def embed(req: EmbedRequest) -> dict[str, Any]:
    model, tokenizer = _embedder.get()
    if isinstance(req.input, str):
        return {"embedding": _embed_one(model, tokenizer, req.input)}
    if not req.input:
        raise HTTPException(status_code=400, detail="input list is empty")
    return {"embeddings": _embed_batch(model, tokenizer, req.input)}


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict[str, Any]:
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents list is empty")
    model, tokenizer = _reranker.get()
    meta = _rerank_metadata(tokenizer)
    embed_layer = model.model.embed_tokens
    instruction = req.instruction or RERANK_DEFAULT_INSTRUCTION
    scored = [
        RerankResult(
            index=i,
            score=_rerank_score(model, tokenizer, embed_layer, meta, req.query, doc, instruction),
        )
        for i, doc in enumerate(req.documents)
    ]
    scored.sort(key=lambda r: r.score, reverse=True)
    if req.top_n is not None:
        scored = scored[: req.top_n]
    return {"results": [r.model_dump() for r in scored]}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "models": {
            "embedder": {
                "id": EMBED_MODEL_ID,
                "loaded": _embedder.loaded,
            },
            "reranker": {
                "id": RERANK_MODEL_ID,
                "loaded": _reranker.loaded,
            },
        },
        "process_resident_mb": round(_resident_mb(), 1),
    }

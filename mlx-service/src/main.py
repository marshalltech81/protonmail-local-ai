"""mlx-service — host-side FastAPI app for embeddings + reranking on
Apple Metal via MLX.

Endpoints:
    POST /v1/embeddings  — OpenAI-compatible embeddings. The single
                           supported call shape across local and cloud
                           backends; the indexer's embedder client
                           (``OpenAIEmbedder``) speaks this dialect
                           against any compliant provider.
    GET  /v1/models      — minimal OpenAI ``/v1/models`` shim so OpenAI
                           clients that probe model availability do not
                           404 against this service.
    POST /embed          — legacy single-key embedding endpoint
                           (``{"embedding": [...]}`` for a single input,
                           ``{"embeddings": [...]}`` for a list). Kept as
                           a deprecated alias during the OpenAI cutover;
                           slated for removal in a follow-up PR once
                           every consumer is on /v1/embeddings.
    POST /rerank         — Qwen3-Reranker yes/no logit scoring over
                           (query, documents) pairs. No OpenAI standard
                           exists for rerank, so this stays in the
                           service's own namespace.
    GET  /health         — model load state + approximate RSS.

Authorization:
    The service binds 127.0.0.1 only and is intended for loopback use
    from the indexer (via host.docker.internal from containers). The
    ``Authorization`` header is accepted and ignored — OpenAI-style
    clients that always send a Bearer token still work, and clients
    that omit it work too. Do NOT change the bind address without
    revisiting this assumption.

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

# NB: ``mlx.core`` and ``mlx_embeddings`` are deliberately *not* imported
# at module load. ``import mlx.core`` triggers Metal device initialization
# on macOS, which fails on headless / CI environments without a GPU
# (e.g. ``RuntimeError: [metal::load_device] No Metal device available``).
# Tests need to be able to ``from src import main`` without hitting Metal,
# so model and tokenizer use of MLX is gated to the lazy-load path inside
# ``_ModelHandle.get`` and the per-call helpers ``_embed_one`` /
# ``_rerank_score`` below. Production behavior is unchanged: the first
# real ``/embed`` or ``/rerank`` request still triggers the same load.
from fastapi import FastAPI, HTTPException
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
                    # Lazy: see module-top docstring on why MLX imports
                    # are deferred. First call here is the first place
                    # production touches Metal.
                    from mlx_embeddings import load

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
    import mlx.core as mx  # lazy — see module-top docstring

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
    import mlx.core as mx  # lazy — see module-top docstring

    body = f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"
    body_ids = tokenizer.encode(body, add_special_tokens=False)
    budget = RERANK_MAX_LENGTH - len(meta["prefix_ids"]) - len(meta["suffix_ids"])
    if len(body_ids) > budget:
        body_ids = body_ids[:budget]
    full_ids = meta["prefix_ids"] + body_ids + meta["suffix_ids"]
    ids = mx.array([full_ids])
    out = model(ids)
    # Recover vocab-sized logits for the last token.
    #
    # ``mlx-embeddings`` loads Qwen3-Reranker-4B with the embedding
    # head exposed (``last_hidden_state``, ``text_embeds``,
    # ``pooler_output``) but no ``logits`` field — the lm_head
    # projection isn't surfaced. We project the last hidden state
    # back to vocab using the input embedding, which is correct
    # *only because* the model card declares
    # ``tie_word_embeddings=True`` (lm_head shares weights with
    # ``model.embed_tokens``). ``QuantizedEmbedding.as_linear`` is the
    # MLX-supported way to apply a tied-weight matmul against an
    # mxfp8 embedding without manual dequant.
    #
    # If a future refactor swaps in a Qwen3-Reranker variant where
    # ``tie_word_embeddings=False``, this block silently produces
    # garbage scores. Verify ``model.config.tie_word_embeddings`` at
    # load time before changing this code, and fall back to mlx-lm
    # (which carries a real CausalLM head) if the assumption breaks.
    last_hidden = out.last_hidden_state[:, -1, :]
    logits = embed_layer.as_linear(last_hidden)
    yes_logit = logits[0, meta["yes_id"]].item()
    no_logit = logits[0, meta["no_id"]].item()
    # Numerically stable log-softmax over the 2-token {no, yes} subset,
    # take exp of the yes index → P(yes | {yes, no}). This is the
    # documented Qwen3-Reranker scoring path (the model is *trained*
    # to be used this way; it's not a generic LLM yes/no workaround).
    m = max(yes_logit, no_logit)
    denom = math.log(math.exp(yes_logit - m) + math.exp(no_logit - m)) + m
    return math.exp(yes_logit - denom)


def _resident_mb() -> float:
    """Process resident memory in MiB. macOS reports ru_maxrss in bytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024)


class EmbedRequest(BaseModel):
    input: str | list[str]


class OpenAIEmbedRequest(BaseModel):
    """Subset of OpenAI's /v1/embeddings request body.

    ``model`` is accepted for protocol-compatibility but ignored — this
    service hosts a single embedder (``EMBED_MODEL_ID``). ``dimensions``
    and ``encoding_format`` are accepted but ignored: the embedder
    always returns the model's native dimension as floats.
    """

    input: str | list[str]
    model: str | None = None
    encoding_format: str | None = None
    dimensions: int | None = None
    user: str | None = None


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


@app.post("/v1/embeddings")
def openai_embeddings(req: OpenAIEmbedRequest) -> dict[str, Any]:
    """OpenAI-compatible embeddings endpoint.

    Accepts the standard ``{"model", "input"}`` body and returns the
    ``{"object": "list", "data": [...]}`` shape so the same indexer
    client works against this service, DeepInfra, OpenRouter, LM Studio,
    vLLM, TEI, etc. ``model`` is accepted for protocol-compatibility
    and ignored — this service hosts a single embedder.
    """
    inputs = [req.input] if isinstance(req.input, str) else req.input
    if not inputs:
        raise HTTPException(status_code=400, detail="input is empty")
    model, tokenizer = _embedder.get()
    vectors = _embed_batch(model, tokenizer, inputs)
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vectors)
        ],
        "model": EMBED_MODEL_ID,
        # Token accounting is not tracked by mlx-embeddings; report 0
        # rather than synthesizing an estimate the operator might rely
        # on for billing reconciliation.
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@app.get("/v1/models")
def openai_models() -> dict[str, Any]:
    """Minimal OpenAI /v1/models shim so OpenAI clients that probe
    model availability do not 404 against this service. Lists only the
    embedder; the reranker is not OpenAI-shaped and is not advertised
    here.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": EMBED_MODEL_ID,
                "object": "model",
                "owned_by": "mlx-service",
            },
        ],
    }


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict[str, Any]:
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents list is empty")
    model, tokenizer = _reranker.get()
    # Fail-fast on the load-bearing assumption that justifies
    # ``embed_layer.as_linear`` in ``_rerank_score``. If a future
    # model swap breaks weight tying we want a loud error here, not
    # silently-wrong scores in retrieval.
    if not getattr(model.config, "tie_word_embeddings", False):
        raise HTTPException(
            status_code=500,
            detail=(
                f"reranker model {RERANK_MODEL_ID!r} has tie_word_embeddings=False; "
                "the embed_tokens.as_linear() logit recovery path requires tied "
                "weights — switch the reranker to mlx-lm (which carries a real "
                "CausalLM head) or use a tied-embedding variant."
            ),
        )
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

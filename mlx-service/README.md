# mlx-service

Host-side FastAPI service that serves embeddings and reranking on Apple
Metal via MLX. Runs as a bare-metal LaunchAgent (not in Docker — MLX
needs Metal access). Bound to `127.0.0.1:8001`. Reachable from the
indexer and mcp-server containers via `host.docker.internal:8001`.

## Models

- Embedder: `mlx-community/Qwen3-Embedding-8B-mxfp8` (4096-dim, ~8 GB resident)
- Reranker: `mlx-community/Qwen3-Reranker-4B-mxfp8` (~4 GB resident,
  generation-style yes/no logit scoring per Qwen3-Reranker's trained
  behavior)

Both load lazily on first request and stay resident.

## Endpoints

- `POST /v1/embeddings` — OpenAI-compatible. Body
  `{"model": str?, "input": str | list[str]}` → returns
  `{"object": "list", "data": [{"object": "embedding", "embedding": [...], "index": int}, ...], "model": str, "usage": {...}}`.
  `model` is accepted for protocol-compatibility and ignored — this
  service hosts a single embedder (`MLX_EMBED_MODEL`). The
  `Authorization` header is accepted and ignored (loopback service).
  This is the canonical embed endpoint; the indexer's `OpenAIEmbedder`
  client and mcp-server's `LocalLLMClient.embed()` both speak this
  shape against any compliant provider (mlx-service, DeepInfra,
  OpenRouter, LM Studio, vLLM, TEI).
- `GET /v1/models` — minimal OpenAI shim listing the embedder's id so
  OpenAI clients that probe model availability do not 404. The
  reranker is not OpenAI-shaped and is not advertised here.
- `POST /rerank` — `{"query": str, "documents": [str, ...], "top_n": int?}`
  → `{"results": [{"index": int, "score": float}, ...]}` sorted desc by
  score. There is no OpenAI rerank standard, so this stays in the
  service's own namespace.
- `POST /embed` — **deprecated.** Legacy single-key endpoint
  (`{"embedding": [...]}` for a single input, `{"embeddings": [...]}`
  for a list) kept for one release as a transitional alias for
  `/v1/embeddings`. Slated for removal in a follow-up PR once every
  consumer is on `/v1/embeddings`.
- `GET /health` — model load state + approximate process resident memory.

## Local run

```bash
uv sync
uv run uvicorn src.main:app --host 127.0.0.1 --port 8001
```

First call to each endpoint triggers a model download from
`mlx-community` into `~/.cache/huggingface/hub/`. Subsequent calls reuse
the cached weights.

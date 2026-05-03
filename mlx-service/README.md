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

- `POST /embed` — `{"input": str | list[str]}` → for single string,
  returns `{"embedding": [...]}` (matches Ollama `/api/embeddings`); for
  list input, returns `{"embeddings": [[...], ...]}`
- `POST /rerank` — `{"query": str, "documents": [str, ...], "top_n": int?}`
  → `{"results": [{"index": int, "score": float}, ...]}` sorted desc by
  score
- `GET /health` — model load state + approximate process resident memory

## Local run

```bash
uv sync
uv run uvicorn src.main:app --host 127.0.0.1 --port 8001
```

First call to each endpoint triggers a model download from
`mlx-community` into `~/.cache/huggingface/hub/`. Subsequent calls reuse
the cached weights.

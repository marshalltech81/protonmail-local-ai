# mlx-lm-server

Host-side LLM server that serves OpenAI-compatible chat completions on
Apple Metal via MLX. Runs as a bare-metal LaunchAgent (not in Docker —
MLX needs Metal access). Bound to `127.0.0.1:8002`. Reachable from the
mcp-server container via `host.docker.internal:8002`.

This is upstream Apple's `mlx_lm.server` CLI wrapped in a LaunchAgent;
no project code lives here besides the LaunchAgent plist and the pinned
`pyproject.toml`. The companion service for embeddings + reranking is
[`mlx-service/`](../mlx-service/) on port 8001.

## Model

Default: `mlx-community/Qwen3-32B-4bit` (~17 GB resident). Adjust the
`--model` flag in the LaunchAgent plist if you want a smaller variant
(e.g. `mlx-community/Qwen3-14B-4bit` at ~8 GB).

The model loads lazily on the first chat-completions request and stays
resident.

## Endpoints

OpenAI-compatible. The same shape Ollama exposes at `:11434/v1` and
that the project's `LocalLLMClient.complete()` posts to:

- `POST /v1/chat/completions` — body `{"model": "...", "messages": [...]}` →
  `{"choices": [{"message": {"content": "..."}}]}`
- `POST /v1/completions` — bare prompt completion (unused by this project)
- `GET /v1/models` — lists the loaded model

## Local run (foreground)

Run from this directory (`mlx-lm-server/`):

```bash
uv sync
uv run mlx_lm.server \
    --model mlx-community/Qwen3-32B-4bit \
    --host 127.0.0.1 \
    --port 8002 \
    --log-level INFO \
    --max-tokens 4096
```

`--max-tokens 4096` matches the LaunchAgent setting and gives Qwen3's
thinking-mode answers headroom; without it `mlx_lm.server` falls back
to its 512-token default and long answers truncate.

First run downloads the model from `mlx-community` into
`~/.cache/huggingface/hub/`. Subsequent runs reuse the cached weights.

## LaunchAgent install

The vendored
[`com.local.mlx-lm-server.plist.template`](com.local.mlx-lm-server.plist.template)
contains `__REPO_ROOT__` and `__USER_HOME__` placeholders so it stays
portable across clones. The install script auto-detects the repo
root from its own location, so it works from either the repo root or
this directory:

```bash
# From the repo root:
./mlx-lm-server/install-launchagent.sh
# Or from inside this directory:
./install-launchagent.sh

launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.local.mlx-lm-server.plist
launchctl print "gui/$(id -u)/com.local.mlx-lm-server" | head
```

Logs land at `~/Library/Logs/mlx-lm-server.log`. Restart with:

```bash
launchctl kickstart -k "gui/$(id -u)/com.local.mlx-lm-server"
```

To remove:

```bash
launchctl bootout "gui/$(id -u)/com.local.mlx-lm-server"
rm ~/Library/LaunchAgents/com.local.mlx-lm-server.plist
```

After `uv sync` rebuilds the venv, re-run `./install-launchagent.sh`
to regenerate the plist and then `launchctl kickstart` to restart the
agent. The plist references absolute paths in `.venv/`, which uv
recreates on rebuild.

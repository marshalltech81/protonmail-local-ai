# protonmail-local-ai

A fully local, privacy-first AI search and intelligence layer for ProtonMail.
Ask questions about your inbox in plain English. Everything stays on your machine.

## What It Does

- **Semantic search** — find emails by meaning, not just keywords
- **Hybrid search** — BM25 keyword + vector similarity merged for best results
- **Thread-aware** — indexes at the conversation level, not individual messages
- **Q&A / RAG** — ask natural language questions, get answers grounded in your email
- **Structured extraction** — pull invoices, dates, action items into structured data
- **Agentic** — multi-step reasoning across your entire mailbox
- **MCP interface** — works directly inside Claude Desktop

## Stack

| Component | Role |
|---|---|
| ProtonBridge | Decrypts ProtonMail, exposes local IMAP/SMTP |
| mbsync | Real-time incremental sync to local Maildir |
| Indexer | Parses threads, generates embeddings, builds SQLite index |
| mlx-service (host) | Apple Metal embedder (Qwen3-Embedding-8B) + reranker (Qwen3-Reranker-4B) on `:8001` |
| mlx-lm-server (host) | Apple Metal LLM serving (default Qwen3-32B-4bit) for `LLM_MODE=local`, OpenAI-compatible at `:8002/v1` |
| SQLite (FTS5 + sqlite-vec) | Hybrid keyword + vector search index |
| MCP Server | Exposes tools to Claude Desktop via HTTP/SSE |

## Prerequisites

- **Docker Desktop** for Mac (or Linux with Docker Engine)
- **Claude Desktop** with MCP support
- **Proton Mail paid account** (required for Bridge access)

> **Apple Silicon / ARM64**: fully supported. The stack builds and runs natively on aarch64.

## Quick Start

### 1. Clone and configure

```bash
git clone git@github.com:marshalltech81/protonmail-local-ai.git
cd protonmail-local-ai
cp .env.example .env
make init-secrets
```

`make init-secrets` creates placeholder secret files under `.secrets/` that
Docker Compose requires before starting. You will fill them in during setup.

### 2. Build images

```bash
make build
```

### 3. First-time Bridge login (one time only)

```bash
make first-run
# Inside the CLI:
#   login  → enter Proton credentials + 2FA
#   info   → copy Bridge username into .env
#            write Bridge password to .secrets/bridge_pass.txt
#   exit
```

After `info`, write the Bridge password to the secret file with owner-only
permissions:

```bash
printf '%s' 'bridge-generated-pass' > .secrets/bridge_pass.txt
chmod 600 .secrets/bridge_pass.txt
```

### 4. Set up the host-side MLX servers

Two LaunchAgents run on the host (not in Docker — MLX needs Metal
access): `mlx-service` for embeddings + reranking on `:8001`, and
`mlx-lm-server` for the local LLM on `:8002`. Containers reach both
via OrbStack's `host.docker.internal`.

See [`mlx-service/README.md`](mlx-service/README.md) and
[`mlx-lm-server/README.md`](mlx-lm-server/README.md) for the install
steps. Models download lazily on first use into
`~/.cache/huggingface/hub/`.

### 5. Start the stack

```bash
make up
make logs  # verify everything is running
```

`make up` now runs a security preflight first: it validates `.env`, checks that
the Bridge password secret exists and is non-empty, and enforces `600`
permissions on secret files before Docker Compose starts the stack.

By default, the MCP server runs in read-only mode:
- search, retrieval, and intelligence tools are available
- mail-changing action tools are not registered
- retrieval is served from the local SQLite index rather than direct IMAP access
- any future live write path must use explicit cert-pinned TLS; insecure
  fallback behavior is rejected rather than attempted

### 6. Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "protonmail-local-ai": {
      "url": "http://localhost:3000/sse"
    }
  }
}
```

Restart Claude Desktop. You should now see the ProtonMail tools available.

## Usage Examples

Once connected, ask Claude Desktop:

- *"Search my email for anything about my lease renewal"*
- *"What did my accountant say about Q3 taxes?"*
- *"Find all invoices from last year and extract the vendor names and amounts"*
- *"Summarize the thread about the project deadline"*
- *"Show me recent INBOX threads with attachments"*

## Privacy Boundaries

This stack is local-first for **storage, sync, indexing, and embeddings**.
Whether your *conversations* are also local depends on which MCP client you
use and which `LLM_MODE` you select. Be deliberate about all three layers.

### 1. Storage and indexing layer — always local

| Data | Where it lives |
|---|---|
| Your emails (Maildir) | Local Docker volume — never leaves your machine |
| Embeddings | Generated locally by mlx-service on Apple Metal — never sent anywhere |
| Search index (SQLite FTS5 + sqlite-vec) | Local Docker volume |
| Bridge ↔ Proton traffic | The only path off your machine for mail data |

The MCP server itself binds to `127.0.0.1:3000` only — nothing else on your
network can reach it.

### 2. Project-internal LLM layer — controlled by `LLM_MODE`

The MCP server's intelligence tools (`ask_mailbox`, `summarize_thread`,
`extract_from_emails`) need an LLM for generation. Where that runs depends on
`LLM_MODE` in `.env`:

| Mode | What happens to retrieved email content |
|---|---|
| `local` (default) | Sent to the host-side mlx-lm-server (Apple Metal) at `LLM_BASE_URL`. Stays on your machine. |
| `cloud` | Sent to Anthropic's Claude API for generation. Requires `.secrets/anthropic_api_key.txt`. |

This setting only governs what the MCP server does *internally* during a tool
call. It does not govern what your MCP *client* does with the result.

### 3. MCP client layer — Claude Desktop is a cloud product

This is the boundary most easily missed. **Claude Desktop is not a local
LLM.** When you wire it up to this MCP server:

1. You ask Claude Desktop a question.
2. Claude (running on Anthropic's servers) decides to call an MCP tool.
3. Claude Desktop relays the call to your local MCP server. The tool runs
   locally — including, in `LLM_MODE=local`, any LLM work via the host
   mlx-lm-server.
4. The tool's *return value* (which often contains email snippets, thread
   bodies, or LLM-generated answers grounded in your mail) is sent back to
   Claude on Anthropic's servers as part of the conversation context.
5. Claude generates the next response using that data as input.

So: **using Claude Desktop as your client transmits email content (whatever
the called tools return) to Anthropic, regardless of `LLM_MODE`.** Anthropic's
data handling for Claude Desktop applies — see Anthropic's current privacy
policy for retention and training-use details.

If you want end-to-end local conversations:

- Drive the MCP intelligence tools directly via `docker exec mcp-server
  python -c "..."`. Less ergonomic; nothing leaves your laptop.
- Or use a different MCP client backed by a local LLM. For Open WebUI, set
  `MCP_TRANSPORT=dual` and run `make open-webui-up` (the target auto-generates
  the session-key secret on first run; for the very first launch, prepend
  `OPEN_WEBUI_ENABLE_SIGNUP=true` to create the admin account — see
  `docs/setup.md` for the full first-run flow including MCP server
  registration). Open WebUI reaches the host-side mlx-lm-server via
  `host.docker.internal:8002/v1` (OpenAI-compatible) for chat, and uses
  `http://mcp-server:3000/mcp` as a Streamable HTTP MCP server. Quality
  varies.

Most users accept the Claude-Desktop-as-frontend tradeoff because the
alternative is much less useful, but it is a real tradeoff and it is not
the same posture as "everything stays local."

## Updating Bridge

When Proton releases a new Bridge version:

```bash
# 1. Update BRIDGE_VERSION in .env
# 2. Verify the local patch and runtime assumptions still hold
make bridge-upgrade-check
# 3. Rebuild and restart
make update
```

## Bridge Re-authentication

If Bridge credentials expire or you change your Proton password:

```bash
make down
docker volume rm protonmail-local-ai_bridge-data
make first-run   # log in again
# update BRIDGE_USER in .env
# write the new Bridge password to .secrets/bridge_pass.txt
make up
```

Your email index is preserved in a separate volume — only Bridge credentials are reset.

## Commands

```bash
make build        # Build all Docker images
make validate-env # Check .env values and secret permissions before startup
make up           # Start the full stack
make down         # Stop the full stack
make logs         # Tail all logs
make first-run    # One-time Bridge login
make bridge-patch-check   # Verify Bridge patch points against upstream source
make bridge-smoke         # Build and smoke test the Bridge image
make bridge-upgrade-check # Run both Bridge upgrade guard checks
make update       # Update Bridge to new version
make status       # Container and index status
make clean        # Remove everything (destructive)
```

## Disclaimer

This is an unofficial community project and is not affiliated with
or endorsed by Proton AG.

## License

MIT

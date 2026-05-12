# protonmail-local-ai

A privacy-first AI search and intelligence layer for ProtonMail. Ask
questions about your inbox in plain English. Email storage, sync, and
indexing always stay on your machine; whether the LLM and embedder
calls are local or remote is your choice — point them at any
OpenAI-compatible / Anthropic-compatible provider, including a
host-side server you install yourself.

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
| Embedder (operator-supplied) | OpenAI-compatible `/v1/embeddings`. Point `EMBED_BASE_URL` at any compliant provider — remote (DeepInfra, OpenRouter) or a host-side server you install yourself (LM Studio, vLLM, TEI, `mlx_lm.server`) |
| Inference (operator-supplied) | Anthropic-compatible Messages API by default (`INFERENCE_MODE=anthropic`); switch to `INFERENCE_MODE=openai` for any OpenAI-compatible chat-completions endpoint at `INFERENCE_BASE_URL` |
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

### 4. Configure your embedder and inference provider

Edit `.env` to point at the providers you want to use:

- `EMBED_BASE_URL` + `EMBED_MODEL` — OpenAI-compatible
  embedder. Remote (DeepInfra, OpenRouter) or a host-side server you
  install yourself (LM Studio, vLLM, TEI, `mlx_lm.server`). Containers
  reach a host-side server via OrbStack's `host.docker.internal`.
  The schema reserves a fixed 4096-dim vector — pick a model that
  produces 4096-dim vectors (Qwen3-Embedding-8B variants) or run a
  schema migration.
- `INFERENCE_MODE` — `anthropic` (default) uses the official
  `anthropic` SDK against the Messages API; `openai` uses the
  official `openai` SDK against any OpenAI-compatible chat-completions
  endpoint at `INFERENCE_BASE_URL`; `none` skips the intelligence
  tools.
- `RERANK_MODE` — `cohere` enables Cohere rerank via the official
  `cohere` SDK; `none` (default) returns RRF order directly.
- API keys go in `.secrets/inference_api_key.txt`,
  `.secrets/embed_api_key.txt`, and `.secrets/rerank_api_key.txt`
  (`chmod 600`). Leave any unused ones empty.

See [`docs/setup.md`](docs/setup.md) for end-to-end examples.

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

Email storage, sync, and indexing always stay on your machine. Whether
embeddings, inference, and your *conversations* leave the host depends
on three independent choices: which embedder URL you wire up, which
`INFERENCE_MODE` you select, and which MCP client you connect. Be
deliberate about all three layers.

### 1. Storage and indexing layer — always local

| Data | Where it lives |
|---|---|
| Your emails (Maildir) | Local Docker volume — never leaves your machine |
| Embeddings | Wherever `EMBED_BASE_URL` points. A host-side server (LM Studio, vLLM, `mlx_lm.server`, TEI, etc.) keeps email body chunks on your machine; a remote provider (DeepInfra, OpenRouter, etc.) ships chunks to that provider at index time and search-query strings at retrieval time. |
| Search index (SQLite FTS5 + sqlite-vec) | Local Docker volume |
| Bridge ↔ Proton traffic | The only mandatory path off your machine for mail data |

The MCP server itself binds to `127.0.0.1:3000` only — nothing else on your
network can reach it.

### 2. Project-internal LLM layer — controlled by `INFERENCE_MODE`

The MCP server's intelligence tools (`ask_mailbox`, `summarize_thread`,
`extract_from_emails`) need an LLM for generation. Where that runs depends on
`INFERENCE_MODE` in `.env`:

| Mode | What happens to retrieved email content |
|---|---|
| `anthropic` (default) | Sent to the Anthropic-compatible Messages API at `INFERENCE_BASE_URL`. Requires `.secrets/inference_api_key.txt`. |
| `openai` | Sent to the OpenAI-compatible chat-completions endpoint at `INFERENCE_BASE_URL`. If that endpoint is a host-side server you install yourself (LM Studio, vLLM, `mlx_lm.server`), retrieved chunks stay on your machine; if it's a remote provider, they ship to that provider. |

This setting only governs what the MCP server does *internally* during a tool
call. It does not govern what your MCP *client* does with the result.

### 3. MCP client layer — Claude Desktop is a cloud product

This is the boundary most easily missed. **Claude Desktop is not a local
LLM.** When you wire it up to this MCP server:

1. You ask Claude Desktop a question.
2. Claude (running on Anthropic's servers) decides to call an MCP tool.
3. Claude Desktop relays the call to your local MCP server. The tool runs
   locally — including any LLM work the tool does internally against the
   endpoint you configured for `INFERENCE_MODE`.
4. The tool's *return value* (which often contains email snippets, thread
   bodies, or LLM-generated answers grounded in your mail) is sent back to
   Claude on Anthropic's servers as part of the conversation context.
5. Claude generates the next response using that data as input.

So: **using Claude Desktop as your client transmits email content (whatever
the called tools return) to Anthropic, regardless of `INFERENCE_MODE`.** Anthropic's
data handling for Claude Desktop applies — see Anthropic's current privacy
policy for retention and training-use details.

If you want end-to-end local conversations:

- Drive the MCP intelligence tools directly via `docker exec mcp-server
  python -c "..."`. Less ergonomic; nothing leaves your laptop.
- Or use another MCP client backed by a local LLM. Keep the client bound to
  localhost and point it at the MCP server transport it supports (`/sse` by
  default, or `/mcp` when `MCP_TRANSPORT=streamable-http` or `dual`).

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

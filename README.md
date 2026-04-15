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
| Ollama | Local embedding model (`nomic-embed-text`) and LLM |
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

### 4. Pull Ollama models

```bash
make pull-models
```

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
- *"Show me all unread emails in my INBOX"*

## Privacy Model

| Data | Where it lives |
|---|---|
| Your emails | Local Docker volume — never leaves your machine |
| Embeddings | Generated locally by Ollama — never sent anywhere |
| Search index | Local SQLite in Docker volume |
| Q&A context | Sent to Ollama locally by default |
| Q&A context (cloud mode) | Optionally sent to Claude API — opt-in when `LLM_MODE=cloud` |

Set `LLM_MODE=local` in `.env` (default) to keep everything fully local.
Set `LLM_MODE=cloud` and write your Anthropic key to
`.secrets/anthropic_api_key.txt` to use Claude API for Q&A.

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
make pull-models  # Pull Ollama models
make update       # Update Bridge to new version
make status       # Container and index status
make clean        # Remove everything (destructive)
```

## Disclaimer

This is an unofficial community project and is not affiliated with
or endorsed by Proton AG.

## License

MIT

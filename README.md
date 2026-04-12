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
mkdir -m 700 .secrets
```

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

### 4. Pull Ollama models

```bash
make pull-models
```

### 5. Start the stack

```bash
make up
make logs  # verify everything is running
```

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
Set `LLM_MODE=cloud` and provide `ANTHROPIC_API_KEY` to use Claude API for Q&A.

## Updating Bridge

When Proton releases a new Bridge version:

```bash
# 1. Update BRIDGE_VERSION in .env
# 2. Rebuild and restart
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
make up           # Start the full stack
make down         # Stop the full stack
make logs         # Tail all logs
make first-run    # One-time Bridge login
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

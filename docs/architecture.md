# Architecture

## Overview

protonmail-local-ai is a fully containerised, privacy-first AI search and
intelligence layer for ProtonMail. Every component runs locally in Docker.
The only optional external call is to the Claude API for Q&A, and that is
opt-in per session.

## Data Flow

```
ProtonMail Cloud (encrypted)
        │
        │  HTTPS (E2E encrypted by Proton)
        ▼
ProtonBridge container
  - Decrypts email using your private key
  - Exposes local IMAP on port 1143
  - Exposes local SMTP on port 1025
  - Credentials persisted in bridge-data volume
        │
        │  IMAP (localhost, internal Docker network)
        ▼
mbsync container
  - Polls Bridge IMAP every SYNC_INTERVAL seconds
  - Writes Maildir format to maildir-volume
  - Maintains sync state for incremental updates
        │
        │  Maildir files (shared volume, read-only for indexer)
        ▼
indexer container
  - Watches maildir-volume via inotify
  - Parses .eml files: MIME, HTML→text, attachments
  - Groups messages into threads via In-Reply-To / References headers
  - Calls Ollama for vector embeddings
  - Writes to SQLite (FTS5 keyword index + sqlite-vec vector index)
        │                              │
        │  embed API (internal)        │  writes
        ▼                              ▼
ollama container              sqlite-volume
  - nomic-embed-text            - threads table (FTS5)
    for embeddings              - threads_vec table (sqlite-vec)
  - llama3.2 (or other)         - message_thread_map
    for local Q&A               - indexed_files
        │
        │  reads sqlite-volume (read-only)
        ▼
mcp-server container
  - Exposes MCP tools via HTTP/SSE on port 3000
  - Hybrid search: BM25 + vector → RRF merge
  - Q&A: retrieves threads → prompts Ollama or Claude API
  - Actions: connects to Bridge IMAP/SMTP for write ops
        │
        │  HTTP/SSE (localhost:3000)
        ▼
Claude Desktop (host machine)
  - Calls MCP tools via natural language
  - Receives structured responses
```

## Container Responsibilities

| Container | Reads from | Writes to | Exposes |
|---|---|---|---|
| `protonmail-bridge` | ProtonMail Cloud | `bridge-data` vol | IMAP 1143, SMTP 1025 (internal) |
| `mbsync` | Bridge IMAP | `maildir-volume` | nothing |
| `ollama` | model requests | `ollama-models` vol | HTTP 11434 (internal) |
| `indexer` | `maildir-volume`, Ollama | `sqlite-volume` | nothing |
| `mcp-server` | `sqlite-volume`, Bridge IMAP/SMTP | nothing | HTTP 3000 (localhost only) |

## Docker Volumes

| Volume | Contents | Back up? |
|---|---|---|
| `bridge-data` | Bridge credentials, GPG key, config | Yes — losing this requires re-login |
| `maildir-volume` | Raw email in Maildir format | Optional — mbsync can re-sync |
| `ollama-models` | Downloaded Ollama model weights | Optional — can re-pull |
| `sqlite-volume` | SQLite index (FTS5 + vectors) | Optional — indexer can rebuild |

## Networking

All containers share the `protonmail-net` bridge network and communicate
using container names as hostnames (e.g. `protonmail-bridge`, `ollama`).

Only one port is exposed to the host: `127.0.0.1:3000` for the MCP server.
No container is reachable from outside the machine.

## Search Architecture

The hybrid search pipeline:

```
User query
    │
    ├─ Embed query text → nomic-embed-text → 768-dim vector
    │
    ├─ BM25 search → SQLite FTS5 virtual table → ranked list A
    │
    ├─ Vector search → sqlite-vec → ranked list B
    │
    └─ Reciprocal Rank Fusion (k=60) → merged list → top-k threads
```

RRF merges the two ranked lists without needing to normalise scores.
Each result's RRF score = 1/(k + rank_in_A) + 1/(k + rank_in_B).
Results appearing in both lists are boosted significantly.

## Thread Indexing

Emails are indexed at the **thread level**, not the message level.
This is the key architectural decision that makes Q&A useful:

1. Messages are grouped using `In-Reply-To` and `References` headers
2. Failing that, subject normalisation within the same folder
3. Each thread gets one embedding representing the full conversation
4. New messages arriving in a thread update the thread's embedding

A query like "what did my landlord say about the heating?" returns the full
landlord thread, not individual one-liners that happen to mention heating.

## Privacy Model

| Operation | Local only | Leaves machine |
|---|---|---|
| Email storage | ✅ | Never |
| Embedding generation | ✅ (Ollama) | Never |
| Vector index | ✅ (SQLite) | Never |
| Keyword search | ✅ (SQLite FTS5) | Never |
| Q&A (local mode) | ✅ (Ollama LLM) | Never |
| Q&A (cloud mode) | Retrieval local | Retrieved chunks → Anthropic API |
| Send/Move/Flag | ✅ (via Bridge) | Never (Bridge handles E2E) |

## LLM Mode Toggle

Set `LLM_MODE` in `.env`:

- `local` — all LLM inference via Ollama. Fully private. Slower on CPU.
- `cloud` — Q&A and agentic tasks use Claude API. Better quality. Retrieved
  email chunks are sent to Anthropic's servers.

The toggle applies per-deployment. A per-session toggle is on the roadmap.

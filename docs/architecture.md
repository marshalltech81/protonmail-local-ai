# Architecture

## Overview

protonmail-local-ai is a fully containerised, privacy-first AI search and
intelligence layer for ProtonMail. Every component runs locally in Docker.
The only optional external call is to the Claude API for Q&A, and that is
opt-in at deployment time via `LLM_MODE=cloud`.

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
  - Polls Bridge IMAP every SYNC_INTERVAL seconds in a bounded retry loop
  - Writes Maildir format to maildir-volume
  - Maintains sync state for incremental updates
  - Fails closed if cert extraction or repeated sync attempts fail
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
                                - pending_deletions (reconciler)
        │
        │  reads sqlite-volume (connection opened read-only)
        ▼
mcp-server container
  - Exposes MCP tools via HTTP/SSE on port 3000
  - Hybrid search: BM25 + vector → RRF merge
  - Q&A: retrieves threads → prompts Ollama or Claude API
  - Retrieval: serves indexed mailbox data from SQLite
  - Actions: disabled by default via MCP read-only mode
  - Any future live write path must be cert-pinned and explicitly enabled
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
| `mcp-server` | `sqlite-volume`, Ollama | nothing | HTTP 3000 (localhost only) |

## Docker Volumes

| Volume | Contents | Back up? |
|---|---|---|
| `bridge-data` | Bridge credentials, GPG key, config | Yes — losing this requires re-login |
| `maildir-volume` | Raw email in Maildir format | Optional — mbsync can re-sync |
| `ollama-models` | Downloaded Ollama model weights | Optional — can re-pull |
| `sqlite-volume` | SQLite index (FTS5 + vectors) | Optional — indexer can rebuild |

## Networking

The stack uses two isolated bridge networks:

- `bridge-net` for ProtonBridge ↔ `mbsync`
- `app-net` for `indexer` ↔ `ollama` ↔ `mcp-server`

For stricter local-only deployments, `docker-compose.hardened.yml` marks
`app-net` as `internal: true` so those services cannot reach the internet.
Use it only after pulling Ollama models and only with `LLM_MODE=local`.

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

## Deletion Reconciliation (opt-in)

`mbsync` is configured `Sync Pull` + `Expunge None`, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP `\Deleted` (Maildir `T`)
flag. Without reconciliation, the local SQLite index keeps those messages
forever.

The indexer ships an opt-in reconciler
(`INDEXER_DELETION_ENABLED=true`) that handles this in two phases:

1. **Tombstone** — a startup sweep plus a live `on_moved` watchdog handler
   record every `T`-flagged file in a `pending_deletions` table. No primary
   data is mutated at tombstone time, so the soft-delete is fully reversible
   if mbsync un-flags the file on a later pull.
2. **Reap** — after a configurable grace window
   (`INDEXER_DELETION_GRACE_DAYS`, default 7 days) the reaper removes the
   reaped message's rows from `message_thread_map` / `indexed_files`, and
   either rebuilds the parent thread from the surviving messages on disk
   (re-parsed, re-embedded) or deletes the thread entirely when nothing
   remains. Ollama failures during rebuild cause the reaper to back off and
   retry on the next pass.

A **mass-delete brake** (`INDEXER_DELETION_MAX_BATCH_PCT`, default 5%) caps
the fraction of total indexed messages the reaper will touch in a single
pass. Transient Bridge outages (vault rebuilds, folder renames, auth
glitches) can cause mbsync to `T`-flag a huge batch at once; the brake
stops the reaper from acting, while still recording tombstones that will
clear themselves if mbsync reverts the flags. `INDEXER_DELETION_FORCE=true`
overrides the brake for intentional bulk cleanups.

`mbsync` keeps `Expunge None` regardless — the reaper cleans up the local
index; it does not change mbsync's pull-only, no-destructive-delete posture
on the Maildir itself.

## MCP Read-Only Enforcement

`mcp-server` never mutates the SQLite index. Read-only posture is enforced
at two layers:

1. The connection is opened via the SQLite URI `file:{path}?mode=ro`, so
   the underlying connection cannot issue writes — any `INSERT`/`UPDATE`/
   `DELETE` raises `OperationalError: attempt to write a readonly database`
   before it reaches the storage engine.
2. `PRAGMA query_only = ON` is set immediately after connect as
   defense-in-depth.

The `sqlite-volume` is mounted writable into `mcp-server` so SQLite can
create the `-shm` sidecar needed for WAL readers. Without the sidecar,
MCP reads would either fail at startup or fall back to a stale-only mode
that does not reflect in-flight indexer writes. Making the application
layer read-only while keeping the filesystem writable gives both
correctness (live WAL visibility) and safety (no mutating path exists).

## Concurrency (indexer)

The indexer serves two concurrent DB writers on a single `sqlite3`
connection:

- the watchdog observer thread, via `MaildirHandler.on_created` /
  `on_moved` callbacks
- the main loop, via periodic `Reconciler.sweep()` / `reap()` passes

`sqlite3.connect(check_same_thread=False)` lets both threads share the
connection, but the Python-level `BEGIN IMMEDIATE` / execute / commit
sequence is not atomic across threads and can raise "cannot start a
transaction within a transaction" or silently commit partial state. A
per-instance `threading.RLock` wraps every public `Database` method so
transactions are serialized from the caller's perspective.

## FTS Rowid Tracking

`threads_fts` is a contentless FTS5 virtual table. Under SQLite's default
contentless configuration two things are true that matter here:

- `DELETE FROM threads_fts WHERE thread_id = ?` silently no-ops (the
  contentless table does not support DELETE), so every update used to
  accumulate stale rows.
- `UNINDEXED` columns always read back as `NULL`, so the MCP keyword-search
  join on `threads_fts.thread_id` could not return any rows.

Schema v3 rebuilds `threads_fts` with `contentless_delete=1` (SQLite ≥ 3.43)
and stores each row's rowid in `threads.fts_rowid`. Writes delete by rowid
before re-inserting; the MCP keyword search joins on
`threads_fts.rowid = threads.fts_rowid`. This fixes both the stale-token
problem and the always-null join.

## Privacy Model

| Operation | Local only | Leaves machine |
|---|---|---|
| Email storage | ✅ | Never |
| Embedding generation | ✅ (Ollama) | Never |
| Vector index | ✅ (SQLite) | Never |
| Keyword search | ✅ (SQLite FTS5) | Never |
| Q&A (local mode) | ✅ (Ollama LLM) | Never |
| Q&A (cloud mode) | Retrieval local | Retrieved chunks → Anthropic API |
| Send/Move/Flag | Disabled by default | Never |

## LLM Mode Toggle

Set `LLM_MODE` in `.env`:

- `local` — all LLM inference via Ollama. Fully private. Slower on CPU.
- `cloud` — Q&A and agentic tasks use Claude API. Better quality. Retrieved
  email chunks are sent to Anthropic's servers.

The toggle applies per-deployment. A per-session toggle is on the roadmap.

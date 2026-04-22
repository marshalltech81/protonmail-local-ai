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
  - Pins Bridge TLS cert on first boot (SHA-256 fingerprint stored in
    mbsync-state volume); refuses to sync on mismatch unless the operator
    sets BRIDGE_CERT_PIN_ROTATE=true for a legitimate rotation
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
  - Serves GET /health for the container healthcheck (200 when the
    read-only SQLite connection answers, 503 otherwise)
  - Hybrid search: BM25 + vector → RRF merge
  - Q&A: retrieves threads → prompts Ollama or Claude API
  - Retrieval: serves indexed mailbox data from SQLite
  - Email excerpts sent to the LLM are wrapped in <untrusted_email>
    tags and framed as untrusted data — defense against prompt
    injection from attacker-controlled email content
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

The stored `body_text` that feeds FTS5 is the full accumulated thread
content (users legitimately search quoted text and signatures). The
input to the embedding model, however, is first passed through
`strip_for_embedding` (`indexer/src/quoting.py`) to remove quoted
replies, signatures, and forward headers. Without that pass the vector
for a long reply chain drifts toward whatever content was quoted most
often — typically the original message or a contract below the
signature — and stops reflecting the substantive content of the latest
replies. Stripping is intentionally conservative: quoted text is still
searchable through FTS and falls back to the original body when the
stripped result would be empty.

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
clear themselves if mbsync reverts the flags. An absolute floor of 10
tombstones per pass is always allowed regardless of the percentage so
that routine cleanup on small mailboxes is not gated by the 5% default.
`INDEXER_DELETION_FORCE=true` overrides the brake for intentional bulk
cleanups.

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

## Durable Indexing Queue

Schema v8 adds the `indexing_jobs` table. The watchdog callbacks and
`initial_index` no longer run the parse / embed / upsert pipeline
inline — they `enqueue` each filepath and return immediately. A worker
loop in the main thread drains the queue via `drain_queue`, capped at
`HEALTH_REFRESH_EVERY` jobs per pass so the reconciler and health-file
refresh aren't starved by a large event burst.

Each job carries `attempts`, `last_stage`, `last_error`, and a
`next_attempt_at` scheduled via exponential backoff
(`base_backoff_seconds × 2^(attempts - 1)`, capped at 6 hours). When
`attempts` reaches `INDEXER_MAX_ATTEMPTS` (default 5), the row
transitions to `status = 'dead'` — it stays in the table for operator
visibility and stops being claimed. Re-enqueuing a path (for instance
a new mbsync delivery that reuses the filename) resets the row to
`queued` with `attempts = 0`, so `dead` is "give up for now," not
"never try again."

Two environment variables shape the queue: `INDEXER_MAX_ATTEMPTS` and
`INDEXER_RETRY_BASE_SECONDS`. Neither is required — the defaults are
suitable for typical mailboxes, and both are documented in
`docs/setup.md` for operators who need to tune retry aggressiveness
against an unreliable Ollama or a flaky mailbox.

Observability: `queue.stats()` returns `{queued, dead}` counts and is
logged at startup when the queue carries non-zero depth from a prior
run. The main loop's periodic health-file refresh continues
independent of queue depth, so a stuck queue does not mark the
container unhealthy (dead jobs are a data issue, not a liveness
issue).

## File Identity on `indexed_files`

Schema v7 augments `indexed_files` with `size`, `mtime_ns`, and
`content_hash` (SHA-256 over the raw file bytes) captured at
`parse_email` time. `is_indexed` stays filepath-keyed — the hot path
remains an O(1) primary-key lookup — and the new columns are written
alongside. On a flag-only mbsync rename (`msg:2,S` → `msg:2,SR`)
`update_filepath` carries the captured identity forward rather than
clearing it, because the file contents on disk are unchanged.

The columns exist to let future reconciler passes distinguish a
flag-only rename from a genuine content change, and to spot a "file
vanished from path A but the same `content_hash` reappears at path B"
rename that mbsync performed without emitting an `on_moved` event.
`find_indexed_paths_by_content_hash` is the lookup that unlocks those
passes; consumers are deliberately not wired in this revision so the
schema change lands as a pure extension.

Rows indexed before v7 (or for which stat / hash capture failed) carry
NULL identity values and are skipped by the content-hash lookup. There
is no migration-time disk walk; backfill is lazy, driven by re-indexing
or by rename events.

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

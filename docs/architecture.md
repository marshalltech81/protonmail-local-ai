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
  - Calls mlx-service (default) or Ollama for vector embeddings
  - Writes to SQLite (FTS5 keyword index + sqlite-vec vector index)
        │                              │
        │  embed API                   │  writes
        ▼                              ▼
mlx-service (host process)    sqlite-volume
  - Qwen3-Embedding-8B-mxfp8    - threads table (FTS5)
    (4096-dim) on Apple Metal   - threads_vec table (sqlite-vec)
  - Qwen3-Reranker-4B-mxfp8     - message_thread_map
    on Apple Metal              - indexed_files
                                - pending_deletions (reconciler)
ollama (host process)
  - qwen2.5 (or other) for
    local LLM inference (LLM_MODE=local)
  - reached from containers via
    host.docker.internal:11434
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

| Container / process | Reads from | Writes to | Exposes |
|---|---|---|---|
| `protonmail-bridge` | ProtonMail Cloud | `bridge-data` vol | IMAP 1143, SMTP 1025 (internal) |
| `mbsync` | Bridge IMAP | `maildir-volume` | nothing |
| `ollama` (host process, not Docker) | model requests | `~/.ollama/` model cache | HTTP 11434 (host bind, reached from Docker via `host.docker.internal`). See `docs/setup.md` for the LaunchAgent + firewall install. |
| `mlx-service` (host process, not Docker) | embed/rerank requests | `~/.cache/huggingface/` model cache | HTTP 8001 (loopback only); reached from Docker via `host.docker.internal`. See `docs/setup.md` for the LaunchAgent install. |
| `indexer` | `maildir-volume`, mlx-service (or Ollama) | `sqlite-volume` | nothing |
| `mcp-server` | `sqlite-volume`, mlx-service (or Ollama), Ollama (LLM) | nothing | HTTP 3000 (localhost only) |
| `open-webui` (optional overlay) | Ollama, `mcp-server` | `open-webui-data` vol | HTTP 8080 (localhost only) |

## Docker Volumes

| Volume | Contents | Back up? |
|---|---|---|
| `bridge-data` | Bridge credentials, GPG key, config | Yes — losing this requires re-login |
| `maildir-volume` | Raw email in Maildir format | Optional — mbsync can re-sync |
| `sqlite-volume` | SQLite index (FTS5 + vectors) | Optional — indexer can rebuild |
| `open-webui-data` | Optional Open WebUI accounts, settings, and chats | Yes, if you want to preserve UI state |

## Networking

The stack uses two isolated bridge networks:

- `bridge-net` for ProtonBridge ↔ `mbsync`
- `app-net` for `indexer` ↔ `mcp-server`. Both reach the host Ollama
  via `host.docker.internal:11434` rather than an in-stack container.
- The optional Open WebUI overlay joins `app-net` and reaches the
  same host Ollama via `host.docker.internal:11434`; it does not run
  its own Ollama.

For stricter local-only deployments, `docker-compose.hardened.yml` marks
`app-net` as `internal: true` so those services cannot reach the internet.

> **Currently broken in the default stack.** After the host-Ollama-as-default
> change (PR #86), the indexer + mcp-server reach Ollama and mlx-service via
> `host.docker.internal`. Whether `host.docker.internal` resolves through a
> network with `internal: true` is runtime-dependent (Docker Desktop and
> OrbStack behave differently and the OrbStack case is unverified). Until the
> overlay is reworked to either move both Ollama and mlx-service into
> containers on `app-net` or explicitly punch `host.docker.internal` through,
> applying it will likely cut off Ollama LLM calls (`LLM_MODE=local`), the
> Ollama embed fallback, and the MLX `/embed` and `/rerank` calls. The
> compose file itself carries the same warning. Do not apply it as-is.

The default stack exposes only `127.0.0.1:3000` for the MCP server. The
optional Open WebUI overlay also exposes `127.0.0.1:8080` for the browser UI.
No container is reachable from outside the machine.

### Ollama (host install, not containerized)

Ollama runs as a host process — `brew install ollama` plus the
LaunchAgent + firewall setup in `docs/setup.md`. Containers reach it
via OrbStack's `host.docker.internal:11434`. This is the only
supported deployment shape: containerized Ollama on macOS cannot use
Metal, so an in-stack Ollama would silently lose Metal acceleration.
The host listener must be bound to `0.0.0.0:11434` so OrbStack
containers can reach it; the macOS Application Firewall must be
enabled with a binary-level block on `/opt/homebrew/bin/ollama` so
the listener is not reachable from the LAN.

## Search Architecture

The hybrid search pipeline:

```
User query
    │
    ├─ Embed query text → Qwen3-Embedding-8B (mlx-service) → 4096-dim vector
    │   (USE_MLX_EMBEDDER=false falls back to Ollama nomic-embed-text @ 768)
    │
    ├─ BM25 search   → SQLite FTS5 over thread bodies      → ranked list A
    │
    ├─ Vector search → sqlite-vec over thread vectors      → ranked list B
    │
    ├─ Vector search → sqlite-vec over per-message chunks  → ranked list C
    │                  (chunks "lifted" to parent thread_id)
    │
    ├─ Reciprocal Rank Fusion (k=60) → merged candidate list
    │
    ├─ optional: post-fusion filter (folder / sender / date / attachments)
    │
    ├─ optional rerank stage (USE_MLX_RERANKER=true, default):
    │   take RRF top RERANK_CANDIDATES (default 50), score each candidate
    │   against the query via Qwen3-Reranker-4B (yes/no logit
    │   comparison), reorder, truncate to the caller's `limit`
    │   (defaulting to RERANK_TOP_N when the caller doesn't specify —
    │   so callers like extract_from_emails(limit=20) get 20, not 10)
    │
    └─ top-k threads (with evidence chunks if requested)
```

RRF merges the three ranked lists without needing to normalise scores.
Each thread's RRF score = sum over lanes of `1/(k + rank_in_lane + 1)`.
The chunk lane credits each thread by the rank of its **best** chunk
only — without that dedup, a thread with many similar sibling chunks
would dominate by accumulated score rather than by relevance.

Every thread is chunked at index time, so the chunk lane is always
populated alongside the BM25 and thread-vector lanes.

The rerank stage is best-effort: a transient `mlx-service` failure
returns an empty result set from the reranker, and `hybrid_search`
falls back to RRF order truncated to the caller's `limit`. A rerank
outage degrades quality without failing the whole query.

## Thread Indexing

Emails are indexed at the **thread level** as the coarse unit of
discovery, with **per-message chunks** as the precise unit of retrieval.

1. Messages are grouped using `In-Reply-To` and `References` headers
2. Failing that, subject normalisation within the same folder
3. Each message's body is sliced into paragraph-packed chunks
   (`indexer/src/chunker.py`); each chunk is FTS-indexed and gets its
   own vector embedding stored in `message_chunks_vec`
4. The thread's vector in `threads_vec` is the **mean** of its chunk
   vectors — coarse and precise retrieval derive from the same source
5. New messages joining a thread emit new chunks (idempotent diff
   write keyed on deterministic chunk IDs) and rewrite the parent
   thread's vector

The indexer commits the thread row, message map, body chunks, attachment
occurrences, and final thread vector inside one SQLite transaction. Chunk and
attachment rows have foreign-key parents in `threads` / `message_thread_map`,
and SQLite foreign-key enforcement is enabled on the indexer connection, so
partial sidecar rows fail closed instead of becoming orphan retrieval state.

The stored `body_text` on `threads` still feeds FTS5 over the full
accumulated thread content (users legitimately search quoted text and
signatures). Chunk inputs are first passed through
`strip_for_embedding` (`indexer/src/quoting.py`) so chunk vectors track
substantive content of each reply rather than accumulated quoted
history. Stripping is intentionally conservative: quoted text is still
searchable through FTS and falls back to the original body when the
stripped result would be empty.

A query like "what did my landlord say about the heating?" returns the
full landlord thread (via the coarse lanes) and surfaces the specific
chunk where the heating discussion appears (via the chunk lane). The
intelligence tools (`ask_mailbox`, `extract_from_emails`) feed those
matched chunks to the LLM with `[chunk N chars X-Y]` provenance
headers rather than the truncated accumulated body, so answers cite
exact passages.

### Chunk write idempotency

Chunk IDs are `sha256(message_pk || index || chunk_text)` — the same
body always produces the same ID set. The indexer's per-message chunk
write diffs the new chunk IDs against stored IDs, embeds only the new
ones, and deletes any that are no longer present. Re-running on
unchanged input is therefore zero embed cost. Attachment chunks use a
composite `message_pk` of `f"{message_id}::{attachment_id}"` so their
chunk IDs are distinct from body chunks for the same message.

## Attachment Indexing

Email attachments flow through the same chunker and embedder pipeline
as message bodies. Two extra tables sit alongside `message_chunks`:

| Table | Keyed by | Purpose |
|---|---|---|
| `attachments` | attachment_occurrence_id | Per-occurrence row capturing filename + MIME + size as it appeared on a specific email. The occurrence id includes the message, payload hash, filename, and attachment slot so duplicate same-payload files in one email are still represented. |
| `attachment_extractions` | attachment_id (= sha256 of payload) | Per-content-hash cache of extracted text + status. The expensive work (Tesseract OCR, pypdf parse, DOCX walk) runs at most once per unique payload. Non-success rows are also honored: `empty` / `too_large` / `unsupported` short-circuit unconditionally; `failed` short-circuits within a 7-day retry window so a chronic failure stops re-running on every reappearance, but a real fix landed via dependency upgrade can pick the payload up later. |

Per-occurrence chunks land in `message_chunks` with the
`attachment_id` column populated. They embed exactly like body chunks
and surface through the same chunk-vector retrieval lane — so a query
matching a PDF's contents lifts the parent thread of the email that
carried it, with zero new MCP search code.

### Extractor dispatch

`indexer.extractors.extract` resolves a (content_type, filename) pair
to a per-format module:

```
content_type → _MIME_DISPATCH (text/plain, application/pdf, ...)
   ↓ unknown MIME
filename ext → _EXT_DISPATCH (.pdf, .docx, .xlsx, .png, ...)
   ↓ no match
status="unsupported" (still searchable by filename via attachments_fts)
```

Per-format modules live under `indexer/src/extractors/` and are
lazy-imported so a missing optional dependency (e.g. `python-docx`
not in this image) downgrades to `unsupported` rather than crashing
the indexer at startup.

### OCR

PDFs and images route through Tesseract when `INDEXER_OCR_ENABLED=true`
(default). The PDF extractor first tries the digital text layer via
`pypdf`; if the result is below a small minimum-character threshold,
it falls through to rendering each page via Poppler (`pdf2image`) and
OCR'ing via `pytesseract`. `INDEXER_OCR_MAX_PAGES` (default 20) caps
the cost on long scanned documents.

### Cost bounds

| Knob | Default | Purpose |
|---|---|---|
| `INDEXER_ATTACHMENT_EXTRACTION_ENABLED` | `true` | Master switch — turns the whole pipeline off if needed |
| `INDEXER_OCR_ENABLED` | `true` | Disables all OCR paths (image + PDF fallback) |
| `INDEXER_ATTACHMENT_MAX_BYTES` | `10000000` (10 MB) | Skip very large attachments — bounds CPU/memory for huge zips |
| `INDEXER_OCR_MAX_PAGES` | `20` | Cap pages OCR'd per PDF |
| `INDEXER_OCR_TIMEOUT_SECONDS` | `60` | Per-page Tesseract timeout — bounds runaway OCR on a crafted high-noise image. Set `0` to disable. |
| `INDEXER_PDF_MAX_DIGITAL_PAGES` | `500` | Cap pages walked by the digital pypdf path — protects against text-only PDFs with thousands of pages. Set `0` to disable. |
| `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` | `2000000` (~500 pages) | Truncate extracted text before persisting in `attachment_extractions`. Bounds SQLite row size for very long OCR'd PDFs. Set to `0` to disable. |

### Cascade on message removal

When a message is reaped, `_delete_attachments_for_message` drops its
`attachments` rows and FTS shadows; the `_delete_chunks_for_message`
cascade also drops the message's attachment chunks (they share the
`message_id` key). Cached extractions in `attachment_extractions` are
**deliberately preserved** — another message may still reference the
same content_hash, and even when nothing does today the cached
extraction means a future re-arrival skips the OCR cost.

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

`threads_fts` is created with `contentless_delete=1` (SQLite ≥ 3.43) and
each row's rowid is stored in `threads.fts_rowid`. Writes delete by rowid
before re-inserting; the MCP keyword search joins on
`threads_fts.rowid = threads.fts_rowid`. This avoids both the stale-token
problem and the always-null join.

## Durable Indexing Queue

The `indexing_jobs` table backs the durable queue. The watchdog
callbacks and `initial_index` no longer run the parse / embed / upsert
pipeline
inline — they `enqueue` each filepath and return immediately. A worker
loop in the main thread drains the queue via `drain_queue`, capped at
`HEALTH_REFRESH_EVERY` jobs per pass so the reconciler and health-file
refresh aren't starved by a large event burst.

Each job carries `attempts`, `last_stage`, `last_error`, and a
`next_attempt_at` scheduled via exponential backoff
(`base_backoff_seconds × 2^(attempts - 1)`, capped at 6 hours). When
`attempts` reaches `INDEXER_MAX_ATTEMPTS` (default 5), the row
transitions to `status = 'dead'` — it stays in the table for operator
visibility and stops being claimed. Watchdog rename / create events
(`on_moved`, `on_created`) DO reset a `dead` row to `queued` with
`attempts = 0` because those events signal real change in the
underlying file. The initial scan does NOT — it only proves the
file exists on disk, not that anything about its content has
changed since the last failure, so re-enqueuing every dead row at
container restart would just re-run the same retry cascade against
the same upstream condition. The scan therefore consults
`queue.is_dead(filepath)` and skips dead-lettered files, leaving
them dead until something explicitly resets them.

Two stage outcomes short-circuit the retry path entirely:

- `parse_skipped_missing` — `parse_email` raised `FileNotFoundError`,
  almost always because mbsync renamed the file (added an IMAP flag
  suffix) between enqueue and read. The path is permanently invalid;
  the renamed file enters the queue under its new name via a fresh
  `IN_MOVED_TO` event. The worker calls `mark_skipped` instead of
  `mark_failed`: row deleted, no retry, no dead-letter.
- `PermissionError` at parse keeps the existing retry path because
  the file genuinely exists; the mbsync chmod race resolves on a
  later sync cycle.

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

`indexed_files` carries `size`, `mtime_ns`, and `content_hash`
(SHA-256 over the raw file bytes) captured at
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

Rows for which `stat` / hash capture failed at parse time carry NULL
identity values and are skipped by the content-hash lookup; the
columns are populated lazily on the next reindex of the file.

## Privacy Model

Three layers, each with its own boundary. The README has the operator-facing
walkthrough; the table below is the per-operation reference.

### Storage and processing layer (always local)

| Operation | Local only | Leaves machine |
|---|---|---|
| Email storage | ✅ | Never |
| Embedding generation | ✅ (Ollama) | Never |
| Vector index | ✅ (SQLite) | Never |
| Keyword search | ✅ (SQLite FTS5) | Never |
| Send/Move/Flag | Disabled by default | Never |

### MCP server intelligence tools (governed by `LLM_MODE`)

| Operation | Local only | Leaves machine |
|---|---|---|
| Q&A — `LLM_MODE=local` | ✅ (Ollama LLM) | Never |
| Q&A — `LLM_MODE=cloud` | Retrieval local | Retrieved chunks → Anthropic Claude API |

### MCP client layer (governed by which client you connect)

When the MCP server is consumed by a cloud-backed client, the tool *return
values* are sent to that client's backend as part of the conversation
context. Tool results often contain email snippets, full thread bodies, or
LLM-generated answers grounded in mail — so the client's backend sees that
content regardless of `LLM_MODE`.

| Client | What sees tool results |
|---|---|
| Claude Desktop | Anthropic (Claude runs in the cloud; tool results go back as conversation context) |
| Local-LLM MCP client (e.g. Open WebUI backed by Ollama) | Stays on machine |
| Direct `docker exec` into mcp-server | Stays on machine |

`LLM_MODE` and the MCP client choice are independent boundaries and must
both be set deliberately if "fully local conversations" is a goal.

The MCP server defaults to SSE for existing Claude Desktop compatibility.
Set `MCP_TRANSPORT=streamable-http` for clients that only speak Streamable
HTTP, or `MCP_TRANSPORT=dual` to serve both `/sse` and `/mcp` on the same
localhost-bound port.

## LLM Mode Toggle

Set `LLM_MODE` in `.env`:

- `local` — all LLM inference via Ollama. Fully private. Slower on CPU.
- `cloud` — Q&A and agentic tasks use Claude API. Better quality. Retrieved
  email chunks are sent to Anthropic's servers.

The toggle applies per-deployment. A per-session toggle is on the roadmap.

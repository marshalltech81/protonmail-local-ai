# Architecture

## Overview

protonmail-local-ai is a containerised, privacy-first AI search and
intelligence layer for ProtonMail. The four containers (Bridge, mbsync,
indexer, mcp-server) all run locally; storage, sync, and indexing
never leave the host. Embedding and inference are operator-supplied
external dependencies — the project itself ships no model-serving
components, and whether they run on the host (LM Studio, vLLM,
`mlx_lm.server`, etc.) or against a remote provider is a deployment
choice.

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
  - After each successful sync, writes a last-sync stamp
    (`.mbsync-last-sync.json`) at the Maildir root
        │
        │  Maildir files (shared volume, read-only for indexer)
        ▼
indexer container
  - Watches maildir-volume via inotify
  - Parses .eml files: MIME, HTML→text, attachments
  - Groups messages into threads via In-Reply-To / References headers
  - Calls EMBED_BASE_URL for vector embeddings
  - Writes to SQLite (FTS5 keyword index + sqlite-vec vector index)
  - Records mbsync's last sync and its own liveness in `ingestion_state`
        │                              │
        │  embed API                   │  writes
        ▼                              ▼
embedder (operator-supplied)  sqlite-volume
  - OpenAI-compatible            - threads + threads_fts + threads_vec
    /v1/embeddings at            - message_chunks + _fts + _vec
    EMBED_BASE_URL               - messages, message_participants,
  - 4096-dim vectors required      message_thread_map
    by current schema            - attachments + attachments_fts,
                                   attachment_extractions
                                 - indexed_files, indexing_jobs,
                                   ingestion_state
                                 - pending_deletions (reconciler)
                                 - entities, entity_aliases

inference (operator-supplied)
  - INFERENCE_MODE=anthropic →
    Messages API at
    INFERENCE_BASE_URL
  - INFERENCE_MODE=openai →
    /v1/chat/completions at
    INFERENCE_BASE_URL
        │
        │  reads sqlite-volume (connection opened read-only)
        ▼
mcp-server container
  - Exposes MCP tools via HTTP/SSE on port 3000, built on the
    standalone `fastmcp` package
  - Rejects any HTTP request whose Host is not localhost, 127.0.0.1,
    [::1] or mcp-server (any port) with 421, and a browser Origin
    outside the same names over http with 403, before it reaches a
    transport (DNS-rebinding defense)
  - Serves GET /health for the container healthcheck (200 when the
    read-only SQLite connection answers, 503 otherwise)
  - Hybrid search: three FTS5 lanes + two vector lanes → RRF merge
    (optional rerank stage when RERANK_MODE=cohere)
  - Q&A: retrieves threads → prompts the configured inference provider
  - Retrieval: serves indexed mailbox data from SQLite
  - Email excerpts sent to the LLM are wrapped in <untrusted_email>
    tags and framed as untrusted data — defense against prompt
    injection from attacker-controlled email content
  - Read-only: no mail-changing tools and no connection to Bridge
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
| `indexer` | `maildir-volume`, embedder | `sqlite-volume` | nothing |
| `mcp-server` | `sqlite-volume`, embedder, inference provider, optional reranker | nothing | HTTP 3000 (localhost only) |
| Embedder (operator-supplied) | indexer + mcp-server requests | depends on provider | OpenAI-compatible `/v1/embeddings` at `EMBED_BASE_URL`. A host-side server reachable via `host.docker.internal`, or a remote provider. |
| Inference (operator-supplied) | mcp-server intelligence-tool requests | depends on provider | Anthropic Messages API or OpenAI `/v1/chat/completions` selected by `INFERENCE_MODE`. |
| Reranker (operator-supplied, optional) | mcp-server hybrid-search requests | Cohere | Cohere rerank API via the official `cohere` SDK when `RERANK_MODE=cohere`. `RERANK_BASE_URL` optional (empty = SDK default). |

## Docker Volumes

| Volume | Contents | Back up? |
|---|---|---|
| `bridge-data` | Bridge credentials and TLS cert (`vault.enc` under `/data/config`), GPG key (`/data/gnupg`), pass store (`/data/pass`), Gluon IMAP cache and logs (`/data/local`), cache (`/data/cache`) | Yes — back up `/data/config`, `/data/gnupg`, and `/data/pass` together: `vault.enc` cannot be decrypted without the GPG key, so a backup missing `gnupg/` is useless, and the Bridge entrypoint refuses to start over it. `/data/local` and `/data/cache` can be omitted — Bridge rebuilds them from Proton — but rebuilding the Gluon cache re-downloads the whole mailbox, which can take hours. Losing the whole volume means re-login plus that full re-download. |
| `maildir-volume` | Raw email in Maildir format | Optional — mbsync can re-sync |
| `sqlite-volume` | SQLite index (FTS5 + vectors) | Optional — indexer can rebuild |

## Networking

The stack uses two isolated bridge networks:

- `bridge-net` for ProtonBridge ↔ `mbsync`
- `app-net` for `indexer` ↔ `mcp-server`. Both reach the
  operator-supplied embedder, inference, and (optional) reranker —
  either at remote URLs or, when the operator runs a host-side server,
  via OrbStack's `host.docker.internal`.

For stricter deployments, `docker-compose.hardened.yml` marks `app-net`
as `internal: true`. This is compatible with operator-installed
host-side providers (subject to Docker runtime behaviour around
`host.docker.internal` under `internal: true`) and intentionally
incompatible with any remote provider — the overlay is meant for the
"keep all retrieval traffic on the box" posture.

The default stack exposes only `127.0.0.1:3000` for the MCP server.
No container is reachable from outside the machine.

### Operator-supplied providers

The embedder surface is OpenAI-compatible (`/v1/embeddings`). The
indexer's `OpenAIEmbedder` client and mcp-server's `EmbedClient` both
use the official `openai` SDK with a custom `base_url`, so an
operator can point `EMBED_BASE_URL` at any compliant provider —
DeepInfra, OpenRouter, LM Studio, vLLM, TEI, `mlx_lm.server` —
without changing any code, provided the model returns 4096-dim
vectors. The schema reserves a fixed 4096-dim
vector, so `EMBED_MODEL` must keep producing 4096-dim vectors
(Qwen3-Embedding-8B variants) or a schema migration is required.
Indexer and mcp-server must point at the same provider + model so
query vectors are comparable to indexed vectors. `EMBED_MODE=openai`
is the only valid value — embed has no disabled mode because
semantic / hybrid search is the headline retrieval feature and the
indexer cannot ingest mail without an embedder.

For inference, `INFERENCE_MODE=anthropic` (default) uses the
official `anthropic` SDK against the Messages API; leave
`INFERENCE_BASE_URL` empty for the SDK default.
`INFERENCE_MODE=openai` uses the official `openai` SDK against any
OpenAI-compatible chat-completions endpoint at `INFERENCE_BASE_URL`.
`INFERENCE_MODE=none` skips registration of the intelligence tools.

Reranking is opt-in via `RERANK_MODE`. `RERANK_MODE=cohere` uses
the official `cohere` SDK against the Cohere rerank API; leave
`RERANK_BASE_URL` empty for the SDK default. `RERANK_MODE=none`
(default) returns RRF order directly.

When operator-installed servers run on the host, they should bind to
`127.0.0.1` only. Containers reach them via OrbStack's
`host.docker.internal` — this is the project's expectation. Binding a
host-side server to `0.0.0.0` or a LAN IP exposes it to the network
beyond what the project intends.

## Search Architecture

The hybrid search pipeline:

```
User query
    │
    ├─ Embed query text → OpenAI /v1/embeddings at EMBED_BASE_URL → 4096-dim vector
    │
    ├─ Keyword list — three FTS5 lanes, fused with RRF into one ranked list:
    │    thread_fts     → BM25 over thread subject, participants, and
    │                     accumulated body
    │    chunk_fts      → BM25 over body and attachment-text chunks
    │                     (lifted to parent thread_id)
    │    attachment_fts → BM25 over attachment filenames / MIME types
    │
    ├─ thread_vec → sqlite-vec over thread vectors
    │
    ├─ chunk_vec  → sqlite-vec over body and attachment-text chunks
    │               (lifted to parent thread_id)
    │
    ├─ Reciprocal Rank Fusion (k=60) of keyword list + thread_vec + chunk_vec
    │
    ├─ optional: post-fusion filter (folder / sender / participant / date /
    │   attachments)
    │
    ├─ optional rerank stage (RERANK_MODE=cohere, default none):
    │   take the fused top max(limit, RERANK_CANDIDATES (default 20)), score each
    │   candidate against the query via the Cohere rerank API (official
    │   cohere SDK), reorder, and truncate to the caller's `limit`
    │
    └─ top-k threads (with evidence chunks if requested)
```

Retrieval runs five lanes in two RRF stages. The three FTS5 lanes are
fused first into a single keyword list — the same list `mode=keyword`
returns — and that list then enters the outer fusion alongside the two
vector lanes. RRF merges ranked lists without needing to normalise
scores: each thread's score is the sum over lists of
`1/(k + rank_in_list + 1)`. The chunk lanes credit each thread by the
rank of its **best** chunk only — without that dedup, a thread with
many similar sibling chunks would dominate by accumulated score rather
than by relevance. `get_evidence(include_scores=True)` reports which
lanes (`thread_fts` / `chunk_fts` / `attachment_fts` / `thread_vec` /
`chunk_vec` / `rerank`) each returned thread matched.

Folder, date, and attachment-flag filters are pushed into the FTS
lanes' SQL so deep-ranked matches are not truncated before they could
qualify; sqlite-vec has no equivalent pushdown, so the vector lanes
run unfiltered and the post-fusion filter applies every filter
uniformly. When a filter is active, each vector lane doubles its KNN
`k` and re-queries until its window holds enough eligible threads for
the request (`max(limit, RERANK_CANDIDATES)` for hybrid), the table is
exhausted, or `k` reaches sqlite-vec's 4096 cap. The query is embedded
once. This is ranked search, not enumeration: an eligible thread
outside a lane's 4096 nearest rows is still missed by that lane.

Threads with no chunk rows — empty bodies, or chunks whose embedding
has not landed yet — never appear in the chunk lanes and rank on the
thread-level lanes alone.

The rerank stage is best-effort: a transient rerank-service failure
returns an empty result set from the reranker, and `hybrid_search`
falls back to RRF order truncated to the caller's `limit`. A ranking
with an out-of-range or repeated index falls back the same way. A rerank
outage degrades quality without failing the whole query.

## Thread Indexing

Emails are indexed at the **thread level** as the coarse unit of
discovery, with **per-message chunks** as the precise unit of retrieval.

1. Messages are grouped using `In-Reply-To` and `References` headers;
   a message already indexed keeps its thread when it is reprocessed
2. Failing that, subject normalisation within the same folder, only
   when the message's sender and one of its other recipients are both
   already in the thread and it falls within 60 days of the thread's
   last message. One shared address is not enough: the mailbox owner
   is a recipient of nearly every message and the sender of every
   outgoing one, so two vendors' "Invoice" mails would otherwise merge
3. Each message's body is sliced into paragraph-packed chunks
   (`indexer/src/chunker.py`); each chunk is FTS-indexed and gets its
   own vector embedding stored in `message_chunks_vec`
4. The thread's vector in `threads_vec` is the **L2-normalized mean**
   of its chunk vectors — coarse and precise retrieval derive from
   the same source, and storing a unit-norm vector lets cosine
   similarity downstream collapse to a dot product
5. New messages joining a thread emit new chunks (idempotent diff
   write keyed on deterministic chunk IDs) and rewrite the parent
   thread's vector

The indexer writes a thread in two phases. **Phase 1** commits the
thread row, `message_thread_map` entry, and `indexed_files` row in a
single `upsert_thread` call, with a seed thread vector chosen from a
three-case priority chain (chunks-mean of any pre-existing chunks,
preserved non-zero prior `threads_vec` row, or a placeholder zero).
Threading state is durable at this point so the next message in the
batch sees this thread when computing its own assignment. **Phase 2c**
then commits the body chunks (`message_chunks` + `message_chunks_fts`
+ `message_chunks_vec`), attachment occurrences, attachment-extraction
cache rows, attachment chunks, and the final normalized thread vector
in one per-message transaction. The two phases are separate
transactions so a crash between them leaves a thread row + message map
+ indexed-file marker with the seed vector but no chunks or attachment
rows yet — the queue retries Phase 2c on the next pass and the
seed-vector chain converges. Within each phase, chunk and attachment
rows have foreign-key parents in `threads` / `message_thread_map`, and
SQLite foreign-key enforcement is enabled on the indexer connection,
so partial sidecar rows fail closed instead of becoming orphan
retrieval state.

Every vector written to `threads_vec` and `message_chunks_vec` is
L2-normalized at the DB write boundary (`upsert_thread`,
`replace_thread_vector`, `_rewrite_thread_row`,
`replace_message_chunks`). A genuine zero placeholder — the Phase 1
seed for a brand-new thread before Phase 2c lands real chunk embeddings
— is preserved unchanged; dividing by zero would NaN-poison the row.
The storage invariant is end-to-end so cosine ranking does not depend
on per-row magnitude.

The stored `body_text` on `threads` still feeds FTS5 over the full
accumulated thread content (users legitimately search quoted text and
signatures). Chunk inputs are first passed through
`strip_for_embedding` (`indexer/src/quoting.py`) so chunk vectors track
substantive content of each reply rather than accumulated quoted
history. Stripping is intentionally conservative: quoted text is still
searchable through FTS and falls back to the original body when the
stripped result would be empty.

A reply can rename the conversation and still join it through
References / In-Reply-To. Its subject, when it differs from the
thread's after `Re:`/`Fwd:` normalization, is kept searchable (#303):

- Keyword: the `subject` column of the thread's `threads_fts` row holds
  the thread subject plus each distinct changed reply subject
  (normalized, at most `FTS_REPLY_SUBJECTS_MAX` subjects and
  `FTS_REPLY_SUBJECTS_MAX_CHARS` characters), so a body already at its
  token cap cannot drop them; `threads.subject` stays the thread
  subject. Each rewrite of the row examines at most the oldest
  `FTS_SUBJECT_SCAN_ROWS` subjects of the thread, each cut to
  `FTS_SUBJECT_SCAN_CHARS` characters, so a change first seen past
  those bounds is not added.
- Semantic: the embedding input of the reply's first body chunk is
  prefixed with `Subject: <its subject>`, cut so the input stays within
  `INDEXER_CHUNK_MAX_TOKENS` and dropped when the chunk alone is at that
  ceiling. The stored chunk text, offsets and chunk ID stay body-only,
  since chunks are the authoritative body store (`get_message`,
  `query_messages(text=...)`).

A renamed reply with no body text (for example attachment-only) has no
body chunk to carry the prefix, so its subject is keyword-searchable
only, with no semantic representation.

**Message body assembly (#295, #298):** a message's body text is every
non-blank inline `text/plain` and `text/html` part outside attachments
(HTML through html2text), in document order, separated by a blank line.
The parts of a `multipart/alternative` are renderings of one body, so
it contributes a single child: the first carrying non-blank plain text,
else the first carrying any text. A whitespace-only plain alternative
therefore gives way to the HTML one. A `multipart/related` contributes
only its root, taken to be its first part (RFC 2387's default), since
its other parts are resources the root refers to; a `start` parameter
naming a different root is not read, so such a message gets its first
part's text instead. The parts of any other container
(`multipart/mixed`, an inline `message/rfc822`) are
sequential content, so text, an attachment, then more text keeps both
texts. Nothing inside an attachment, such as a forwarded email attached
as a file, is body text. Neither is an inline `message/*` part sent
in a transfer encoding (base64 or quoted-printable, which RFC 2046
forbids for it): the parser exposes it as its encoded transport text,
so it adds nothing to the body. At most 200 text parts per message
(`MAX_BODY_TEXT_PARTS`) are decoded; later ones are left out.

A query like "what did my landlord say about the heating?" returns the
full landlord thread (via the coarse lanes) and surfaces the specific
chunk where the heating discussion appears (via the chunk lane). The
intelligence tools (`ask_mailbox`, `extract_from_emails`) feed those
matched chunks to the LLM with `[chunk N chars X-Y]` provenance
headers rather than the truncated accumulated body. `ask_mailbox`
labels each passage (`E1`, `E2` ...) with its message's claimant ID,
sender and sent date, and checks the labels its answer cites against
the passages supplied (see `docs/mcp-tools.md`).

### Chunk write idempotency

Chunk IDs are `sha256(message_pk || index || chunk_text)` — the same
body always produces the same ID set. The indexer's per-message chunk
write diffs the new chunk IDs against stored IDs, embeds only the new
ones, and deletes any that are no longer present. Re-running on
unchanged input is therefore zero embed cost. A body chunk's
`message_pk` is its message's claimant ID (see Per-Message Records);
attachment chunks use a composite `message_pk` of
`f"{claimant_id}::{attachment_id}"` so their chunk IDs are distinct from
body chunks for the same message.

## Per-Message Records

Threads are the retrieval unit; `messages` is the authoritative
per-message record.

**Claimant IDs.** A Message-ID is set by the sender, so two different
files can claim the same one, by accident or to overwrite another
message's evidence. Every per-message row is therefore keyed by a
claimant ID rather than the bare Message-ID: the Message-ID plus `#`
and the first eight hex digits of the SHA-256 of the file's raw bytes
(`parser.claimant_id`). The bytes are the identity because nothing that
happens to a Maildir file changes them: flags and the delivery name
live in the filename and the folder is the directory, so a flag rename,
a folder move and a reparse keep the key, while different content gets
a new one. Both claimants are kept, each with its own `messages`,
`message_thread_map`, `message_participants`, chunk and attachment
rows (attachment occurrence IDs and chunk `message_pk`s are derived
from the claimant ID), and reaping or reprocessing one never touches
the other's rows. Neither wins by arrival order. Thread membership
still resolves by Message-ID (In-Reply-To / References and the
known-Message-ID lookup), so a second claimant joins the first one's
thread; `threads.message_ids` lists claimant IDs, and the thread body
carries both texts. A byte-identical copy of a message (the same mail
filed twice) shares one claimant ID, as it shared one Message-ID
before. The MCP tools return `claimant_id` beside `message_id`, and
`get_message` accepts either, listing the claimants when a bare
Message-ID names several (see `docs/mcp-tools.md`).

Each indexed message gets one row — its own
subject, `sent_at` (`Date:` header; a missing or unparseable header is
dated at first index and that date is kept when the message is
reprocessed or its thread rebuilt), folder, `in_reply_to` /
references, attachment flag, and its source: `filepath` (the Maildir
locator, kept current across flag renames; when a rename crosses
folders the new `folder` is written in the same transaction, so a failed
update rolls back whole and the Maildir walk re-indexes the file) plus
`size_bytes` and
`content_hash` (SHA-256 of the raw `.eml`). The MCP server returns
these as `source_file` on every message, evidence chunk, and attachment
result (see `docs/mcp-tools.md`). `message_participants`
normalizes From / To / Cc into one row per (message, role, address),
with `address` canonical and lowercased and the display name kept as
written; malformed entries with no recoverable address are skipped.
An index on `(address, role)` makes "every message from / to X" an
exact indexed lookup — the basis for exhaustive enumeration, as
opposed to relevance search. The MCP server's `query_messages`
enumerates over these tables (count plus keyset pages ordered by
`(sent_at, claimant_id)`), and `find_contact` aggregates
`message_participants` instead of parsing each thread's participant
JSON.

Both are written inside `upsert_thread`'s transaction, after the
message's `message_thread_map` row. `messages` references
`message_thread_map` and `message_participants` references `messages`,
both `ON DELETE CASCADE`, so every existing removal path — reaper,
whole-thread delete, rebuild — cleans them up without separate code.

## Entities

Entity resolution is deterministic: no model suggests or performs a
merge. Each `message_participants` row also writes, in the same
transaction:

- a **person** entity per canonical address (`entity_id`
  `person:<address>`), with every display name seen for that address
  recorded in `entity_aliases`. Two different addresses are never
  merged, however similar their names: display names are
  sender-controlled.
- an **organization** entity per exact address domain
  (`org:<domain>`), linked from the person through `organization_id`.
  The domain is used as written, with no public-suffix lookup, so
  `mail.example.com` and `example.com` are two organizations rather
  than a guessed merge. Addresses at a short list of free-mail
  providers (`FREE_MAIL_DOMAINS` in `indexer/src/entities.py`) get no
  organization.

IDs are derived from the address and domain, so reprocessing a message
rewrites the same rows. Entity and alias writes are capped at
`MAX_ENTITY_PARTICIPANTS_PER_MESSAGE` (200) distinct addresses per
message, authors first (a repeated address is written once, with the
first display name it carries in that message, and does not count
again), so a crafted header listing thousands of recipients
cannot drive unbounded writes; later participants still get their
`message_participants` rows, just no new entity. The MCP server's
`find_contact` reports each contact's organization.

Known limitation: entities are not pruned when messages are removed,
so an address seen only in reaped mail keeps its `entities` row and
aliases. Every read joins through `message_participants`, which is
pruned, so such an entity never surfaces in results; it only takes
space.

### Source authority

`entities.authority_class` comes only from an operator-written rules
file, `config/authority.toml` (mounted read-only at `/config`; see
`docs/setup.md`), mapping exact addresses and domains (with their
subdomains) to `counsel`, `management`, `vendor`, `government`,
`personal` or `other`. `authority_rule` records the rule that matched
(`address:<pattern>` or `domain:<pattern>`) as provenance; an entity no
rule matches is `unclassified` with no rule. An address rule beats a
domain rule, and the closest listed parent domain wins. No model
classifies anything.

Known limitation: authority reflects the **claimed** From address. The
index does not authenticate senders, so spoofed mail claiming an
address or domain from a classified rule is classified too (see #463).
Treat the class as a description of who the message says it is from,
not proof.

The indexer loads the file once at startup, before opening the
database. An absent file classifies nothing; a file that cannot be
inspected (a dangling symlink, an unreadable parent directory) or is
malformed fails closed, with errors naming positions only (table,
key and entry numbers), never the file's text. Domain rules must be
LDH labels joined by single dots, at most 16 labels and 253
characters, so a wildcard, URL or empty label is rejected rather than
loaded as a rule that can never match. Loading is bounded (1 MiB,
10,000 patterns), and every existing entity is reclassified under the
loaded rules in one transaction, so an edit takes effect at the next
start. Classifying a sender's domain does at most one lookup per label
over at most 16 labels and 253 characters; a longer or deeper
sender-supplied domain is unclassified without any lookup.

Authority is metadata, never a ranking weight. The MCP server exposes it
as an `authority_class` filter on `search_emails` and `query_messages`
(a message matches when one of its From senders carries the class; a
thread when one of its messages does) and on `find_contact` results.
Filtering removes results without reordering or rescoring the rest.

## Attachment Indexing

Email attachments flow through the same chunker and embedder pipeline
as message bodies. Two extra tables sit alongside `message_chunks`:

| Table | Keyed by | Purpose |
|---|---|---|
| `attachments` | attachment_occurrence_id | Per-occurrence row capturing filename + MIME + size as it appeared on a specific email. The occurrence id includes the message, payload hash, filename, and attachment slot so duplicate same-payload files in one email are still represented. |
| `attachment_extractions` | attachment_id (= sha256 of payload) | Per-content-hash cache of extracted text + status. The expensive work (Tesseract OCR, pypdf parse, DOCX walk) runs at most once per unique payload, including within one indexing batch, where results not yet committed are shared by content hash. Non-success rows are also honored: `empty` / `too_large` short-circuit unconditionally; `unsupported` short-circuits while it also holds for the occurrence at hand (an "OCR disabled" row, for an occurrence that needs OCR — an image, or a PDF whose row the PDF extractor wrote because it found no digital text layer — until OCR is turned on; any other until the occurrence's MIME type or filename selects an extractor, since the same bytes can arrive as `.bin` first and `.txt` later); `failed` short-circuits within a 7-day retry window so a chronic failure stops re-running on every reappearance, but a real fix landed via dependency upgrade can pick the payload up later. The `extractor` column carries a version (`docx@3`); a row written by an older version of a fixed extractor (`extractors.EXTRACTOR_VERSIONS`) is refreshed by re-running that extractor from whichever occurrence of the bytes arrives (whatever its own filename or MIME type), and the indexer re-queues every message carrying those bytes once at startup so their chunks are rebuilt, except dead-lettered messages, which keep their stale chunks until `make requeue-dead` rescues them. Rows from a newer version (after a rollback) are kept. A stale row an OCR extractor wrote (`image-ocr`, `pdf-ocr`) is kept and served while `INDEXER_OCR_ENABLED=false`, since a refresh could only replace its text with "OCR disabled"; it is refreshed once OCR is on. Likewise, once OCR is on, the startup sweep re-queues each message whose occurrence of bytes cached "OCR disabled" would now be OCR'd; an occurrence that selects no extractor (`.bin`) is not re-queued, since its reprocess would serve the same row. |

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
(default). The PDF extractor first reads the digital text layer via
`pypdf`, page by page; each page whose text is below a small
minimum-character threshold is rendered via Poppler (`pdf2image`) and
OCR'd via `pytesseract`, so a PDF mixing digital and scanned pages
OCRs only the scanned ones. If OCR fails on a PDF that has usable
digital text, that text is kept; with no usable digital text the
extraction is recorded as `failed`. A multipage TIFF (a scanned
invoice or fax) is OCR'd page by page; other image formats' extra
frames are animation and only the first is read.
`INDEXER_OCR_MAX_PAGES` (default 20) caps the pages OCR'd per
document of either kind.

With OCR off, images, and PDFs whose whole text layer is below the
threshold, are recorded as "OCR disabled". When the indexer starts
with OCR on, it re-queues once each message carrying such an
attachment as an image or a PDF, except dead-lettered messages. A PDF
with usable digital text is indexed from it while OCR is off, and its
scanned pages are not re-read when OCR is turned on later.

### Cost bounds

| Knob | Default | Purpose |
|---|---|---|
| `INDEXER_ATTACHMENT_EXTRACTION_ENABLED` | `true` | Master switch — turns the whole pipeline off if needed |
| `INDEXER_OCR_ENABLED` | `true` | Disables all OCR paths (image + PDF fallback) |
| `INDEXER_ATTACHMENT_MAX_BYTES` | `10000000` (10 MB) | Skip very large attachments — bounds CPU/memory for huge zips |
| `INDEXER_OCR_MAX_PAGES` | `20` | Cap pages OCR'd per PDF or multipage TIFF |
| `INDEXER_OCR_TIMEOUT_SECONDS` | `60` | Per-page Tesseract timeout — bounds runaway OCR on a crafted high-noise image — and the deadline for rendering a scanned PDF's pages with Poppler. Set `0` to disable both. |
| `INDEXER_PDF_MAX_DIGITAL_PAGES` | `500` | Cap pages walked by the digital pypdf path — protects against text-only PDFs with thousands of pages. Set `0` to disable. |
| `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` | `2000000` (~500 pages) | Truncate extracted text before persisting in `attachment_extractions`. Bounds SQLite row size for very long OCR'd PDFs. Set to `0` to disable. The XLSX extractor also stops at 10,000,000 characters of its own, whatever this is set to, so shared strings repeated across many cells cannot expand without limit (#294). |

### Cascade on message removal

When a message is reaped, `_delete_attachments_for_message` drops its
`attachments` rows and FTS shadows; the `_delete_chunks_for_message`
cascade also drops the message's attachment chunks (they share the
`claimant_id` key). Cached extractions in `attachment_extractions` are
**deliberately preserved** — another message may still reference the
same content_hash, and even when nothing does today the cached
extraction means a future re-arrival skips the OCR cost.

## Deletion Reconciliation (mirror by default)

`mbsync` is configured `Sync Pull` + `Expunge None`, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP `\Deleted` (Maildir `T`)
flag. Without reconciliation, the local SQLite index keeps those messages
forever.

The indexer runs a reconciler that handles this in two phases. It is on
by default (mirror mode: upstream deletions leave the local index);
`INDEXER_DELETION_ENABLED=false` turns it off (archive mode: the index is
append-only and keeps deleted mail). An unrecognized value fails indexer
startup rather than picking a mode.

1. **Tombstone** — a startup sweep plus a live `on_moved` watchdog handler
   record every `T`-flagged file in a `pending_deletions` table. No primary
   data is mutated at tombstone time, so the soft-delete is fully reversible
   if mbsync un-flags the file on a later pull. A tombstone is recorded
   only for the path `message_thread_map` currently holds for the
   message, so a sweep that resolved a path before the watcher restored
   or moved the file cannot leave a stale tombstone for the reaper.
2. **Reap** — after a configurable grace window
   (`INDEXER_DELETION_GRACE_DAYS`, default 7 days) the reaper first checks
   the file each tombstoned message maps to now: if it exists and is not
   trashed, the tombstone is stale (the file moved away and back during a
   sweep) and is cleared instead of reaped. This check runs before the
   mass-delete brake below counts the batch, so stale tombstones cannot
   hold the brake shut. Otherwise it removes the
   reaped message's rows from `message_thread_map` / `indexed_files` and
   any indexing job still queued for its file (in the same transaction,
   which first re-checks that every message it removes is still
   tombstoned past the grace window, so a restore — or a restore and a
   fresh tombstone — after the reaper read its tombstones is left for a
   later pass), and
   either rebuilds the parent thread from the surviving messages on disk
   (re-parsed, re-embedded) or deletes the thread entirely when nothing
   remains. Embedding-endpoint failures during rebuild (operator-supplied
   `EMBED_BASE_URL`) cause the reaper to back off and retry on the next
   pass.

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
on the Maildir itself. Because reaped `.eml` files normally remain on disk,
the indexer's enqueue paths skip `T`-flagged files while reconciliation is
enabled so a reaped message is never re-indexed (see *Ingestion
completeness*).

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
inline — they `enqueue` each filepath and return immediately. The main
thread drains the queue via the **two-phase batched** path
(`_drain_queue_batched`); the initial scan runs to empty
(`max_passes=None`, `batch_size=INITIAL_INDEX_BATCH_SIZE`), while the
steady-state main loop runs one bounded pass per tick
(`max_passes=1`, `batch_size=INDEXER_STEADY_STATE_BATCH_SIZE`) so the
reconciler, periodic recovery sweep, periodic WAL checkpoint, and
health-file refresh all interleave cleanly with indexing work. Both
paths share the Phase 1 / Phase 2 implementation so seed-vector
selection and failure isolation behave identically:

- **Phase 1 (per message)**: parse → thread → `upsert_thread` with a
  seed thread vector chosen by a three-case priority chain:
  1. Thread has chunk vectors → `mean(chunks)`. Canonical seed for
     already-indexed threads with content.
  2. No chunks but a prior `threads_vec` row exists with a non-zero
     embedding → preserve that. Covers chunkless threads whose vector
     came from Phase 2c's subject-fallback path (an earlier blank-body
     message in the same thread).
  3. Neither → placeholder zero. Truly new threads, or post-crash
     recovery on a thread that already had a zero row.

  Cases 1 and 2 are what make a Phase 2 failure non-corrupting: if the
  bulk embed fails on a new sibling message, the parent thread retains
  its prior valid vector — chunk-derived or subject-fallback — instead
  of being clobbered to zero. Threading state is durable before the
  next message in the batch's threader runs, so a reply B that arrives
  in the same batch as its parent A correctly threads into A's
  thread — not a sibling.
- **Phase 2a (per message, no DB)**: chunk body + extract attachments
  WITHOUT embedding. New chunks accumulate into a flat batch-wide
  texts list with offsets recorded on each per-message state object.
- **Phase 2b (batched)**: a single `embedder.embed_batch` over every
  new chunk's text across the batch. ~25k single-message HTTP
  round-trips collapse to ~500 multi-message ones at the default
  `INITIAL_INDEX_BATCH_SIZE=50`, plus the embedder client's own
  per-call chunking via `EMBED_BATCH_SIZE` (default 64).
- **Phase 2c (per message)**: write chunks/vectors/attachments inside
  one per-message transaction, then replace the Phase 1 seed thread
  vector with the real mean-of-chunks vector (or the subject-fallback
  embed when this message contributes no chunks to a chunk-less
  thread).

The two-phase split is what makes a cloud-embedder reindex tolerable:
~1 hour against a 25k-message mailbox with a remote provider drops to
~5–10 minutes. Steady-state (watchdog) ingestion runs through the same
path with `max_passes=1` and a smaller batch size, so a 1-message
delivery still produces just one HTTP call (no overhead) while a 5+
message burst from an mbsync sync collapses into one bulk embed call
instead of N round-trips.

Failure isolation is preserved across phases:

- Phase 1 error for one message → that message marked failed, the
  rest of the batch continues.
- Phase 2a error (chunk/extract) → marked failed, batch continues.
- Phase 2b (embed) error → a one-string health probe decides whose
  fault it is:
  - **Probe fails** (embedder down, rate-limited, or rejecting the
    key/model): every in-flight message is *deferred* — `attempts`
    unchanged, class `retryable` or `operator_action_required` — and
    a circuit breaker pauses draining (30 s, doubling to 10 min,
    reset on the next successful embed). No outage can dead-letter
    mail. The breaker is shared by the initial drain and the main
    loop. Pausing also skips Phase 1, so new mail is not
    keyword-searchable until the embedder returns.
  - **Probe succeeds**: something in the batch may be bad, so each
    message is re-embedded on its own and good ones are indexed in the
    same pass. A passing probe only describes one tiny request, so
    each individual failure is attributed (`classify_embed_failure`,
    deliberately separate from the HTTP client's retry predicate)
    before any message is charged:
    - transport error, 408, 429 (infrastructure) or 401 / 403 / 404
      (configuration): not the message's fault — it and every
      remaining message are deferred without spending attempts and
      the breaker opens; messages already embedded are still
      committed;
    - 400 / 413 / 422 (the provider refused the request): if the
      request carried several of the message's texts, they are re-sent
      one per request first — a rejected batch only shows the request
      was too big, not that the content is bad (the indexer logs a
      hint to lower `EMBED_BATCH_SIZE`). A rejection of a single text
      is the only terminal case, dead-lettered as
      `permanent_source_failure`;
    - 5xx or anything else (uncertain): re-probe. A failing probe
      means the provider went down (pause as above); a passing probe
      points at this input, which spends one attempt (`mark_failed`),
      so a genuinely poison input still dead-letters eventually
      without stalling the queue behind the breaker.

  Phase 1 commits are idempotent — `upsert_thread` merges existing
  rows — so any later pass re-runs Phase 1 + Phase 2 cleanly.
- Phase 2c (DB write) error for one message → marked failed, others
  succeed.

The queue's `claim_batch(N)` fetches a snapshot of N distinct due rows
in one query so the gather phase cannot re-claim the same row
repeatedly while Phase 2c is deferred.

### Recovery sweep for chunkless zero-vector threads

A recovery sweep (`_recover_zero_vector_threads`) runs at the head
of every `initial_index` call AND periodically from the main loop
on `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS` (default 30 min). It
catches the failure modes the queue retry path can't fix on its own
without breaking the durable queue's bounded-retry contract:

- **Symptom**: Phase 1 commits (thread + `message_thread_map` +
  `indexed_files` rows) landed, but Phase 2c never wrote chunks.
  `is_indexed` returns True so the standard Maildir walk skips the
  file. The thread carries the placeholder zero `threads_vec` row.
- **Detection**: `Database.find_zero_vector_chunkless_thread_filepaths`
  finds threads with no `message_chunks` rows and an all-zero
  `threads_vec` blob, then maps them back to filepaths via
  `message_thread_map`.
- **Repair policy** — uniform across the startup and periodic
  paths, both calling `_recover_zero_vector_threads(...,
  resurrect_dead=False)` (the default). What each queue state
  triggers:
  - **No queue row** (the row was cleaned up out-of-band; rare): the
    file is re-enqueued with reason `recovery` and the next drain
    pass completes Phase 2.
  - **`queued` row** (the durable retry cascade is in flight):
    skipped, because clobbering the row would reset `attempts`
    mid-cascade. The queue itself owns the recovery and the next
    drain claims the row once `next_attempt_at` is due. This is
    also the genuine crash-mid-batch case: a process killed
    between Phase 1 and `mark_failed`/`mark_succeeded` leaves the
    row at `queued`, charged one attempt only if it was the message
    being parsed or extracted when the process died (see below).
  - **`dead` row** (deterministic Phase 2 failure exhausted
    `max_attempts`): skipped — left alone as an operator-visible
    terminal state. Auto-resurrecting would burn embedder load
    against the same poison-pill payload on every container
    restart and contradict `initial_index`'s own `is_dead` skip.
    To rescue a dead-lettered file, an operator confirms the
    underlying cause is fixed and runs `make requeue-dead`
    (optionally `CLASS=...`), which resets dead rows to `queued`
    with a fresh attempt budget.

Healthy chunkless threads (e.g., subject-fallback threads from
blank-body messages) are not touched — the DB query filters out
non-zero `threads_vec` rows.

Each job carries `attempts`, `last_stage`, `last_error`, `last_error_class`
(`retryable` / `permanent_source_failure` / `operator_action_required`), and a
`next_attempt_at` scheduled via exponential backoff
(`base_backoff_seconds × 2^(attempts - 1)`, capped at 6 hours). Outside
the embed stage, `last_error` holds the exception type name alone, since
parser, codec and library messages can quote the mail being indexed. An
`OSError` with an errno keeps the errno and its fixed `os.strerror` text
(not its message or filename), and only `OversizedMessageError` (path
and sizes) and the embedder's fixed-text `EmbedResponseError` keep their
message. The embed stage records `scrub_embed_error` instead: an SDK
status error as its type and status code, a connection or timeout error
and `EmbedResponseError` in full, and anything else as its type name. When
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
them dead until something explicitly resets them. It also skips
files that already have a `queued` row, so a restart cannot reset
an in-flight retry cascade to zero attempts.

A crash or hang never reaches `mark_failed`, so the message that
caused it would otherwise be claimed again at the same attempt count
after every restart, forever. The one message whose parse (Phase 1)
or chunk / attachment extraction (Phase 2a) step is running is
therefore charged one attempt while the step runs
(`IndexingQueue.begin_attempt`) and marked `last_stage = 'interrupted'`,
both refunded when the step returns or its outcome is recorded. A
process that dies mid-step leaves only that message charged and marked
— never its batchmates — so an ordinary restart costs at most one
attempt. An out-of-memory kill can come from the whole batch's
footprint rather than the message it landed on, and a restart replays
the same batch, so a claimed batch containing a marked row runs that
row alone first; only a message that also dies on its own keeps being
charged, and it is dead-lettered once its attempts are exhausted.

The bulk embed (Phase 2b) and the vector commits (Phase 2c) hold the
whole batch's vectors, so a kill there cannot be pinned on one
message. A batch of several survivors is marked `interrupted` without
charging anyone, which replays each message alone after a restart; a
lone survivor is already running alone, so it stays charged through
both phases until its outcome is recorded.

A hang does not kill the process on its own, and Compose does not
restart an unhealthy container, so a stall guard thread
(`src/stall_guard.py`) exits the indexer when one unit of work — a
message's parse, or one attachment's extraction — has run longer than
`INDEXER_MESSAGE_TIMEOUT_SECONDS` (default 3600, `0` disables). Each
attachment, and each completed embed request while a lone survivor is
watched through the bulk embed, restarts the clock, so a message with many legitimately
slow scanned PDFs (up to ~21 min each at the default OCR limits) is not
cut off. The restart policy brings the indexer back with the attempt
counted.

### Ingestion completeness

The watchdog observer starts **before** the initial drain, so mail
mbsync delivers while a long initial index is running is enqueued
and picked up by the same drain-to-empty loop. Filesystem events are
the low-latency path but not the correctness guarantee: every
`INDEXER_RECOVERY_SWEEP_INTERVAL_SECS` (default 30 min) the main loop
re-walks the Maildir with the same rules as the startup scan
(`_enqueue_unindexed_messages`: skip indexed, dead-lettered, and
already-queued files; enqueue the rest with reason `rescan`). A file
whose event was missed — restart, event coalescing, a delivery
while the observer was not running — is therefore indexed
eventually rather than omitted until the next container restart.

When deletion reconciliation is enabled, every enqueue path — the
startup scan, the periodic rescan, the zero-vector recovery sweep, and
the watchdog's `on_created` / new-delivery `on_moved` branches — skips
`T`-flagged files, and the drain never indexes a claimed job whose file
has been `T`-flagged since it was queued (the reaper owns that message):
a message still in the index keeps its job parked, which the reap
deletes or, if mbsync clears the flag first, the rename moves back to the
live path and makes due at once; a trashed file never indexed has its job dropped. A reaped message's `.eml` stays on disk under the default
`INDEXER_UNLINK_ON_REAP=false` and is no longer indexed or queued, so
treating it as undiscovered mail would resurrect it into search (and
the next sweep would start a fresh grace window). If mbsync later
clears the `T` flag because the message was restored upstream, the
file is live mail again and is re-indexed normally. With
reconciliation disabled (archive mode), the index is append-only and
`T`-flagged files are indexed like any other.

Two stage outcomes short-circuit the retry path entirely:

- `FileNotFoundError` at parse — almost always because mbsync renamed
  the file (added an IMAP flag suffix) between enqueue and read. The
  path is permanently invalid; the renamed file enters the queue under
  its new name via a fresh `IN_MOVED_TO` event. The worker calls
  `mark_skipped(reason="file_missing")` instead of `mark_failed`: row
  deleted, no retry, no dead-letter, and an INFO
  `skipped: <path> reason=file_missing` log line.
- `PermissionError` at parse is deferred (60 s) without spending an
  attempt. mbsync `chmod go+r`s new files only after its whole sync
  finishes, so during a long sync a delivered file stays unreadable to
  the indexer's UID for longer than the retry budget; charging that
  expected handoff dead-lettered valid mail. A job still unreadable a
  day after it was enqueued takes the normal retry path, so a real
  permissions fault still ends in a visible `dead` row.

Three environment variables shape the queue: `INDEXER_MAX_ATTEMPTS`,
`INDEXER_RETRY_BASE_SECONDS`, and `INDEXER_MESSAGE_TIMEOUT_SECONDS`.
None is required — the defaults are
suitable for typical mailboxes, and both are documented in
`docs/troubleshooting.md` for operators who need to tune retry aggressiveness
against an unreliable embed service or a flaky mailbox.

Observability: `queue.stats()` returns `{queued, dead}` counts and is
logged at startup when the queue carries non-zero depth from a prior
run. The main loop's periodic health-file refresh continues
independent of queue depth, so a stuck queue does not mark the
container unhealthy (dead jobs are a data issue, not a liveness
issue).

### Index currency

`get_mailbox_status` answers "is the index current?" from SQLite
alone, because mcp-server mounts neither Maildir nor Bridge. After
each successful sync, once the new files have been made readable,
mbsync writes `.mbsync-last-sync.json` (completion time plus
`SYNC_INTERVAL`) at the Maildir root. It writes a temporary file named
after that sync (`.mbsync-last-sync.<time>.<interval>.tmp`) and
renames it into place. The stamp sits outside every `cur`/`new`
folder, so no Maildir walk or watchdog handler treats it as mail.

A stamp on disk does not prove the indexer has queued that sync's
mail: the watcher may still be behind on its delivery events. So the
indexer **acknowledges** a sync only when every message it delivered
is queued:

- when the watcher handles the stamp's rename. Watchdog dispatches
  events in order, so the sync's delivery events were handled first.
  The sync is read from the temporary file's name, not the stamp's
  content, which a later sync may already have replaced.
- when a Maildir walk (startup or the periodic rescan) finishes: the
  stamp read before the walk is acknowledged.

Acknowledgements only move forward. With every health heartbeat
(per message and per embed batch, at most every 30 s), the indexer
upserts the latest acknowledged sync and its own timestamp into the
one-row `ingestion_state` table. A missed stamp event
reads as a stale sync until the next rescan; a missed delivery event
stays invisible to `current` until the rescan queues it.

## File Identity on `indexed_files`

`indexed_files` carries `size`, `mtime_ns`, and `content_hash`
(SHA-256 over the raw file bytes) captured at
`parse_email` time. `is_indexed` stays filepath-keyed — the hot path
remains an O(1) primary-key lookup — and the new columns are written
alongside. On a flag-only mbsync rename (`msg:2,S` → `msg:2,SR`)
`update_filepath` carries the captured identity forward rather than
clearing it, because the file contents on disk are unchanged. It moves
the file's `indexing_jobs` row in the same transaction, retry or dead
state intact: a file whose Phase 1 committed but whose Phase 2 is still
pending is already indexed, so the rename is not re-enqueued, and a job
left on the old path would be dropped as missing.

The columns exist to let future reconciler passes distinguish a
flag-only rename from a genuine content change, and to spot a "file
vanished from path A but the same `content_hash` reappears at path B"
rename that mbsync performed without emitting an `on_moved` event.
No code reads them yet: a content-hash lookup and its consumers would
land together with such a pass.

Rows for which `stat` / hash capture failed at parse time carry NULL
identity values, which a future content-hash lookup must skip; the
columns are populated lazily on the next reindex of the file.

## Privacy Model

Three layers, each with its own boundary. The README has the operator-facing
walkthrough; the table below is the per-operation reference.

### Storage and processing layer (always local)

| Operation | Local only | Leaves machine |
|---|---|---|
| Email storage | ✅ | Never |
| Vector index | ✅ (SQLite) | Never |
| Keyword search | ✅ (SQLite FTS5) | Never |
| Send/Move/Flag | Disabled by default | Never |

### Embedder, reranker, and inference (operator-supplied)

| Operation | Local only | Leaves machine |
|---|---|---|
| Embedding — `EMBED_BASE_URL` points at a host-side server | ✅ | Never |
| Embedding — `EMBED_BASE_URL` points at a remote provider | Retrieval queries + indexed content | Email body chunks → provider |
| Reranking — `RERANK_BASE_URL` points at a host-side server | ✅ | Never |
| Reranking — `RERANK_BASE_URL` points at a remote provider | Retrieval queries | Retrieved chunks → provider |
| Q&A — `INFERENCE_MODE=openai`, host-side `INFERENCE_BASE_URL` | Retrieval local | Never |
| Q&A — `INFERENCE_MODE=openai`, remote `INFERENCE_BASE_URL` | Retrieval local | Retrieved chunks → OpenAI-compatible provider |
| Q&A — `INFERENCE_MODE=anthropic` (default) | Retrieval local | Retrieved chunks → Anthropic-compatible provider |

> **Note on remote providers.** Pointing any of these at a remote
> provider ships data over the network: every email body chunk
> through the embedder at index time, every search-query string
> through the embedder at retrieval time, and every retrieved chunk
> through the inference and reranker endpoints. This is a deliberate
> departure from a fully-local posture; choose the provider URLs
> accordingly. To keep all retrieval traffic on the box, point each
> URL at a host-side server you install yourself.

### MCP client layer (governed by which client you connect)

When the MCP server is consumed by a cloud-backed client, the tool *return
values* are sent to that client's backend as part of the conversation
context. Tool results often contain email snippets, full thread bodies, or
LLM-generated answers grounded in mail — so the client's backend sees that
content regardless of `INFERENCE_MODE`.

| Client | What sees tool results |
|---|---|
| Claude Desktop | Anthropic (Claude runs in the cloud; tool results go back as conversation context) |
| Local-LLM MCP client | Stays on machine |
| Direct `docker exec` into mcp-server | Stays on machine |

`INFERENCE_MODE` and the MCP client choice are independent boundaries and must
both be set deliberately if "fully local conversations" is a goal.

The MCP server defaults to SSE for existing Claude Desktop compatibility.
Set `MCP_TRANSPORT=streamable-http` for clients that only speak Streamable
HTTP, or `MCP_TRANSPORT=dual` to serve both `/sse` and `/mcp` on the same
localhost-bound port. Every transport, and `/health`, sits behind the same
Host/Origin allowlist. A Streamable HTTP session idle for
`MCP_SESSION_IDLE_TIMEOUT_SECS` (default 1800 s) is ended, so abandoned
sessions do not accumulate.

## Inference Mode Toggle

Set `INFERENCE_MODE` in `.env`:

- `anthropic` (default) — Anthropic-compatible Messages API via
  `INFERENCE_BASE_URL`. Retrieved email chunks are sent to
  that provider.
- `openai` — OpenAI-compatible chat completions via
  `INFERENCE_BASE_URL`. Point this at a remote provider or at
  a host-side server you install yourself; only the latter keeps
  retrieved chunks on your machine.

The toggle applies per-deployment. A per-session toggle is on the roadmap.

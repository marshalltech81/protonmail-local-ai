# Architecture

## Overview

protonmail-local-ai is a containerised, privacy-first AI search and
intelligence layer for ProtonMail. The official Proton Mail Bridge app
runs on the host (see [Bridge](#bridge)) and three containers (mbsync,
indexer, mcp-server) run beside it; storage, sync, and indexing never
leave the host. Embedding and inference are operator-supplied
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
Proton Mail Bridge app (on the host, outside Compose)
  - Decrypts email using your private key
  - Exposes IMAP on the host's loopback (127.0.0.1, port 1143 by default)
  - Login, credentials and updates are managed in the app
        │
        │  IMAP over implicit TLS via host.docker.internal (socat tunnel)
        ▼
mbsync container
  - Polls Bridge IMAP every SYNC_INTERVAL seconds in a bounded retry loop;
    each mbsync run has a deadline (SYNC_DEADLINE_SECONDS, default a
    day) past which it is stopped and counted as a failed sync (#282)
  - Writes Maildir format to maildir-volume
  - Maintains sync state for incremental updates, in each folder's own
    Maildir directory, and writes child folders with a leading dot so no
    folder name can collide with another or with Maildir's own
    directories (#275, #281; see [Maildir layout](#maildir-layout));
    refuses to start on a Maildir synced with the earlier layout
  - Requires Bridge's TLS cert to match BRIDGE_CERT_FINGERPRINT on every
    start, then pins it (SHA-256 fingerprint stored in mbsync-state
    volume); refuses to sync on mismatch unless the operator sets
    BRIDGE_CERT_PIN_ROTATE=true for a legitimate rotation
  - Fails closed if cert extraction or repeated sync attempts fail
  - After each successful sync, writes a last-sync stamp
    (`.mbsync-last-sync.json`) at the Maildir root. A sync whose only
    errors are Proton folders it can no longer open (renamed or deleted
    upstream; the local copy is kept) counts as successful, with a
    warning that gives a count, not folder names (#276). Other isync
    messages that name a folder or a Maildir path are logged with
    `<folder>` or `<path>` in its place (#570)
  - Healthcheck is liveness only: healthy while the sync loop is alive
    (a heartbeat touched around every attempt is fresh, or an mbsync or
    its permission repair walk is running, up to that run's deadline),
    so the indexer and MCP server start during a long first sync;
    freshness comes from the last-sync stamp
  - After every permission repair, whatever the sync's outcome,
    renames an empty `.mbsync-perms-repaired` marker into place at the
    Maildir root; folders a sync attempt creates are watched once the
    indexer handles that marker or the stamp (#516, #524)
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
                                 - pending_deletions, reaped_messages
                                   (reconciler)
                                 - entities, entity_aliases
                                 - vector_generations (embedder
                                   identity record)

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
  - Exposes MCP tools over Streamable HTTP at /mcp on port 3000,
    built on the standalone `fastmcp` package
  - Rejects any HTTP request whose Host is not localhost, 127.0.0.1,
    [::1] or mcp-server (any port) with 421, and a browser Origin
    outside the same names over http with 403, before it reaches the
    transport (DNS-rebinding defense)
  - Requires a static bearer token on /mcp (401 otherwise, before a
    session is created); see Endpoint authentication below
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
        │  Streamable HTTP (127.0.0.1:3000/mcp)
        ▼
MCP client (host machine; Claude Desktop via the repo's stdio adapter)
  - Calls MCP tools via natural language
  - Receives structured responses
```

## Container Responsibilities

| Container / process | Reads from | Writes to | Exposes |
|---|---|---|---|
| Proton Mail Bridge app (on the host, [Bridge](#bridge)) | ProtonMail Cloud | its own data on the host | IMAP on the host's `127.0.0.1` |
| `mbsync` | Bridge IMAP | `maildir-volume` | nothing |
| `indexer` | `maildir-volume`, embedder | `sqlite-volume` | nothing |
| `mcp-server` | `sqlite-volume`, embedder, inference provider, optional reranker | nothing | HTTP 3000 (localhost only) |
| Embedder (operator-supplied) | indexer + mcp-server requests | depends on provider | OpenAI-compatible `/v1/embeddings` at `EMBED_BASE_URL`. A host-side server reachable via `host.docker.internal`, or a remote provider. |
| Inference (operator-supplied) | mcp-server intelligence-tool requests | depends on provider | Anthropic Messages API or OpenAI `/v1/chat/completions` selected by `INFERENCE_MODE`. |
| Reranker (operator-supplied, optional) | mcp-server hybrid-search requests | Cohere | Cohere rerank API via the official `cohere` SDK when `RERANK_MODE=cohere`. `RERANK_BASE_URL` required (`default` = SDK default). |

## Docker Volumes

| Volume | Contents | Back up? |
|---|---|---|
| `maildir-volume` | Raw email in Maildir format | Optional — mbsync can re-sync |
| `mbsync-state` | mbsync's pinned Bridge certificate fingerprint | No — re-pinned from `BRIDGE_CERT_FINGERPRINT` |
| `sqlite-volume` | SQLite index (FTS5 + vectors) | Optional — indexer can rebuild |

### Maildir layout

mbsync writes one Maildir per Proton folder (`mbsync/mbsyncrc.template`):

- **Folders:** `SubFolders Legacy`. A top-level folder (`INBOX`, `Sent`,
  `Trash`, `Spam`, `Folders`, ...: Bridge's fixed names) is the directory
  of that name under `/maildir`; each child is its parent's directory plus
  `/.` and its name. `Folders/Clients/cur` is
  `/maildir/Folders/.Clients/.cur`, so a child can never land on its
  parent's own `cur`, `new` or `tmp` (#281), and a name keeps its dots
  (`SubFolders Maildir++` refuses them). isync 1.5.1 decodes the
  modified UTF-7 names Bridge lists into UTF-8 (`Folders/Caf&AOk-` is
  `Folders/Café`, `Folders/A&-B` is `Folders/A&B`), and the decoded names
  become directory names as they are. isync 1.4.4, in the image before,
  kept the modified UTF-7 names undecoded, so an existing install must
  migrate such folders before upgrading (`docs/setup.md`, "Upgrading to
  the isync 1.5.1 mbsync image").
- **Sync state:** `SyncState *`. Each folder's UIDVALIDITY and UIDs live
  in `.mbsyncstate` (with `.journal`, `.new` and `.lock` while a sync runs)
  in the folder's own directory, next to isync's `.uidvalidity`, so no two
  folders can share a state file (#275).
- **Virtual folders:** `All Mail`, `Labels/*` and the top-level `Starred`
  are views in Proton, not places: Bridge lists each message there as
  well as in its real folder. The channel's `Patterns` leave them out, so
  each message is synced and indexed once. A star still arrives, as the
  Maildir `F` flag on the real copy (`flagged` in the MCP tools); a label
  does not. A custom folder named `Starred` is `Folders/Starred` and
  syncs.
- **Names isync needs for itself:** a child folder named `uidvalidity`,
  `isyncuidmap.db`, `mbsyncstate`, `mbsyncstate.journal`, `mbsyncstate.new`
  or `mbsyncstate.lock` would be one of those files, so the channel's
  `Patterns` leave it, and everything below it, out. It is not synced and
  nothing reports it.
- **Indexer:** a message's folder is the path below `/maildir` to the
  directory holding its `cur`/`new`, with the one leading dot of every
  component after the first removed (`indexer/src/parser.py`
  `_derive_folder`), so tools report `Folders/Clients/cur`. The isync
  state files sit outside every `cur`/`new` and are never read as mail.
- **Earlier layout:** a Maildir synced before this layout (state files at
  the root, children without the dot) is refused at mbsync's start,
  before it connects to Bridge; see
  [troubleshooting](troubleshooting.md#mbsync-refuses-an-earlier-maildir-layout).

`mbsync/tests/layout_check.sh` (`make test-mbsync-layout`, run in CI)
syncs these cases with the shipped image's isync and synthetic Maildir
stores.

Container logs are not in a volume. Every service uses the `json-file`
driver capped at three 10 MiB files (the `x-logging` block in
`docker-compose.yml`), so a long backfill cannot grow a log without
limit on the host.

## Networking

The stack uses two isolated bridge networks:

- `bridge-net` for `mbsync` alone; it reaches the Bridge app on the
  host through `host.docker.internal`
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

## Health and Readiness Signals

"Bridge is healthy" does not mean "mail is syncing". The path from
Proton to a searchable index passes through six states, each reported
by a different signal, and each later state depends on the earlier
ones (#274):

| State | Meaning | Reported by | Not proven by it |
| --- | --- | --- | --- |
| Bridge listening | Something accepts TCP on Bridge's IMAP port | mbsync's `Bridge IMAP port is reachable` line; it covers only mbsync's startup (the Bridge app runs outside Compose, so there is no Bridge health check) | TLS, a logged-in account, any sync |
| TLS handshake OK | Bridge completes implicit TLS with a certificate that matches mbsync's pin | mbsync startup: certificate extraction, `BRIDGE_CERT_FINGERPRINT` and the pin. A failure stops mbsync with a named error before any credential is sent. mbsync's health also requires the extracted certificate, so a healthy mbsync passed this on its current start | That isync accepts the certificate: its own validity and host name checks run on each sync, so an expired certificate that still matches the pin shows as failed syncs. A logged-in account, any sync |
| Bridge authenticated | An account is logged in to Bridge and accepts mbsync's `LOGIN` | No dedicated signal. Bridge listens, completes TLS and greets with no account logged in. The first proof is a successful sync; a rejected login is a failed sync in mbsync's log | — |
| mbsync syncing | mbsync's sync loop is alive | mbsync container health (liveness: a heartbeat touched around every attempt, or a sync or its permission repair running, up to the run's deadline) | That any sync succeeded: the loop is healthy between failed attempts until five consecutive failures exit it and Docker restarts it |
| Last successful sync | mbsync completed a sync | The success stamp `.mbsync-last-sync.json` at the Maildir root, written by mbsync. `get_mailbox_status` cannot read Maildir; its `last_sync_at` (and the reason `no successful mail sync has been recorded` or `last successful mail sync was ... ago`) is the last sync the indexer has acknowledged, after queuing that sync's mail, so it can lag the stamp | The stamp alone: that the indexer has read it. `last_sync_at`: that the queued mail is indexed yet |
| Index current | The indexer has acknowledged a recent sync and has no pending or retrying jobs | `get_mailbox_status` `current` and its reasons (see [Index currency](#index-currency)); `make status` prints the same fields | That every message is indexed: dead-lettered jobs (the `dead` count) do not affect `current`, and their messages may be missing from search until `make requeue-dead`. Nor mail that reached Proton after the last sync |

mbsync's own startup checks TLS and the pin with an error that names
the cause, and its sync results and stamp report the rest. No
healthcheck carries credentials. Symptom-to-layer diagnostics are in
[troubleshooting.md](troubleshooting.md#which-layer-is-failing).

## Bridge

mbsync syncs from the official Proton Mail Bridge app running on the
host (#497), over implicit TLS (RFC 8314, Bridge's "SSL" IMAP mode,
`TLSType IMAPS`; #638): the TLS handshake is the first thing on the
connection, so there is no plaintext phase in which a STARTTLS offer
could be stripped or a command or response injected before encryption.
The operator sets the app's IMAP connection mode to SSL. Certificate
extraction (`openssl s_client` without `-starttls`) and isync both
speak only implicit TLS, with no fallback: a Bridge still serving
STARTTLS greets in plaintext, the handshake fails, and mbsync stops at
startup with `Bridge is not serving implicit TLS` before any credential
is sent.

mbsync reaches the app at
`host.docker.internal:${BRIDGE_IMAP_PORT:-1143}`, which OrbStack and
Docker Desktop forward to the app on the host's loopback interface.
mbsync sits alone on `bridge-net` with its usual hardening; there is no
Bridge service in Compose for it to wait on, so its bounded IMAP wait
covers an app that is not running, and the indexer waits for mbsync's
health check. No port is published and no host networking is used.
Login and updates happen in the app. On Linux, `host.docker.internal`
(`host-gateway`) reaches the docker bridge address rather than the host
loopback the app binds, so this setup is not supported there (see
[setup.md](setup.md#platform-support)).

The app's certificate is upstream Bridge's: self-signed, `CA:TRUE`,
common name and only subject alternative name `127.0.0.1`. isync 1.5.1
(the version in the mbsync image, Debian trixie; 1.4.4 behaves the same)
loads `CertificateFile` like this
(`src/socket.c`): a non-CA certificate in the file is trusted as the
exact server certificate, whatever its name, but a CA certificate goes
into the verification store, after which the chain must verify and the
host name in `Host` must match a DNS subject alternative name or the
common name. Bridge's certificate is a CA certificate, so with
`Host host.docker.internal` the connection is refused with `certificate
owner does not match hostname`. Overriding `localhost` with
`extra_hosts` would not help either: the certificate has no `localhost`
name, and only the literal `127.0.0.1` matches.

`docker-compose.yml` therefore sets `BRIDGE_CERT_HOST=127.0.0.1`, and the
entrypoint renders

```text
Host 127.0.0.1
Tunnel "exec socat - TCP:host.docker.internal:<port>"
TLSType IMAPS
CertificateFile /tmp/mbsync/bridge-cert.pem
```

With `Tunnel`, isync runs the command instead of opening a socket to
`Host`, and keeps `Host` only for the certificate check. Implicit TLS
and verification run end to end between mbsync and the app; `socat` only
relays bytes. (`nc` cannot be the relay: isync waits for the server to
close the connection after `LOGOUT`, and `nc` does not pass that close
on unless Bridge sends a TLS close_notify.) The trust anchors are
unchanged: `CertificateFile` holds only the certificate the entrypoint
extracted and checked against the persistent pin (isync also loads the
system CA store, and public CAs do not issue certificates
for `127.0.0.1`), so a different
certificate at that address is refused twice, by the pin at startup and
by isync's chain check on every sync. `mbsync/tests/tls_check.sh` (`make
test-mbsync-tls`, run in CI) exercises both with a synthetic server
whose certificate has this shape and which speaks implicit TLS, along
with recovery from a Bridge that is down at startup and the refusal of
a STARTTLS server without sending credentials.

The first certificate is never trusted on first use. The app listens on
an unprivileged port on the host's loopback, which another local account
can hold while the app is not running, so trust on first use would pin
that account's certificate and send it the Bridge password. The
entrypoint therefore requires `BRIDGE_CERT_FINGERPRINT`, taken from the
app on the host, and refuses any
other certificate on every start, before the pin is consulted (a
rotation accepts only that certificate) and before mbsync logs in. An
unset `BRIDGE_CERT_FINGERPRINT` stops mbsync at startup, before it waits
for the app.

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

#### Embedder identity record

The index records which embedder built it, so a changed embedder is
caught at startup instead of mixing incomparable vectors into search.
On a fresh index the indexer writes one `vector_generations` row,
status `active`: `EMBED_MODE`, the resolved endpoint (the SDK's
`base_url` after construction, with userinfo, query and fragment
dropped), `EMBED_MODEL`, the vector dimensions, and a **calibration
vector** — the embedding of a fixed synthetic text — with that text's
SHA-256. The columns for revision, tokenizer, context window, chunk
configuration hash and label stay NULL: the OpenAI-compatible API
exposes none of them. This is the first slice of the `vector_generations`
registry in PLAN.md Phase 2; there is no generation lifecycle yet, so
the table holds exactly one row.

On every start both services compare their embedder against the row
(`indexer/src/embed_identity.py`, `mcp-server/src/lib/embed_identity.py`):
mode, model, endpoint and dimensions must match, and the calibration
text is re-embedded and must lie within cosine distance 0.01 of the
stored vector. The vector check catches what the fields cannot: a
host-side server that reloaded a different model of the same dimension
under the same name, or a provider alias that moved. Re-embedding one
text on an unchanged deployment differs by float noise only (zero for a
deterministic server, well under 1e-3 for GPU-batched providers), while
unrelated models sit near distance 1, so 0.01 leaves a wide margin
both ways; a re-quantization of the same model can fall either side of
it. A mismatch fails startup closed with a fixed message naming the
differing fields (see docs/troubleshooting.md, "Embedder identity
mismatch"); the message never quotes a provider response.

The indexer writes the row; mcp-server only reads it. The indexer's
calibration vector is also its startup width check (#841): a vector
that is not 4096 wide exits with "Embedder produced N-dim vectors"
before the row is read or written, so a fresh index never records an
embedder whose vectors it cannot store. A failed calibration request
(the indexer's runs right after `wait_for_ready`, with the client's
usual retries) exits the service with the scrubbed error and the
restart policy tries again.
mcp-server exits the same way while the indexer has not yet recorded
the row (it does so once its embedder answers); its calibration request
is bounded as a whole by `EMBED_TIMEOUT_SECS`. While mcp-server cannot
verify its embedder, the SQLite-only tools are down with it (#661 tracks
keeping them up). The checks run at
startup only; periodic re-checks are tracked separately. An index that
holds messages but no row, or predates the table, fails closed with
rebuild instructions, since nothing says which embedder wrote it.

Every enabled layer must name its endpoint: `{LAYER}_BASE_URL` is a
URL, or `default` for the SDK's documented default endpoint. An empty
value fails startup in `validate-env.sh`, the indexer and mcp-server
before any provider client is built (owner decision 2026-10-05, #750):
the request body, with mail text, reaches a provider before it checks
the API key, so a key is not a choice of provider. The rule is defined
once in `AGENTS.md` (Architecture Summary).

For inference, `INFERENCE_MODE=anthropic` uses the official
`anthropic` SDK against the Messages API (`INFERENCE_BASE_URL=default`
for the SDK default). `INFERENCE_MODE=openai` uses the official
`openai` SDK against any OpenAI-compatible chat-completions endpoint at
`INFERENCE_BASE_URL`. `INFERENCE_MODE=none` (the default) skips
registration of the intelligence tools and sends nothing to an
inference provider.
Experimental tools (currently `brief_issue` and `check_conclusion`) are
registered only when `MCP_EXPERIMENTAL_TOOLS=true` and inference is
enabled; they send the same kind of retrieved excerpts to the inference
endpoint and store nothing (see `docs/mcp-tools.md`, Experimental tools).

Reranking is opt-in via `RERANK_MODE`. `RERANK_MODE=cohere` uses
the official `cohere` SDK against the Cohere rerank API
(`RERANK_BASE_URL=default` for the SDK default). `RERANK_MODE=none`
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
    ├─ keyword slot: the best thread_fts hit is moved up to rank 3 if it
    │   fused lower
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

**Keyword slot (#701).** RRF with k=60 scores adjacent ranks almost
alike, so a thread that only `thread_fts` matches strongly (a person
named only in the headers, a topic named only in the subject) fuses
below threads that match weakly in several lanes, and no lane weight
short of keyword-only ordering changes that. So the best `thread_fts`
hit is guaranteed a place in the top three: if it fused lower, it is
moved up to rank 3 and the rest keep their order. This is applied to
the keyword list before it is cut to its fetch size (the list
`mode=keyword` returns and the outer fusion reads), and again after the
post-fusion filters in keyword and hybrid mode, where the hit is the
best one the filters left. A sender, participant or authority filter
runs after the keyword list is cut, so with one of them the cut extends
through the last `thread_fts` hit, every result at its own fused
position (the outer fusion credits each by that position). It runs
before the rerank window is cut, so a reranker sees the hit and decides
its final position. The slot never widens that window, since every
candidate in it is sent to the rerank provider: with
`RERANK_CANDIDATES` and `limit` both below 3, the window can cut the
promoted hit before the reranker sees it. The moved
thread keeps its fused score and carries `keyword_slot` in its lane
provenance. Only one thread is moved per query; there is no strength
test, so a common-word query also gets its top thread keyword hit in
the top three.

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

Each candidate is sent to the reranker as `Subject: <thread subject>`,
then one `Reply subject: <subject>` line per distinct message subject
that differs from the thread's after reply-prefix normalization (a
reply that changed the subject), then its best evidence chunk or, without
evidence, its snippet. The added subjects are read from `messages`
(the first 50 per thread, oldest first) and capped at 5 subjects and
500 characters per candidate. With `RERANK_MODE=none` none of this runs.

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
   outgoing one, so two vendors' "Invoice" mails would otherwise merge.
   Subjects are compared as stored, cut to `SUBJECT_MAX_CHARS` (2,000)
   characters (#541): two subjects that agree on their first 2,000
   characters match, and a `Re:` reply to a longer subject does not.
   Only crafted mail has such subjects
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
`segment_for_embedding` (`indexer/src/quoting.py`), which keeps a
message's own text as its `body` and leaves quotes, signatures and
forwarded text out of it, so chunk vectors track substantive content of
each reply rather than accumulated quoted history. Stripping is
intentionally conservative: quoted text is still searchable through FTS,
and a message with no body text falls back to its whole original body
rather than an empty embedding input.

**Chunk kinds (#646).** Segmentation happens before chunking, so a
chunk never spans kinds: `segment_for_embedding` (same module) splits
the body into runs of one kind and `chunker.chunk_segments` chunks each
run on its own, carrying no overlap across a run boundary.
`message_chunks.kind` stores the kind, from a closed set checked by the
chunker and by a `CHECK` constraint:

| Kind | Text |
|---|---|
| `body` | the message's own text: every other line before the first marker, with `>` and reply-header lines left out |
| `quote` | `>` lines, reply-header lines, and everything from an Outlook reply block or `-----Original Message-----` on |
| `signature` | from the RFC 3676 `-- ` delimiter on |
| `forwarded` | from a forward preamble (`---------- Forwarded message ---------`, `Begin forwarded message:`) on |
| `calendar` | reserved: nothing produces it yet, since a `text/calendar` part inside a multipart is not body text and has no attachment extractor |
| `attachment` | text extracted from an attachment (exactly the rows with `attachment_id` set) |

The kinds come from the line rules above; there is no new parsing.
What is chunked is unchanged: a message with body text is chunked as
one `body` run, its quotes, signature and forwarded text left out as
before. Only a message with no body text
(the fallback case above) is chunked as its non-body runs, each with
its kind, instead of as one undifferentiated body; its two-line wrapped
reply headers are dropped there as they are from body text. Such a
message is chunked as at most 32 runs, after which the remaining runs
are joined per kind, so a body that alternates kinds line by line
cannot turn into one chunk per line. Chunk offsets index the
normalized runs joined by a blank line. Retrieval does not yet use the
kind; `get_evidence` reports it on each passage.

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
- Semantic: the embedding input of every message's first body chunk,
  renamed reply or not, is prefixed with `Subject: <its subject>`
  (#687), so a topic named only in a subject reaches the chunk vector
  and the thread vector. The prefix is cut so the input stays within
  `INDEXER_CHUNK_MAX_TOKENS` and dropped when the chunk alone is at that
  ceiling, the subject is blank, or it is the parser's `(no subject)`
  placeholder for a missing header (a literal `(no subject)` or a
  reply to one counts the same). The stored chunk text, offsets and
  chunk ID stay body-only, since chunks are the authoritative body store
  (`get_message`, `query_messages(text=...)`). Because the chunk ID does
  not cover the embedding input, an index built before #687 keeps its
  old vectors until it is rebuilt from Maildir.

A message with no body text (for example attachment-only) has no body
chunk to carry the prefix, so its subject is keyword-searchable only,
with no semantic representation unless the thread has no chunks at all
and falls back to a subject-only vector.

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
(`MAX_BODY_TEXT_PARTS`) are decoded; later ones are left out. The
walk over a message's parts stops after 10,000 parts
(`MAX_WALKED_PARTS`, the parts inside attachments included); later
parts add no body text and no attachments.

A query like "what did my landlord say about the heating?" returns the
full landlord thread (via the coarse lanes) and surfaces the specific
chunk where the heating discussion appears (via the chunk lane). The
intelligence tools (`ask_mailbox`, `extract_from_emails`) feed those
matched chunks to the LLM with `[chunk N chars X-Y]` provenance
headers rather than the truncated accumulated body. `ask_mailbox`
labels each passage (`E1`, `E2` ...) with its message's claimant ID,
sender and sent date, and checks the labels its answer cites, that
each statement cites a passage or is marked unsupported or uncertain,
and that its quotes appear in the indexed text of the passages cited.
`summarize_thread` applies the same check to its summary, and
`extract_from_emails` checks the labels each record field cites and
looks for its text values in the cited passages (see
`docs/mcp-tools.md`).

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

**Parser seam.** `indexer/src/parser.py` parses in two halves (#1077).
`parse_email_bytes(raw, source)` turns a message's RFC 822 bytes into a
`Message` and reads nothing from disk; `parse_email(path)`, the Maildir
adapter, reads the file under the size cap and builds the
`SourceMetadata` it hands over: the folder, the Maildir flags, the
file's size and mtime, and its path, which only names the message in
log lines and the `filepath` record. Everything below the parser
(threading, chunking, extraction, the database writer) sees the
`Message`, so another source (an mbox import, say) would plug in at
`parse_email_bytes` with its own adapter.

**Claimant IDs.** A Message-ID is set by the sender, so two different
files can claim the same one, by accident or to overwrite another
message's evidence. Every per-message row is therefore keyed by a
claimant ID rather than the bare Message-ID: the Message-ID plus `#`
and the first sixteen hex digits of the SHA-256 of the file's raw bytes
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

**Message-ID length.** A Message-ID is at most 998 characters
(`parser.MESSAGE_ID_MAX_CHARS`, the RFC 5322 line limit), counted as
stored: after surrounding whitespace, header folding and the angle
brackets are removed, so `<` plus 998 characters plus `>` is accepted.
A message whose own Message-ID is longer is unindexable, like one with
none: it is dead-lettered at the parse stage with the fixed text
`unindexable: no Message-ID or one over 998 characters`, and the ID
itself never reaches the log or `last_error`. A new thread's ID is its
root message's Message-ID, so this also bounds thread IDs. A longer
`In-Reply-To` or `References` entry is dropped from the message (it
could never match an indexed Message-ID), so threading uses the rest
and the stored reply fields stay bounded; each drop is counted as a
parser cap (`in_reply_to_length` / `references_length`, like the
subject cut as `subject_length`; see `docs/troubleshooting.md`,
#902). This assumes no indexed
message has a longer ID: an index built before the limit (none is
deployed) is rebuilt from Maildir, as for any pre-deployment change,
since a reply's dropped reference to an older over-long ID could no
longer find that message's thread.

Each indexed message gets one row — its own
subject (cut to `SUBJECT_MAX_CHARS`, 2,000 decoded characters, at parse
time, #541), `sent_at` (`Date:` header; a missing or unparseable header is
dated at first index and that date is kept when the message is
reprocessed or its thread rebuilt), `occurred_at` (the top `Received:`
header's date, or NULL; see Message time), folder, `in_reply_to` /
references, attachment flag, read state, and its source: `filepath` (the Maildir
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
Address headers are unfolded (RFC 5322 §2.2.3: a line break followed by
a space or tab is removed, the whitespace kept) before they are parsed,
and the Content-Disposition / Content-Type headers an attachment
filename is read from are unfolded the same way before RFC 2231
decoding, so a header folded inside a quoted name or a `filename` /
`name` parameter leaves no line break in the stored value, while a line
break the sender percent-encodes in the filename is kept (#688). An index built before this keeps the
old values until the affected messages are re-indexed.
RFC 2231 decoding leaves RFC 2047 encoded-words (`=?utf-8?q?...?=`) in
a filename untouched, and some clients send a long non-ASCII name as
several of them; the parser then decodes those the same way as Subject
(#924). In Subject, the From fallback and filenames, an encoded-word
whose charset label the codec rejects (unknown, `idna`, a NUL in the
label) is decoded as UTF-8 with replacement characters rather than
failing the message, with one rate-limited WARNING per word naming the
exception type (#942). A value whose encoded-words still do not
decode is kept as sent, with
one rate-limited WARNING naming the exception type, and a value that
decodes to nothing is kept as sent too, so the part stays an
attachment. Filenames stored before this are corrected only when their
message is re-indexed.

**Read state.** `seen`, `flagged` and `replied` are the `S`, `F` and
`R` flags in `filepath`'s `:2,<flags>` suffix (`maildir.message_state`,
built on the same `parse_flags` as the `T` trash check; other letters
are ignored, and a file without the suffix is unread). mbsync is
pull-only, but it mirrors a read, star or reply made in Proton by
renaming the file (`:2,` → `:2,S`, and `new/` → `cur/`), so the flags
are written wherever `filepath` is: by `upsert_thread` from the parsed
file's path, and by `update_filepath` in the same `UPDATE` as the new
locator. Every rename path — the watchdog's `on_moved`, the
reconciler's `handle_moved` and sweep, and the startup `sweep_paths`
that heals renames made while the indexer was down — goes through
`update_filepath`, so a state change is one row update with no reparse
or re-embed, and the state cannot disagree with the stored path.
Thread-level questions (`list_threads(filter_type="unread")`) are
answered from these rows at query time; nothing per-thread is stored.
An index on `(address, role)` makes "every message from / to X" an
exact indexed lookup — the basis for exhaustive enumeration, as
opposed to relevance search. The MCP server's `query_messages`
enumerates over these tables (count plus keyset pages ordered by
`(effective_at, claimant_id)`), and `find_contact` aggregates
`message_participants` instead of parsing each thread's participant
JSON.

Both are written inside `upsert_thread`'s transaction, after the
message's `message_thread_map` row. `messages` references
`message_thread_map` and `message_participants` references `messages`,
both `ON DELETE CASCADE`, so every existing removal path — reaper,
whole-thread delete, rebuild — cleans them up without separate code.

Every message-level filter the tools accept (`sender`, `recipient`,
`participant`, `subject`, `text`, `folder`, the effective-time bounds,
the attachment and read flags, `authority_class`) is a leaf of one
predicate module, `mcp-server/src/lib/predicates.py` (#1084): each
leaf has a name, a value shape, one SQL compiler over a `messages` row
and an evaluability rule, and adapters build the leaf list for
`query_messages` (conjoined on one message, with the keyset cursor
bound to a digest of the list), for the evidence-scope labels (the
same, as a label per message) and for `search_emails`' thread filters
(each leaf decided on its own against the thread, as before). A new
predicate is written once there; `docs/mcp-tools.md`, "Filter
predicates", lists the leaves.

## Message time

The index records two times per message, `sent_at` and `occurred_at`,
and derives one effective time from them. Every date the MCP tools
filter on or sort by is the effective time; outputs return both stored
fields.

**`sent_at`** is the message's `Date:` header as parsed by
`parsedate_to_datetime` (`indexer/src/parser.py` `_parse_date`),
converted to UTC and stored as an ISO 8601 string
(`2024-03-05T12:00:00+00:00`). A header without a zone (`-0000`) is
read as UTC. It is the sender's claim, not a delivery time: a sender
can backdate or future-date it.

**`occurred_at`** is the delivery time: the date of the topmost
`Received:` header, which the last receiving server adds (expected to
be Proton's; to be checked against real mail at go-live). The parser takes the text after that header's last
`;` (RFC 5321 puts the date there), parses it with
`parsedate_to_datetime`, and stores it in UTC like `sent_at`
(`indexer/src/parser.py` `_parse_received_date`). It is NULL when the
header is absent (sent mail has none) or its date is unparseable; it
never falls back to `Date:`, and no Maildir timestamp or current time
is synthesized. Lower `Received:` headers are added by servers the
sender chooses and are not read. Only the header's last
`RECEIVED_DATE_MAX_CHARS` (256) characters are searched for the `;`,
so a crafted multi-megabyte header costs one slice; a date text longer
than that reads as unparseable. Errors the email package or the codecs
raise on the header degrade to NULL, and the header text is never
logged.

**Effective time** is `COALESCE(occurred_at, sent_at)`, stored as the
virtual generated column `messages.effective_at` (indexed on its own
and with `thread_id`, `folder` and `message_id`).

*Undated mail.* A missing or unparseable `Date:` header is dated at
the time the indexer first parses the file, and that first persisted
`sent_at` is kept when the message is reprocessed or its thread
rebuilt (`Database.keep_persisted_fallback_date`, #297). A delivered
undated message's effective time is its `occurred_at`, read from the
file every time; only undated mail without a readable `Received:`
date (undated sent mail) is dated again at rebuild time when the index
is rebuilt from empty.

Where the times are stored:

| Stored as | What it holds |
|---|---|
| `messages.sent_at` / `messages.occurred_at` | The message's own times (authoritative) |
| `messages.effective_at` | `COALESCE(occurred_at, sent_at)`, generated, never written |
| `threads.date_first` / `date_last` | The earliest and latest effective time among the thread's messages, recomputed from its `messages` rows on every upsert so a re-dated message moves the range; the reap rebuild derives them from the survivors the same way |

A chunk stores no date of its own (#575): a passage's dates, body and
attachment chunks alike, are read from its message's `messages` row
through the claimant ID. A reprocess commits a re-dated message in
Phase 1, before its chunks are rewritten, so a stored chunk copy could
lag the message whenever Phase 2 failed.

`messages.indexed_at`, `message_chunks.chunked_at`,
`attachments.seen_at` and `indexed_files.indexed_at` are indexer
bookkeeping, not message time, and no tool returns them as a message
date. Bitemporal modeling (when a claim was made versus when the event
it describes happened) waits for Phase 5.

**Outputs.** Every per-message and per-passage result returns
`sent_at` and, beside it, `occurred_at` (null when unknown), in the
stored string form: message headers (`get_message`, `get_thread`,
`query_messages`), evidence chunks (`get_evidence`), citations
(`ask_mailbox`, `brief_issue`, `check_conclusion`,
`extract_from_emails`) and attachment hits (`search_attachments`, the
carrying message's times). Thread results carry `date_first` /
`date_last`; an attachment hit also carries its thread's `date_last`.
`get_mailbox_status` reports the oldest and newest thread dates in the
index.

**Filters and sorting.** `date_from` / `date_to` always bound the
effective time (a date-only bound covers its whole UTC day). How a
result qualifies depends on its unit:

| Result | Qualifies when | Sorted by |
|---|---|---|
| Thread (`search_emails`; the threads `get_evidence`, `ask_mailbox`, `extract_from_emails`, `brief_issue` and `check_conclusion` retrieve) | Its `[date_first, date_last]` span (effective times) overlaps the range | Relevance |
| Evidence passage of a retrieved thread (same tools except `search_emails`, mailbox-wide path) | Its thread qualifies; the passage's own dates may fall outside the range, and `ask_mailbox` and `get_evidence` then label it `context` | Relevance within the thread |
| Message (`query_messages`) | Its effective time is in the range | Effective time, newest first |
| Attachment (`search_attachments`) | The carrying message's effective time is in the range | Relevance; with no query, effective time, newest first |

Thread admission and message dates agree because both use the
effective time: a message sent on 31 January and delivered on
1 February is in a February range under every tool, and its thread's
span starts on 1 February.

So a date range selects whole threads (owner decision, 2026-10-02,
replacing #561's per-passage scoping): any passage of a thread whose
span overlaps the range may be shown, including one from a message
outside it, and a thread whose span straddles a short range with no
message inside it still qualifies. Each passage carries its own
message's `sent_at` and `occurred_at` (on `get_evidence` chunks and
on citations), so a model can see which passages fall outside the
range. `ask_mailbox`, `get_evidence`, `extract_from_emails`,
`brief_issue` and `check_conclusion` also label each passage
`in scope` or `context` by whether its own message's effective time
(and sender, participant and folder) meets every filter (#755, #895,
`docs/mcp-tools.md`, "Evidence scope"); the prompt-building tools
state the filters in their prompt and ask the model to work from
in-scope passages. The attachment-name bias that leads a thread's evidence with
the file the query names is not date-scoped either: it orders
passages within a qualifying thread. Ranking lanes are not date-scoped
per passage: a passage outside the range can still lift its thread's
rank.
`list_threads` sorts by `date_last`, newest first; `get_thread` lists
messages by effective time, oldest first; `summarize_thread`'s recent
tail takes the chunks whose messages have the latest effective time.

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

Entities are pruned with the mail that mentions them (#464). When the
reaper removes messages (a partial reap or a whole-thread delete), the
same transaction deletes, among the entities those messages mentioned
only:

- a person with no `message_participants` row left for its address,
  with its aliases;
- an alias that no remaining row carries for its address, while the
  person stays;
- an organization of a deleted person once no person belongs to it.

Entities and aliases still mentioned by surviving mail are untouched.
The sweep examines only the reaped messages' addresses that own an
entity (so recipients past the per-message entity cap cost nothing
beyond the one read that filters them out), each with an indexed
lookup (`idx_message_participants_address_name` serves the
alias check), so its cost follows those messages, not the size of the
table. No MCP output changes: every read joins through
`message_participants`, so a pruned entity could never surface.

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
not proof. One guard applies: Proton files most spoofed and
DMARC-failing mail in Spam, so a message in the `Spam` folder never
counts toward an `authority_class` filter (`AUTHORITY_EXCLUDED_FOLDERS`
in `mcp-server/src/lib/sqlite.py`). Spoofed mail Proton leaves in the
inbox still matches; gating on DKIM/DMARC verdict headers is deferred
until Bridge's headers have been checked on real mail (#463).

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
(a message outside Spam matches when one of its From senders carries
the class; a thread when one of its non-Spam messages does) and on
`find_contact` results, which stay per contact and ignore folders.
Filtering removes results without reordering or rescoring the rest.

## Attachment Indexing

Email attachments flow through the same chunker and embedder pipeline
as message bodies. Two extra tables sit alongside `message_chunks`:

| Table | Keyed by | Purpose |
|---|---|---|
| `attachments` | attachment_occurrence_id | Per-occurrence row capturing filename + MIME + size as it appeared on a specific email. The occurrence id includes the message, payload hash, filename, and attachment slot so duplicate same-payload files in one email are still represented. `extractor_module` names the extraction row the occurrence uses (see below; '' when its label selects no extractor). When one message carries the same bytes under labels that run different extractors, the texts share the message's chunk slice for the payload, and every text's chunks are kept. A text hit in `search_attachments` is attributed to an occurrence whose row is a success; with two such occurrences in one message, to the first by occurrence id. |
| `attachment_extractions` | (attachment_id, extractor_module) | Cache of extracted text + status, keyed by the payload's sha256 and the extractor module the occurrence's MIME type and filename run on those bytes, after the container check (OOXML bytes labelled `.doc` run `docx` and share the `.docx` row; '' when the label selects no extractor) (#928). Dispatch from a label and the bytes is deterministic, so every occurrence with the same key would extract the same result, and an occurrence is served only what an extraction under its own label gives, whatever labels of the same bytes arrived before it: an OLE2 `.doc` first seen as `.txt`, or a PowerPoint file first sent as `.doc` (#986), no longer decides the later occurrences' result. The same bytes under two labels that run different extractors store two rows; the cost is one extraction per module the bytes arrive under and a second copy of the text, which is negligible next to the mail itself. The expensive work (Tesseract OCR, pypdf parse, DOCX walk) runs at most once per payload and module, including within one indexing batch, where results not yet committed are shared by the same key. Non-success rows are also honored: `empty` short-circuits unconditionally; `too_large` short-circuits while the payload still exceeds `INDEXER_ATTACHMENT_MAX_BYTES`, and is re-extracted once the operator raises the cap far enough for it to fit (#693); `unsupported` short-circuits while it holds: an "OCR disabled" row until OCR is turned on; an "OLE2 compound file" row (an OLE2 payload under an OOXML label, #694, #936), a "binary payload labelled as text" row (#932), a "not an OLE2 compound file" row under the `ppt` module (#957) and a "no extractor" row for good, since the label and the bytes decide them; an encrypted-PDF or pypdf-limit row under `pdf`, an eager-part-budget row under `xlsx` and a pre-open package-budget row under `pptx` or `docx` for good too, since that module would decline the same bytes again (#931, #1032); any other (an extractor not importable in the image) only under the '' module; `failed` short-circuits within a 7-day retry window so a chronic failure stops re-running on every reappearance, but a real fix landed via dependency upgrade can pick the payload up later. The `extractor` column carries a version (`docx@5`); a row written by an older version of a fixed extractor (`extractors.EXTRACTOR_VERSIONS`) is re-extracted by the next occurrence that uses it, and the indexer re-queues every message with an occurrence using it once at startup so their chunks are rebuilt, except dead-lettered messages, which keep their stale chunks until `make requeue-dead` rescues them. Rows from a newer version (after a rollback) are kept. A row is deleted with the last `attachments` row that uses it (see *Cascade on message removal*). A stale row an OCR extractor wrote (`image-ocr`, `pdf-ocr`) is kept and served while `INDEXER_OCR_ENABLED=false`, since a refresh could only replace its text with "OCR disabled"; it is refreshed once OCR is on. Likewise, once OCR is on, the startup sweep re-queues each message with an occurrence using an "OCR disabled" row. The sweep also re-queues, whatever the OCR setting, each message whose occurrence uses a "no extractor" or "OLE2 compound file" row but whose label now selects another module, as when a release starts routing an extension such as `.heic` (#691), `.dotx` (#937), `.pptx` (#936), `.ppt` (#957), `.pptm` / `.ppsx` / `.potx` (#947) or `.ppsm` / `.potm` (#1042); the reprocess writes the occurrence's own row and points the occurrence at it, so each is re-queued once. Likewise, after `INDEXER_ATTACHMENT_MAX_BYTES` is raised, it re-queues every message with an occurrence using a `too_large` row whose size (`attachments.size_bytes`) now fits; the re-run rewrites the row, so each is re-queued once, and bytes still over the cap are never re-queued (#693). Schema v1 introduced the key; see *Schema versions*. |

Per-occurrence chunks land in `message_chunks` with the
`attachment_id` column populated and `kind` set to `attachment`. They embed exactly like body chunks
and surface through the same chunk-vector retrieval lane — so a query
matching a PDF's contents lifts the parent thread of the email that
carried it, with zero new MCP search code.

### Extractor dispatch

`indexer.extractors.extract` resolves a (content_type, filename) pair
to a per-format module:

```
content_type → _MIME_DISPATCH (text/plain, application/pdf, ...)
   ↓ unknown MIME
filename ext → _EXT_DISPATCH (.pdf, .docx, .xlsx, .pptx, .png, ...)
   ↓ no match
status="unsupported" (still searchable by filename via attachments_fts)
```

Per-format modules live under `indexer/src/extractors/` and are
lazy-imported so a missing optional dependency (e.g. `python-docx`
not in this image) downgrades to `unsupported` rather than crashing
the indexer at startup.

Legacy Office types (#694, #935, #957): `application/msword` / `.doc`,
`application/vnd.ms-excel` / `.xls` and `application/vnd.ms-powerpoint`
/ `.ppt` select the `doc`, `xls` and `ppt` extractors. The container
decides which one runs: a genuine legacy
binary is an OLE2 compound file (it starts with `D0 CF 11 E0 A1 B1 1A
E1`) and goes to the legacy extractor; any other payload goes to the
DOCX or XLSX extractor, as a best effort for OOXML files mislabelled as
a legacy type. `.ppt` has no OOXML fallback: a `.ppt`-labelled payload
that is not OLE2 is recorded `unsupported` ("not an OLE2 compound file
(labelled legacy .ppt)") without running anything, and that row is
served for later `.ppt` occurrences. An OLE2 payload whose label
selects an OOXML extractor or none (for example a password-protected OOXML package, also OLE2,
labelled `.docx` or `.pptx`) is recorded `unsupported` ("OLE2
compound file") without running anything, since no OOXML parser (DOCX,
XLSX, PPTX) can read it; a `failed` row would be retried every 7 days.
That row is the OOXML module's: an occurrence labelled `.doc` /
`.xls` / `.ppt` selects the legacy extractor and has its own row
(#928). A
row v0 wrote for such a payload carried no extractor and was
migrated under the '' module, so the startup sweep re-queues once
each message whose occurrence of it now selects a module.

- **`.doc`** text comes from `catdoc` (Debian's `catdoc` package in the
  indexer image), run with `-d utf-8 -w` (UTF-8 output whatever the
  locale, no line wrapping), under 64 MiB of address space and 10 s of
  CPU, killed after 60 s (#995). catdoc streams the document: measured
  in the image, the fixture and LibreOffice-written documents of 8.8 and
  21.9 MB take 0.002 to 0.11 s of CPU and complete under 4 to 5 MiB of
  address space.
- **`.xls`** is read by `xlrd` 2.0.2 in a child Python process
  (`extractors/xls_child.py`, started as `python -I`). xlrd's work on
  opening a workbook is not bounded by the payload size: its
  shared-string loop trusts a declared count and a signed skip length
  (6 KB of workbook can loop until memory runs out), and its OLE2
  directory walk recurses with no cycle check (`RecursionError`). So
  the child runs under 512 MiB of address space and 30 s of CPU, set
  before it starts, and the parent kills it after 45 s. Inside
  those limits it applies the XLSX extractor's budgets, counted while
  reading: at most 1,024 sheets, loaded one at a time and unloaded after
  each; 5,000,000 cells visited (padding included, plus 64 per row),
  checked before the next sheet loads; 10,000,000 characters. A budget
  that cut the text is logged through the extractor-cap WARNING
  (`xls_sheets`, `xls_expanded_cells`, `xls_text_chars`). The child
  costs a Python start-up and an xlrd import per workbook, about 0.1 s
  in the image; a 20 MB workbook of 1,000,000 cells takes about 1.2 s
  and 145 MB.
- **`.ppt`** is read by Apache POI 5.5.1's HSLF reader
  (`SlideShowExtractor`: slide text, placeholders and text boxes alike,
  and speaker notes; masters and comments are left out) in a Java
  process (owner decision 2026-10-07, #957). `indexer/java/PptText.java`
  runs on a Java runtime trimmed with `jlink` to five modules
  (`java.base`, `java.desktop`, `java.xml`, `java.logging`,
  `jdk.unsupported`); both live under `/opt/ppt` in the image. The
  build's `ppt-builder` stage fetches the jars pinned in
  `indexer/java/pom.xml` with Maven (strict checksums on download, into
  a BuildKit cache mount with its own id that a later rebuild reuses
  and that trusts every build the operator runs on that builder,
  #1070), compiles the
  reader and writes the runtime; the JDK and Maven stay in that stage,
  and the runtime image grows by about 67 MB (411 to 478 MB). Java runs
  under 512 MiB of address space and 30 s of CPU, and the parent kills
  it after 45 s. The JVM
  runs with a 128 MiB heap, bounded code cache, class space and
  metaspace, the serial collector, C1 only and no class-data sharing,
  the set it needs to start under that limit. Its own messages are
  turned off or sent to stderr, which is discarded, so nothing but the
  deck's text reaches stdout, and it writes no perf-data file, crash
  report or core. Each deck costs one JVM start-up: 0.15 to 0.35 s and
  60 to 70 MB on the fixture decks. catppt, the first candidate, read
  no slide text from decks current PowerPoint or LibreOffice save
  (#958).

All three run through one subprocess runner (`extractors/_runner.py`).
It starts every tool through `extractors/_launcher.py` (`python -I`),
which lowers its own address space (`RLIMIT_AS`) and CPU time
(`RLIMIT_CPU`) to the limits the extractor passes, caps glibc at two
malloc arenas (the JVM needs it to start under its limit), and
`execve`s the tool, so the limits hold before the tool reads a byte;
`run_tool` has no default limits, and a test checks that each caller
passes both (#995). The payload goes to a mode-600 file under `/tmp`
(tmpfs), deleted after the run; the tool gets an argument list with no shell, no stdin and an
environment of `LC_ALL=C.UTF-8` only; its stderr is discarded, since it
can quote the document; and its stdout is read incrementally up to a
byte cap (8 MiB for catdoc and for the `.ppt` reader, whose text past
the cap is not indexed and is reported as `doc_output_bytes` /
`ppt_output_bytes`). A timeout, a death by signal (a crash, or the CPU
limit), a non-zero exit (a tool that fails an allocation under the
address-space limit exits with an error), or an xls child's malformed or
oversized output records `failed` with a fixed error type
(`ToolTimeoutError`, `ToolCrashError`, `ToolExitError`,
`XlsOutputError`); nothing the tool printed reaches a log or
`last_error`. catdoc has no Homebrew formula, so its tests skip on a
Mac without it; CI installs it and fails if it is missing, and the
image carries it. The `.ppt` runtime is built for Linux, so the tests
that run the real reader skip on a Mac unless `INDEXER_TEST_PPT_HOME`
points at an exported `/opt/ppt`; CI exports it from the Dockerfile's
`ppt-runtime` stage and fails if it is missing.

The limits on every external program the indexer runs:

| Program | Address space | CPU time | Wall clock |
|---|---|---|---|
| catdoc (`.doc`) | 64 MiB | 10 s | 60 s |
| xlrd child (`.xls`) | 512 MiB | 30 s | 45 s |
| Java with Apache POI (`.ppt`) | 512 MiB | 30 s | 45 s |
| Tesseract (images, scanned PDFs) | none | none | `INDEXER_OCR_TIMEOUT_SECONDS` per page |
| Poppler `pdfinfo` / `pdftoppm` (scanned PDFs) | none | none | the OCR render deadline (see `INDEXER_OCR_TIMEOUT_SECONDS`) |

Tesseract and Poppler are started by pytesseract and pdf2image, not
through the runner, so they have no memory or CPU limit of their own
and are bounded only by the container's (#1021).

Binary payloads labelled as text: the text extractor decodes whatever
it is given, so a PDF, ZIP (or OOXML), OLE2, PNG, JPEG or GIF file sent
as `text/plain`, or named `.txt` / `.csv` / `.md` with no usable
Content-Type, was indexed as replacement characters. When a payload
bound for the text extractor starts with one of those formats' fixed
signatures (`%PDF-`, `PK\x03\x04`, `PK\x05\x06` (an empty ZIP),
`D0 CF 11 E0 A1 B1 1A E1`,
`89 50 4E 47 0D 0A 1A 0A`, `FF D8 FF`, `GIF87a` / `GIF89a`), the
dispatcher records it `unsupported` ("binary payload labelled as text")
without decoding it (#932). Only those fixed prefixes are checked; the
bytes decide, and the row is the text module's. An occurrence
labelled with the real type (`.pdf`, an image) runs that type's
extractor and has its own row (#928). A binary file labelled only as
text is therefore not extracted (#969).

Word templates: the template MIME type
(`application/vnd.openxmlformats-officedocument.wordprocessingml.template`)
and `.dotx` route to the DOCX extractor (#937), with the same ZIP
guard, OLE2 check, walk and caps as `.docx`. python-docx's
`docx.Document` refuses a package whose main part is the template
type, so the extractor opens the package with python-docx's `Package`
and registers the template content type in its `PartFactory` at import,
which loads the main part as a `DocumentPart`. The payload bytes are
not changed. A template cached `unsupported` ("no extractor") by an
earlier release is re-queued by the startup sweep below, and the
DOCX version bump (`docx@5`) refreshes a template labelled `.docx`
that the previous version recorded as `failed`.

python-docx follows a package's part relationships recursively while it
opens it, so a crafted `.docx` chaining a few thousand related parts
(well under 1 MB) raised `RecursionError`, which the dispatcher
re-raises as host pressure. The DOCX extractor catches it around the
package open only and records the attachment `failed` with
`DocxRelationshipChainError` (#945); a `RecursionError` anywhere else
still escapes as host pressure. No row was cached for such a payload
before, so the DOCX version is not bumped; a message that was
dead-lettered by the old behaviour is picked up again by
`make requeue-dead`.

Before python-docx opens a `.docx` or `.dotx`, the package is checked
from the ZIP central directory (#967, #946), as for `.pptx` below and
with the same shared check. python-docx parses every XML part it
relates whole with lxml, and builds a part for every related member
while checking each relationship against a list of the parts it has
already visited, so opening costs the number of related members times
the number of relationships. Plainly timed, 20,000 related members took
2.6 s and 5,000 members with 4 MiB of the smallest relationships 2.7 s;
32 MiB of element-dense XML peaked at 784 MiB while it opened. So a
document fails as `DocxPackageBudgetError`, before python-docx reads any
member, when its members expand by more than 32 MiB past their
compressed sizes, number more than 5,000, or hold more than 4 MiB of
relationship (`.rels`) parts. A synthetic 500-page formatted document
with 2,000 pictures and 10,000 hyperlinks has about 2,000 members,
16 MiB of expansion and 2.1 MiB of relationships. Pictures are stored
compressed and barely expand, but members stored uncompressed are not
counted against the expansion budget, so at the default
`INDEXER_ATTACHMENT_MAX_BYTES` python-docx can still parse up to about
64 MiB of XML (#1033). An over-budget document is recorded
`unsupported` ("document exceeds a pre-open package budget"), not
`failed`, since the same bytes trip the budget on every run (#1032; see
*Permanent extractor failures* below). The DOCX version is not bumped,
neither by the budgets (#1036) nor by the mapping (#1032; PR #1068's
bump to `docx@6` was reverted): a bump would re-run every cached
document once through the walk after the open, which has no budget of
its own yet (#1031), and re-record an over-budget document that was
read in full before the budgets as `unsupported`. So a document read in
full before keeps its cached text. A package-budget row recorded
`failed` before the mapping is not re-queued by the startup sweep, so
it stays `failed` until the same bytes are processed again (a new
occurrence, or the message reprocessed for another reason) more than
7 days after it was recorded; that re-run reads the central directory
once, never opens the document, and records the row `unsupported`
under the mapping. A deliberate `docx` bump, once #1031 lands,
converts whatever rows remain at the next start. A row stamped
`docx@6` by a build between #1068 and its revert (#1075) is kept, since
a newer row is never downgraded; rolling back to such a build treats
the `docx@5` rows written since as stale and re-runs them through the
unbudgeted walk at its next start, which is that build's behaviour.
`DocxRelationshipChainError` (#945)
still applies to a chain under these budgets.

PowerPoint (#936): `application/vnd.openxmlformats-officedocument.presentationml.presentation`
and `.pptx` route to the PPTX extractor (`python-pptx`), which reads,
slide by slide, the text of every shape (text boxes, placeholders, auto
shapes), each table as one line per row, the shapes inside group
shapes at any depth, and then the slide's speaker notes. Pictures are
not read or OCR'd, and chart and SmartArt text is not extracted (#947).
Macro-enabled decks (`application/vnd.ms-powerpoint.presentation.macroEnabled.12`,
`.pptm`), slideshows
(`application/vnd.openxmlformats-officedocument.presentationml.slideshow`,
`.ppsx`) and templates
(`application/vnd.openxmlformats-officedocument.presentationml.template`,
`.potx`) route to the same extractor (#947), as do macro-enabled
slideshows (`application/vnd.ms-powerpoint.slideshow.macroEnabled.12`,
`.ppsm`) and templates
(`application/vnd.ms-powerpoint.template.macroEnabled.12`, `.potm`)
(#1042), with the same ZIP guard, OLE2 check, pre-open package budgets,
walk and caps as `.pptx`. `pptx.Presentation` refuses every main-part
type but the presentation and macro-enabled presentation, although
python-pptx's own part factory already loads the slideshow and template
types as a presentation part; so the extractor opens the package with
python-pptx's `Package` and reads the main part when it is one of the
six presentation types. python-pptx has no mapping for the macro-enabled
slideshow and template main parts, so the extractor registers those two
in its `PartFactory` at import, as the DOCX extractor does for `.dotx`;
without it the main part loads as a generic part and the extractor
refuses it. The payload bytes are not changed. Only slide and notes
text is read from a `.pptm`, `.ppsm` or `.potm`: the macro part
(`vbaProject.bin`) is loaded by python-pptx as an opaque part and never
read or run. The PPTX version bump (`pptx@2`) refreshed a slideshow or
template labelled `.pptx` that the previous version recorded as
`failed`; the #1032 bump (`pptx@3`) does the same for a macro-enabled
slideshow or template labelled `.pptx`. Legacy binary
`.ppt` is read by the `ppt` extractor (Apache POI), not this one. A
`.pptx`, `.pptm`, `.ppsx`, `.potx`, `.ppsm` or `.potm` cached as "no
extractor" before #936, #947 or #1042 is re-queued once by the startup
sweep above and read.

Encrypted PDFs: `pypdf` opens an encrypted PDF with the empty user
password, so an owner-password-only PDF (print or copy restrictions,
no open password, common for statements and legal letters) extracts
like any other, including AES-encrypted ones, which use the
`cryptography` package (#691). No other password is tried. A PDF that
needs a real open password is recorded as `unsupported` ("encrypted
PDF (open password required)") and stays searchable by filename only.

Permanent extractor failures: an exception the same bytes always
repeat is recorded `unsupported` with fixed text instead of `failed`,
so it is not re-run every 7 days (#931): a PDF that needs an open
password (pypdf `FileNotDecryptedError`), a PDF over one of pypdf's
structural limits while it is opened or its pages are listed
(`LimitReachedError`, such as page-tree depth or entry count; "PDF
structure exceeds pypdf limits"),
a workbook over the XLSX eager-part budget below ("workbook exceeds
the eager-part budget"), and a deck or document over the PPTX or DOCX
pre-open package budgets ("presentation exceeds a pre-open package
budget", "document exceeds a pre-open package budget"; #1032), which
are decided from the ZIP central directory alone before the package is
opened. Each is matched by exact exception class;
anything else stays `failed`. The row is keyed by the module that
raised the error (#928), so it is served only to occurrences that run
that module on the bytes; an occurrence whose label runs another
extractor has its own row and extracts. It is stamped with the
extractor version (`pdf@5`), so a later version bump, for example one
that raises a budget, refreshes it; the bumps that came with the
`pdf`, `xlsx` and `pptx` mappings (`pdf@5`, `xlsx@6`, `pptx@3`)
refreshed the `failed` rows the previous versions wrote, since the
startup sweep re-runs only stale rows, never aged `failed` ones. `docx`
was deliberately not bumped (its walk is unbudgeted, #1031; see the
DOCX budget paragraph above), so a `.docx` / `.dotx` package-budget row
recorded `failed` before the mapping stays `failed` until the same
bytes are processed again more than 7 days on, or until a `docx` bump
follows #1031. Each logs a rate-limited WARNING
(`extractor <module> declined ...; recorded unsupported, not retried`).
A pypdf limit hit inside one page's text extraction (a `/ToUnicode`
map over its size limit, for example) is not one of these: like any
per-page error, that page is counted in `pdf_pages_failed` (and OCR'd
when OCR is on) and the other pages' text is kept.

The parsing libraries log and warn with values read from the
attachment (pypdf's font dictionaries and encoding names, openpyxl's
cell values, Pillow's TIFF tags), so the indexer's logging setup
(`quiet_document_libraries` in `indexer/src/main.py`) raises the
`pypdf` and `PIL` loggers to `CRITICAL` and ignores warnings raised
inside `openpyxl` and `PIL` (#690). Outcomes stay visible through the
extractors' own fixed-text log lines and the `attachment_extractions`
status.

HEIC / HEIF photos (the iPhone default) are images like any other:
`image/heic`, `image/heif` (any `image/` type) and the `.heic`, `.heif`
and `.hif` extensions route to the image extractor, which opens them through the `pillow-heif`
Pillow plugin (#691). They go through the same byte cap, pixel cap and
decompression-bomb handling as other images; Pillow checks the size in
the header before anything is decoded. Only the primary image is OCR'd;
thumbnails, depth maps and auxiliary images are not decoded. The HEIF
image-sequence extensions `.heics` / `.heifs` are not routed by name;
a sequence sent with an `image/` MIME type reaches the image extractor
like any image.

`pillow-heif` bundles its own copies of the native libheif and libde265
(HEVC) decoders, plus a libx265 encoder that is never used. Both
decoders have a long record of memory-safety CVEs and run inside the
indexer process on attacker-supplied files. The owner accepted that
risk on 2026-10-04. Debian security updates do not cover the bundled
copies: a fix arrives only by bumping `pillow-heif`. Dependabot and
Trivy see only the `pillow-heif` version, not the bundled libraries, so
watch libheif and libde265 advisories and bump `pillow-heif` when a
release picks up a fix.

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
document of either kind; a document the cap cuts short logs a WARNING
and is counted in the attachments line (`docs/troubleshooting.md`).

OCR assumes English. The indexer image installs only Tesseract's
English language data (`eng`, plus `osd`), and the extractors pass no
language to Tesseract, so it uses its English default. Scans in a
non-Latin script or with heavy accents extract poorly, and installing
another `tesseract-ocr-*` language pack would not change that by
itself, since nothing selects it. There is no setting for this today;
issue #490 tracks it.

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
| `INDEXER_ATTACHMENT_MAX_BYTES` | `33554432` (32 MiB) | Skip very large attachments — bounds CPU/memory for huge zips. Sized for the 10–30 MB scanned PDFs common in real mail; an `.eml` under the default `INDEXER_PARSE_MAX_BYTES` (50 MB) carries at most ~36 MB of base64-encoded attachment. Raising it re-queues, once at startup, the messages whose attachments were cached `too_large` and now fit |
| `INDEXER_OCR_MAX_PAGES` | `20` | Cap pages OCR'd per PDF or multipage TIFF |
| `INDEXER_OCR_TIMEOUT_SECONDS` | `60` | Per-page Tesseract timeout — bounds runaway OCR on a crafted high-noise image — and the deadline for rendering a scanned PDF's pages with Poppler. pdf2image's own page count before each render takes no timeout, so the indexer first times one bounded page count: one over half the deadline is an OCR timeout, and each render's timeout holds back that time. A page count much slower on pdf2image's call than on the timed one can still overrun (#868). Set `0` to disable both. |
| `INDEXER_PDF_MAX_DIGITAL_PAGES` | `500` | Cap pages walked by the digital pypdf path — protects against text-only PDFs with thousands of pages. Set `0` to disable. A PDF cut here logs the `pdf_digital_pages` extractor-cap WARNING (#903). |
| `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` | `2000000` (~500 pages) | Truncate extracted text before persisting in `attachment_extractions`. Bounds SQLite row size for very long OCR'd PDFs. Set to `0` to disable. The XLSX extractor also stops at 10,000,000 characters of its own, whatever this is set to, so shared strings repeated across many cells cannot expand without limit (#294). Text cut by either logs an extractor-cap WARNING (`extracted_chars`, `xlsx_text_chars`) and counts in the attachments line's `extractor_caps` (#903). |

The XLSX extractor has fixed budgets of its own besides these. It cuts
worksheets at an XML node budget (#432). The parts openpyxl loads whole
rather than streams (the shared-string table, `[Content_Types].xml`,
the workbook and its relationships, styles, theme, core and custom
properties, each worksheet's relationships, and chartsheets with their
drawings, charts and images) are charged their declared sizes before
openpyxl opens the workbook: 8 MiB per part, and 16 MiB and 4,096 reads
across the workbook. A workbook over one of these is recorded
`unsupported` ("workbook exceeds the eager-part budget", #931) with no
text kept (#428). External links
are not loaded at all.

The PPTX extractor (#936) is bounded by the zip guard before
python-pptx opens the deck, and by a 32 MiB expansion budget: python-pptx
parses every XML part whole (about 15 bytes of memory per byte of XML),
so a deck whose members expand by more than 32 MiB past their
compressed sizes fails as `PptxPackageBudgetError` before it is
opened, as does one with more than 20,000 members or more than 8 MiB of
relationship (`.rels`) parts, since python-pptx builds a part for every
related member and walks every relationship as it opens; the dispatcher
records it `unsupported` ("presentation exceeds a pre-open package
budget", #1032), not `failed`. It then counts its walk against four budgets
per presentation: 5,000 slide-list entries (an entry naming a slide
already read is skipped, not read again), 100,000 shapes (each group
and every shape in it, and each notes-page shape), 200,000 table rows
and cells, and 10,000,000 characters (a paragraph, and each element in
one, also costs eight). The first budget to run out keeps the text read so
far and logs its extractor-cap WARNING (`pptx_slides`, `pptx_shapes`,
`pptx_table_cells`, `pptx_text_chars`). python-pptx parses each part
whole and follows the package's relationships recursively when it
opens a deck; a crafted chain of related parts too long to follow
fails as `PptxRelationshipChainError` rather than as host pressure.

### Cascade on message removal

When a message is reaped, `_delete_attachments_for_message` drops its
`attachments` rows and FTS shadows; the `_delete_chunks_for_message`
cascade also drops the message's attachment chunks (they share the
`claimant_id` key). In the same transaction it deletes each cached
`attachment_extractions` row of the message's payloads that no remaining
`attachments` row uses (same payload and `extractor_module`), so a
payload's extracted text does not outlive every message that carried it
(#562, #928). A row another message's occurrence still uses is kept.
When a reprocess points an occurrence at another module's row, the row
it left is purged the same way if nothing else uses it. The cost is the cache for a re-arrival: the same bytes
arriving after their last carrier was reaped are extracted again. The
check is one indexed statement per payload the message carried
(`idx_attachments_attachment_id` and the extraction primary key), so
it does not scan either table.

The indexer's write connection sets `PRAGMA secure_delete = ON` (#602),
so SQLite overwrites the bytes of every row a reap deletes with zeros,
including freed overflow pages (long bodies and extractions), instead
of leaving them in free pages of `mail.db`. The indexer image's SQLite
(Debian trixie's libsqlite3 3.46.1) is compiled with
`SQLITE_SECURE_DELETE` and already defaults to ON; a Python whose
bundled SQLite does not (Homebrew Python 3.14's SQLite 3.53.4 defaults
to OFF) gets the same behaviour from the explicit pragma, so local runs
and tests match the container. `FAST` was not used because it leaves
freed overflow pages unzeroed. sqlite-vec zeroes a deleted
vector's slot in its chunk blob itself, and a chunk emptied by deletes
is dropped and its pages zeroed. What the pragma does not cover:

- **WAL window.** The zeroed pages reach `mail.db` at the next
  checkpoint; until then the main file still holds the old page. The
  WAL frames written when the row was inserted or updated also still
  hold its text until the WAL is truncated. SQLite's automatic
  checkpoint restarts the WAL from the start but does not shrink it,
  so older frames beyond the new write point linger. The indexer's
  periodic `wal_checkpoint(TRUNCATE)` (every
  `INDEXER_WAL_CHECKPOINT_INTERVAL_SECS`, default 600 s) ends both;
  it reports busy and retries on the next pass while an mcp-server
  read transaction is open, so the window can be longer under
  continuous queries.
- **FTS5 index terms (between maintenance passes).** The FTS5 tables
  are contentless, so they hold no message text, but the terms of a
  deleted row (stemmed words with their row and position lists) stay
  in live `*_fts_data` segment pages until a merge rewrites that
  segment. These pages are not freed, so `secure_delete` does not
  touch them, and FTS5's own `secure-delete` option does not apply to
  `contentless_delete=1` tables (verified on 3.46.1 and 3.53.4). The
  indexer therefore scrubs them (#641, #670): each periodic
  maintenance pass, just before the `wal_checkpoint(TRUNCATE)`, it
  merges every FTS5 table (`threads_fts`, `message_chunks_fts`,
  `attachments_fts`) that a reap deleted from since its last pass into
  one segment, which drops the deleted terms, those of earlier deletes
  included; the checkpoint that follows copies the rewritten pages
  into `mail.db` and truncates the WAL frames that held the old ones.
  A reap marks the tables it deleted from, and so does a message's
  chunk replacement, which happens only when the extractor or chunker
  output changes (for example after an `EXTRACTOR_VERSIONS` bump),
  since the replaced text may be what the fix removed. A thread
  re-index does not: it replaces the thread's `threads_fts` row on
  every new reply with a newer version of the same live mail, so
  nothing private is left behind, and FTS5's automerge keeps the
  superseded rows in check (measured: 20,000 re-indexes with no scrub
  left the index about 20% above its fully merged size, not growing).
  The merge runs in steps, each committed on its own, so the indexer's
  write lock is held for one step at a time: the first FTS5 `merge`
  has a negative page budget and starts the merge, and later ones
  continue it with a positive budget, as the FTS5 documentation gives,
  so a segment indexing writes between steps cannot restart it. Each pass logs one line,
  `fts scrub tables=<names> duration=<s>`; a failed step logs its
  exception type and the table stays pending for the next pass, as
  does one not finished within the step cap, which continues its merge
  there; a reap in between starts a new merge, which also takes in the
  segments written since the old one began. So there is no fixed
  deletion time. A reaped message's terms usually leave the file at the
  first pass after the reap, once that pass's merge finishes and its
  checkpoint copies the rewritten pages. They stay across further
  passes while the merge is unfinished (a table needing more than the
  per-pass cap of `_FTS_SCRUB_MAX_STEPS` steps of `_FTS_SCRUB_STEP_PAGES`
  pages, 1,000 x 2,000 today, continues on later passes) or its step
  fails, and longer while a busy checkpoint retries, as above. Merge
  completion, the checkpoint and storage below SQLite (free blocks,
  snapshots, backups) are separate stages, and only the first two are
  under the indexer's control. The pending mark is kept in memory,
  so every table starts pending: the indexer scrubs all three once at
  startup, right after opening the database and before it waits for
  the embedder, which covers a reap whose scrub a restart cut short
  even while the embedder is down.
- **Pages freed before the pragma.** It zeroes pages as later deletes
  free them; it does not rewrite pages already on the freelist. A
  `mail.db` reaped under a SQLite that defaulted OFF (an indexer run
  outside the container, before #602) can still hold those rows'
  bytes. The container's SQLite already defaulted ON, so a database
  only ever written by the indexer image is not affected. To clear an
  affected file, rebuild the index from Maildir, or, with the indexer
  stopped, run `VACUUM;` against `mail.db` (it rewrites the file
  without the freelist and needs free space equal to its size).
- **Below SQLite.** Truncating the WAL and zeroing pages in place do
  not reach filesystem free blocks, APFS or volume snapshots, or
  backups of `mail.db`.

Measured on a 169 MB synthetic index (SQLite 3.53), deleting 1,000
threads with three chunks each took 3.8-4.0 s with the pragma OFF, ON
or FAST alike; ON wrote about 8 MB of WAL against 6 MB. Dropping an
emptied sqlite-vec chunk (up to 1,024 vectors, about 16 MiB at 4,096
dimensions) writes that many zero bytes once under ON.

A scrub merges the whole table, so its cost grows with the index, not
with the delete. Measured with plain timing on a synthetic index the
size of a ~50k-message mailbox (167 MB: 20,000 threads of about 800
words, 150,000 chunks of 200 words, 30,000 attachment names) after a
reap-sized delete, on both 3.53.4 and the image's 3.46.1, a one-shot
`optimize` took `threads_fts` 0.41-0.55 s and about 75 MB of WAL,
`message_chunks_fts` 0.75-0.88 s and about 96 MB, `attachments_fts`
0.01 s and 2 MB. The stepwise scrub does the same total work: on a
20,000-thread table it took 9 steps of at most 0.2 s, with a WAL peak
of 17 MB against 69 MB for `optimize`. The WAL is truncated by the
checkpoint that follows. A scrub runs only after a reap and once at
startup, not on every pass while mail arrives.

The purge only looks at payloads the message being reaped carried. A
database whose reaps ran before #562 can still hold extraction rows
whose last carrier was already reaped, and nothing revisits them
(#626). Rebuild the index from Maildir, or, with the indexer stopped,
run once against `mail.db`:

```sql
DELETE FROM attachment_extractions WHERE NOT EXISTS (
  SELECT 1 FROM attachments a
  WHERE a.attachment_id = attachment_extractions.attachment_id
    AND a.extractor_module = attachment_extractions.extractor_module);
```

## Schema versions

The indexer stamps the schema version in `schema_version` (and
`PRAGMA application_id`). A fresh install creates the current schema
directly; an existing index runs the forward migrations in
`indexer/src/migrations/` at startup, each in its own transaction, so a
failure leaves the last applied version stamped and the next start
retries it. A version above the code's, or a missing migration file,
stops startup (`docs/troubleshooting.md`).

Before deploying a release that adds a migration, take a snapshot with
`make backup-index BACKUP_DIR=<directory outside the checkout>`: a
migration that commits but turns out wrong is then undone with
`make restore-index BACKUP=<file>`, after recreating the containers
from the release before the migration, instead of a full rebuild from
Maildir (`docs/troubleshooting.md`, "Back up and restore the index").

| Version | Migration | Change |
|---|---|---|
| 0 | (initial schema) | First deployed schema (2026-10-03). |
| 1 | `0001_extraction_cache_per_module.sql` | `attachment_extractions` keyed by (content hash, extractor module); `attachments.extractor_module` (#928). Each v0 row keeps its result and stamp and is keyed by its stamp's module (`docx@5` -> `docx`, `pdf-ocr@4` -> `pdf`), or '' when it has no stamp (`unsupported`, `too_large`); each occurrence is pointed at its payload's row, as before. No `EXTRACTOR_VERSIONS` bump comes with it, so nothing is re-extracted for the re-keying alone. An occurrence whose label selects another module than its row's moves to its own row the next time its message is reprocessed. |

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

**Reaped-source records.** A citation or search hit from an earlier
answer can name a message or thread the reaper has since removed. Such
a source reads as removed for 30 days and then as not found, and the
record never keeps its evidence text (PLAN Resolved decisions 14;
extracted attachment text the reap leaves in `attachment_extractions`
is the separate #562, see *Cascade on message removal*). For that
window the reap transaction writes one content-free `reaped_messages`
row per removed message:
claimant ID, Message-ID, thread ID and reap time, and nothing else (no
subject, body, participants, attachment names or chunk IDs, which hash
the passage text). `get_message`, `get_thread` and thread-scoped
`get_evidence` consult it, in the same read snapshot, only after the
live lookup finds nothing, and answer "reaped from the index on <date>
(mirror retention)" instead of "not found"; `get_thread` on a surviving
thread lists its reaped messages (see *Reaped sources* in
`docs/mcp-tools.md`). The date is the local reap, not the upstream
deletion, and the record does not say whether the message was deleted
upstream or its file went missing locally. The
identifiers come from the sender's Message-ID, so the records are kept
short: the indexer deletes rows older than 30 days
(`REAPED_RECORD_RETENTION_DAYS` in `indexer/src/database.py`) at startup,
before the embedder wait and the initial index, and on every
reconciliation interval, in archive mode too, so rows written before a
switch to archive still expire. mcp-server mirrors the window
(`src/lib/sqlite.py`) and ignores older rows when it reads them, so a
row the indexer has not pruned yet is never served past 30 days.
Answers are not
persisted, so nothing else refers to a reaped source. The records are
not carried across a rebuild of the index from Maildir (reaped files
are not reindexed), so after a rebuild an earlier reap reads as "not
found".

**Byte-identical copies.** Two files with the same bytes share one
claimant ID (see *Claimant IDs*), so `message_thread_map` holds only
the path indexed last, while `indexed_files` holds both (#1102). When a
sweep finds a message's mapped file gone, it first looks for another
indexed path with the same `content_hash` that still exists, with one
pass over `indexed_files` per sweep covering every missing message. If
one exists (a live copy is preferred to a `T`-flagged one), the message
is remapped to that copy: its locator, folder and S/F/R state move as
for a rename, and a tombstone or queued job on the gone path moves too
unless the copy already has its own. The copy's tombstone is kept; of
two queued jobs the runnable one is kept (the copy's, unless it is dead
and the gone path's is not). The remap is refused, and retried once
after resolving the copy again, when the watcher renamed the copy or
moved the mapping since the sweep resolved it. The trash rule then
applies to the copy as to any file, so a `T`-flagged copy is tombstoned
and a live one clears an earlier tombstone. A mapped file that still
exists but is `T`-flagged is checked the same way, for a live copy
only, in the same lookup: when one exists the message moves to it
instead of being tombstoned, so trashing one copy does not reap a
message whose other copy is live. The rename sweep (`sweep_paths`),
which runs at startup, before each periodic rescan's Maildir walk and
on folder-watch recovery, does the same remap for gone files, so
archive mode does not keep the gone path, folder and flags. The INFO
lines of both sweeps count these as `remapped=N`. Only a message with
no surviving copy is tombstoned as missing (and a trashed one with no
live copy as trashed), so removing every copy still reaps it as before, and the reap
then unmarks every path with the message's bytes, not only the mapped
one (looked up once per reap pass, unmarked by path in each thread's
reap transaction, with any queue row on those paths): a copy that comes
back after a transient outage is re-indexed by the next Maildir walk
instead of staying marked indexed, or held by a dead job, with nothing
left to repair it. Just before a thread's reap is written, the copies
are checked on disk once more: if mbsync restored one since the sweep,
the message moves to it, its tombstone is cleared and the thread is
left for the next pass (logged at INFO with a count). A failure of the
rename sweep before a periodic rescan is logged at WARNING with its
type and does not stop that rescan's walk, and its recovery is logged
like the other recurring steps'. Known limitation (#1141): a copy that
is not the mapped path and is moved to another folder keeps its old
path in `indexed_files`, so the lookup cannot find it until the next
periodic rescan indexes the destination.

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
on the Maildir itself. The indexer never deletes Maildir files: its
`/maildir` mount is read-only and the files belong to mbsync, so a
reaped message's `.eml` stays on disk (deleting the files of mail
deleted in Proton is tracked on the mbsync side in #728). Because of
that, the indexer's enqueue paths skip `T`-flagged files while reconciliation is
enabled so a reaped message is never re-indexed (see *Ingestion
completeness*).

**Trash under mirror.** Deleting a message in Proton normally moves it
to Trash, and mbsync syncs the Trash folder. The INBOX copy is
`T`-flagged and tombstoned, but a Trash copy with the same Message-ID
arrives and is indexed, so the message is reaped only after it is
purged from Trash (plus the grace window). To keep it out of results
meanwhile, the MCP server leaves Trash out of mailbox-wide retrieval
unless a call names it (`folders=["Trash"]`, or `folder="Trash"` for
`query_messages`); see *Trash is left out by default* in
`docs/mcp-tools.md` (#441). The exclusion is query-side only: Trash is
still synced and indexed, and mbsync is unchanged.

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

**Initial scan order (#699, #752).** The queue hands out rows by due
time (`next_attempt_at`), and the initial scan queues each unindexed
message due at its own effective time (the topmost `Received:`, else
`Date:`, read from its header block only by `message_sort_time`),
capped at the walk's start; undated and future-dated messages are due
at the start. So the backlog is indexed oldest first across every
folder, mail the watcher queues while the scan runs is due when it
arrives and follows the backlog, and rows already queued and never
tried (left by an interrupted scan or by a version before this order)
are re-dated the same way, so they interleave by date with newly found
mail; rows in a retry backoff or parked as trashed keep their due
time. This keeps each message
ahead of the replies to it: the threader joins a reply to an indexed
parent through `In-Reply-To` / `References`, but never merges a parent
into a thread its replies started earlier, so a reply indexed first
leaves the conversation split. The previous folder-by-folder walk order
(Sent before INBOX) put about 10,000 of the live mailbox's 21,662
replies ahead of every message they reference; oldest first puts none.
Reading the header blocks takes about 12 s for 33,000 messages. Newest
first, which would make recent mail searchable sooner, is not offered:
it would split threads until #752's merge-on-arrival exists (owner
decision, 2026-10-05). Periodic rescans keep walk order and read no
headers.

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
  per-call chunking via `EMBED_BATCH_SIZE` (default 64). With
  `EMBED_CONCURRENCY` above 1 (default 1) those requests overlap: up to
  that many are in flight, a new one is sent only as one finishes, and
  vectors are reassembled in input order. The first failure stops new
  requests and fails the call as on the sequential path (#713). Each
  request also checks for a recorded failure right before every
  provider call, retries included, and skips the call if there is one
  (#720); a request already past that check counts as in flight, like
  one already sending.
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

The container healthcheck (`indexer/healthcheck.sh`) fails once the
heartbeat file is 10 minutes old. Besides each message and embed
request, the PDF and image extractors refresh it after every page they
read or OCR, so a scanned PDF that runs for ~20 minutes stays healthy
(#485). Pages do not restart the stall guard's clock, which stays per
attachment; a page that hangs refreshes nothing.

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

watchdog's dispatcher thread catches only its own empty-queue
timeout, so an exception escaping a handler (`enqueue` or
`is_indexed` on a locked database, a full disk) ends it, and from
then on only the rescan finds new mail. Every heartbeat
(`touch_health_file`: per message, embed request and attachment page,
during the initial index as well as each pass of the main loop)
checks `observer.is_alive()` before it refreshes the health file; a
dead thread logs an ERROR and exits the process with status 1, the
stall guard's remedy, so Compose restarts the indexer with a fresh
watcher and the startup walk covers the gap (#870). The handlers are
not wrapped in a catch-all: the failure must stay visible. The
Maildir walk and the watch's directory walk each log a WARNING with
the number of directories they could not read, since mail in them is
neither indexed nor watched until they are readable; the count never
carries folder names.

mbsync creates each folder directory 0700 and makes it readable to
the indexer's UID only in its post-sync permission repair, which runs
after every sync attempt, failed ones included, and before the
last-sync stamp is written. inotify cannot watch a directory the
indexer cannot read, and watchdog skips it silently, so a folder
created during a sync is unwatched when it becomes readable (#516).
After each repair mbsync renames an empty `.mbsync-perms-repaired`
marker into place at the Maildir root, so a failed attempt, which
writes no stamp, still signals (#524); the marker acknowledges no
sync. The marker's rename, or the stamp's, signals the main loop,
which walks the folder directories (not `cur`/`new`/`tmp`, so the walk is linear in
folders) and, when any directory is readable that was not when the
watch was last scheduled, or sits at a known path with a new inode
(deleted and recreated, which drops its watch), or when the watch has
reported a directory created since (a recreated directory may reuse
its inode number), unschedules and re-schedules the recursive watch (`FolderWatchRefresher`, `indexer/src/folder_watch.py`). Events
in the gap between the old and new watch are covered by the rename
sweep (`sweep_paths`) and a Maildir walk, which also queues the mail
already in the newly watched folders; if either step fails, both run
again on the next marker, sync stamp or periodic tick until they
succeed. Each signal re-schedules the watch at most once, and a sync
attempt that opens no new directory costs only the folder walk. The
same check also runs with the periodic rescan, for a marker that
could not be written and to retry a re-schedule that failed.

When deletion reconciliation is enabled, every enqueue path — the
startup scan, the periodic rescan, the zero-vector recovery sweep, and
the watchdog's `on_created` / new-delivery `on_moved` branches — skips
`T`-flagged files, and the drain never indexes a claimed job whose file
has been `T`-flagged since it was queued (the reaper owns that message):
a message still in the index keeps its job parked, which the reap
deletes or, if mbsync clears the flag first, the rename moves back to the
live path and makes due at once; a trashed file never indexed has its job dropped. A reaped message's `.eml` stays on disk (the indexer never deletes
Maildir files; see #728 for local file deletion) and is no longer
indexed or queued, so treating it as undiscovered mail would resurrect
it into search (and
the next sweep would start a fresh grace window). If mbsync later
clears the `T` flag because the message was restored upstream, the
file is live mail again and is re-indexed normally. With
reconciliation disabled (archive mode), the index is append-only and
`T`-flagged files are indexed like any other. Archive mode writes no
tombstones and reaps nothing, but it inherits any `pending_deletions`
row an earlier mirror-mode run left: the watchdog handler and the
startup rename sweep carry such a tombstone along with flag renames,
and clear it when a rename drops the `T` flag, so a message restored
upstream stops reading as pending deletion (`docs/mcp-tools.md`,
*Pending deletion*; #860). The handler logs one INFO line per restore
within the indexer's shared per-window line budget (the rest are
counted on the queue heartbeat's `suppressed_lines`); the sweep reports
`tombstones_cleared=N` on its own line.

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
(per message, per embed batch and per attachment page read, at most
every 30 s), the indexer
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

`content_hash` is read by one lookup, `find_identical_copies`: when a
message's mapped file is gone, the reconciler sweep and the startup
rename sweep look for another indexed path with the same bytes and
remap the message to it, and the reaper unmarks every such path of a
message it removes (see *Byte-identical copies* under *Deletion
Reconciliation*, #1102). `indexed_files` has no `content_hash` index,
so the lookup reads the wanted hashes by claimant ID and makes one pass
over `indexed_files` per sweep or reap pass. `size` and `mtime_ns` are
not read; they remain for a future pass that tells a flag-only rename
from a genuine content change.

Rows for which `stat` / hash capture failed at parse time carry NULL
identity values. The lookup skips a message whose own hash is NULL, and
a NULL row never matches a hash, so such a message is handled as before
(tombstoned as missing when its file is gone); the columns are
populated lazily on the next reindex of the file.

## Vulnerability Scans

`.github/workflows/security.yml` runs Trivy over the repository: the
uv lockfiles, `pyproject.toml` files and `indexer/java/pom.xml` for
vulnerable dependencies, and the Dockerfiles for misconfiguration. It
fails on HIGH or CRITICAL findings. The misconfiguration scan runs
offline (`--offline-scan`): Trivy's Maven analyzer would otherwise
resolve `pom.xml` from Maven Central, which rate-limits the shared CI
runners (#1047). The dependency scan still resolves it, from a cached
`~/.m2/repository` keyed on `indexer/java/pom.xml` that the job fills
with `mvn dependency:resolve` (strict checksums, JDK 21 like the image
build) before the scan; Trivy reads POMs from that directory before
asking Maven Central, so a warm cache makes no request (#1069). `make trivy`
runs the same scans locally with the same flags (#1017), skipping the
per-checkout `.uv-cache` as the workflow does;
`scripts/tests/trivy_flags_test.sh` fails when the two differ.

### Image scan

`.github/workflows/docker.yml` also scans the three built images
(indexer, mcp-server, mbsync) with Trivy after `docker compose build`,
on each change to a build input and weekly (#977). Before that build,
a pull-request run restores the indexer's `ppt-builder` stage from the
GitHub Actions cache (BuildKit's `gha` backend, #1070), so Maven
Central is contacted only when `indexer/java/pom.xml` or a layer
before it changed. Every run on `main` (a push, the weekly schedule, a
manual dispatch) restores nothing: it builds the stage from the
current Debian packages (a restored apt layer is never rerun, and
Trivy cannot see the `jlink` runtime) and writes the cache
pull-request runs restore, so a restored stage is never older than the
last build on `main`. The `indexer pytest` job of
`.github/workflows/tests.yml`, which exports the `ppt-runtime` stage
for the indexer's `.ppt` tests, restores and writes the same cache
under the same rule (#1105), so it too reaches Maven Central only on
a run on `main` or after a change to the stage's inputs; that export
is one build, which fetches the restored layers it copies, so it
needs no loaded image. The runner's BuildKit is new on every
run, so the Dockerfile's cache mount of the Maven repository helps
local rebuilds only. This covers what
the lockfiles do not: Debian packages installed with apt (catdoc,
Tesseract, Poppler and the base image's own packages), the Python
packages actually installed, and the `.ppt` reader's jars in
`/opt/ppt/lib`. The job fails on HIGH or CRITICAL findings that have a
fixed version; findings with no fix yet are not gated (owner decision
2026-10-07), but the full report for each image, every severity and
unfixed findings included, is uploaded as the `trivy-image-reports`
artifact.

The same job writes a software bill of materials per image (#767):
Trivy's CycloneDX JSON output for each built image, listing the
packages the scan above saw (Debian packages, Python packages, the
`.ppt` reader's jars) with no vulnerability data, which the reports
carry. The three files (`sbom-indexer.cdx.json`,
`sbom-mcp-server.cdx.json`, `sbom-mbsync.cdx.json`) are uploaded as
the `image-sboms` artifact of the run, kept for 14 days: open the run
under the repository's Actions tab (the Docker workflow) and download
the artifact from its summary page, or run `gh run download <run-id>
-n image-sboms`. An SBOM lists packages, never mail data, and Trivy
reads the built image from the Docker daemon, not the checkout. There
is no local target for it; `trivy image --format cyclonedx --output
<file> <image>` with the pinned Trivy produces the same file.

Not covered: the Java runtime that `jlink` builds into `/opt/ppt/jre`
has no package records, so Trivy neither scans it nor lists it in the
SBOM (#1008).

`make trivy` runs the same three gates after its filesystem scans, and
`make trivy-images` runs them alone (#1065), with the workflow's
scanners, severity, exit code and `--ignore-unfixed`. They scan the
images `make build` last produced, as `docker compose config --images`
lists them (the project name, then `-indexer`, `-mcp-server`,
`-mbsync`), and fail with a message naming the image when one is not
built; rebuild before scanning a change,
since the gates read the image, not the checkout. The full reports
have no local equivalent: run `trivy image <name>` by hand for every
severity. `scripts/tests/trivy_flags_test.sh` derives the gates from
`docker.yml` and fails when the Makefile drifts from them.

## Privacy Model

Three layers, each with its own boundary, plus the host's disk, where
everything is stored unencrypted (see At rest). The README has the
operator-facing walkthrough; the tables below are the per-operation
reference.

### Storage and processing layer (always local)

| Operation | Local only | Leaves machine |
|---|---|---|
| Email storage | ✅ | Never |
| Vector index | ✅ (SQLite) | Never |
| Keyword search | ✅ (SQLite FTS5) | Never |
| Send/Move/Flag | Disabled by default | Never |

### At rest (on the host's disk)

Bridge decrypts mail locally, and this project then keeps it as plain
files. Proton's server-side protections do not cover these copies, and
the project adds no encryption of its own:

| Data | Where | Stored |
|---|---|---|
| Every message as an `.eml` file, attachments included | `maildir-volume` | Unencrypted |
| The index: thread and message text, chunks, extracted attachment text, participants, vectors | `sqlite-volume` (`mail.db` and its WAL) | Unencrypted |
| Sync state, folder names and the Bridge certificate pin | `mbsync-state` and the Maildir | Unencrypted |
| Credentials: the Bridge IMAP password, the MCP bearer token, provider API keys | `.secrets/*.txt` in the checkout | Unencrypted (mode 600) |
| Operator configuration naming real people: source-authority rules (addresses, domains), the Bridge username | `config/authority.toml`, `.env` in the checkout | Unencrypted |

Protecting them is the host's job, which makes it a setup requirement:

- **Full-disk encryption** (FileVault on macOS). OrbStack and Docker
  Desktop keep Docker volumes inside a virtual-machine disk image, by
  default on the startup disk, which FileVault covers. The protection
  holds only where the data actually is: a Docker disk image moved to
  another drive (Docker Desktop allows it), or a checkout (with
  `.secrets/`) on another drive, needs that drive encrypted too.
  Without it, anyone with the disk can read the mailbox.
- **An unlocked, logged-in machine exposes them.** Code running as the
  operator's user, or as root, can read the volumes. This is the same
  trust condition as the MCP bearer token ("processes running as the
  operator are trusted", see Endpoint authentication). Full-disk
  encryption protects a powered-off machine, or one restarted and not
  yet unlocked at login. A screen lock does not re-lock FileVault, so a
  logged-in session that is only screen-locked is not protected by it.
- **Backups.** Any backup of these volumes, of the index
  (`make backup-index`), or of a Maildir archive
  (`docs/troubleshooting.md`), holds the whole mailbox: keep it
  encrypted (for example an encrypted Time Machine destination) and
  never inside the checkout. A backup of the checkout itself carries
  every git-ignored file, and any of them can hold private data:
  credentials (`.secrets/`, `*.pem`, `*.key`), addresses
  (`config/authority.toml`, `.env`), eval queries grounded in the real
  mailbox (`mcp-server/tests/eval/queries.json`, `eval-queries.md`),
  local Maildir or data copies (`maildir/`, `data/`) and logs. So
  protect a checkout backup like the volumes, or leave out every ignored
  file (`git status --ignored` lists them). Rotating credentials after an
  exposure covers `.secrets/`, but nothing takes back the addresses or
  the mailbox-derived queries.
- **Deleted mail.** Mail deleted in Proton stays on disk. A reap removes
  the message from the index, but its `.eml`, attachments included,
  stays in the Maildir indefinitely: mbsync never expunges
  (`Expunge None`) and the indexer's Maildir mount is read-only, so
  removing those files is undecided (#728). Below that, deleted text can
  also persist in free blocks, snapshots and backups (see "Cascade on
  message removal" and "Deletion Reconciliation" above).

Application-level encryption of the index (for example SQLCipher) is
out of scope: sqlite-vec would need to work with it, and the indexer
would need its key without a person present.

The MCP server switches off FastMCP's OpenTelemetry instrumentation at
startup (`telemetry_mode = "off"`, overriding any `FASTMCP_TELEMETRY_MODE`),
so it creates no spans and propagates no trace context even if an OTel SDK
and exporter were present.

### Embedder, reranker, and inference (operator-supplied)

| Operation | Local only | Leaves machine |
|---|---|---|
| Embedding — `EMBED_BASE_URL` points at a host-side server | ✅ | Never |
| Embedding — `EMBED_BASE_URL` points at a remote provider | Retrieval queries + indexed content | Email body chunks → provider |
| Reranking — `RERANK_BASE_URL` points at a host-side server | ✅ | Never |
| Reranking — `RERANK_BASE_URL` points at a remote provider | Retrieval queries | Candidate thread subjects, up to five changed reply subjects each, and retrieved chunks → provider |
| Q&A — `INFERENCE_MODE=openai`, host-side `INFERENCE_BASE_URL` | Retrieval local | Never |
| Q&A — `INFERENCE_MODE=openai`, remote `INFERENCE_BASE_URL` | Retrieval local | Retrieved chunks → OpenAI-compatible provider |
| Q&A — `INFERENCE_MODE=anthropic` | Retrieval local | Retrieved chunks → Anthropic-compatible provider |
| Q&A — `INFERENCE_MODE=none` (default) | ✅ | Never (intelligence tools not registered) |

> **Note on remote providers.** Pointing any of these at a remote
> provider ships data over the network: every email body chunk
> through the embedder at index time, every search-query string
> through the embedder at retrieval time, and every retrieved chunk
> through the inference and reranker endpoints. This is a deliberate
> departure from a fully-local posture; choose the provider URLs
> accordingly. To keep all retrieval traffic on the box, point each
> URL at a host-side server you install yourself. A URL is never
> chosen implicitly: an empty `{LAYER}_BASE_URL` fails startup, and
> the SDK's default remote endpoint needs the explicit value `default`
> (#750). Startup logs one `Privacy:` warning per enabled layer whose
> endpoint host is not `127.0.0.1`, `::1`, `localhost` or
> `host.docker.internal`, naming the SDK's default host for `default`.
> `make status` shows the same classification for the running
> mcp-server in its Privacy section: each layer's mode and LOCAL or
> REMOTE with the host name only, plus whether `app-net` is internal
> (the `docker-compose.hardened.yml` no-egress overlay) (#768).

> **Answer evaluation (development tool).** `make eval-answers` runs
> outside the containers and sends only the committed synthetic corpus
> (questions, retrieved synthetic passages and the answers) to the
> `INFERENCE_*` answerer and the separately configured `JUDGE_*` grader.
> It refuses any index that is not the committed synthetic corpus, so it never
> reads or sends the mailbox. See `mcp-server/tests/eval/README.md`.

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

The MCP server speaks one transport, Streamable HTTP at `/mcp`, on a
localhost-bound port. The legacy HTTP+SSE transport (`/sse` and
`/messages/`) was removed (#498); `MCP_TRANSPORT=sse` or `dual` fails
startup with migration steps. `/mcp` and `/health` sit behind the same
Host/Origin allowlist, checked before a session is created. A Streamable HTTP session idle for
`MCP_SESSION_IDLE_TIMEOUT_SECS` (default 1800 s) is ended, so abandoned
sessions do not accumulate.

### Endpoint authentication

`/mcp` requires a static bearer token (`Authorization: Bearer <token>`),
the `mcp_auth_token` Docker secret from `.secrets/mcp_auth_token.txt`
(mode 600). `mcp-server/src/main.py` `_build_app` sets a fastmcp
`TokenVerifier` as the server's auth provider, so fastmcp's `http_app`
wraps the `/mcp` route in its `RequireAuthMiddleware`: a request without
the token, or with another one, gets `401` before it reaches the
Streamable HTTP session manager, so it creates no session. The token is
compared in constant time (`hmac.compare_digest`). `/health` is a custom
route outside that wrapper and stays open for the container
healthcheck. The Host/Origin allowlist runs as before and still rejects
a bad Host (`421`) or Origin (`403`) whatever the token. Startup fails
when the token is empty, and neither the token nor the `Authorization`
header is logged. Rejections are logged with a fixed reason
(`missing_token`, `invalid_token`, `bad_host`, `bad_origin`; #878),
rate-limited to the first per reason in each 60-second window plus one
per-window count line, and
the MCP SDK's own Host/Origin warning, which quotes the raw header, and
fastmcp's per-request `Auth error returned` line are filtered out. The
`MCP_AUTH_TOKEN` environment variable is read only
when the secret file is absent, for running the server outside a
container; Compose always mounts the secret, and `validate-env` rejects
the variable in `.env`.

**Trust condition.** The loopback-only port keeps other machines out.
The token adds a boundary against other local accounts and against web
pages in the operator's browser, which cannot read the mode-600 file.
It does not separate the operator from code running as the operator's
own user: any such process (a malicious package, a compromised tool)
can read the file and call the endpoint. The model is therefore
"processes running as the operator are trusted". Reaching the server
from another device or from a hosted connector would need its own
design (a private-network gateway or an OAuth resource server; PLAN.md
Resolved decisions 13) and is not supported.

## Inference Mode Toggle

Set `INFERENCE_MODE` in `.env`:

- `none` (default) — the intelligence tools are not registered and
  nothing is sent to an inference provider.
- `anthropic` — Anthropic-compatible Messages API via
  `INFERENCE_BASE_URL` (`default` for api.anthropic.com). Retrieved
  email chunks are sent to that provider.
- `openai` — OpenAI-compatible chat completions via
  `INFERENCE_BASE_URL`. Point this at a remote provider or at
  a host-side server you install yourself; only the latter keeps
  retrieved chunks on your machine.

The toggle applies per-deployment. A per-session toggle is on the roadmap.

# Architecture

## Overview

protonmail-local-ai is a containerised, privacy-first AI search and
intelligence layer for ProtonMail. The official Proton Mail Bridge app
runs on the host (see [Bridge](#bridge)) and three containers (mbsync,
indexer, mcp-server) run beside it; storage, sync and the index never
leave the host, while the text sent for embedding, inference and
reranking goes to whichever endpoints the operator configures.
Embedding and inference are operator-supplied
external dependencies — the project itself ships no model-serving
components, and whether they run on the host (LM Studio, vLLM,
`mlx_lm.server` for inference only, etc.) or against a remote provider is a deployment
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
  - Polls Bridge IMAP SYNC_INTERVAL seconds after each sync ends, in a
    bounded retry loop;
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
    volume); refuses to sync on mismatch. A legitimate rotation is one
    `make rotate-bridge-pin` run, a one-shot container that may replace
    the pin and syncs nothing (#1609)
  - Fails closed if cert extraction or repeated sync attempts fail
  - After each successful sync, writes a last-sync stamp
    (`.mbsync-last-sync.json`) at the Maildir root. A sync whose only
    errors are Proton folders it can no longer open (renamed or deleted
    upstream; the local copy is kept) counts as successful, with a
    warning that gives a count, not folder names (#276). Other isync
    messages that name a folder or a Maildir path are logged with
    `<folder>` or `<path>` in its place (#570), matched under the
    configured channel and store names; a channel line it cannot match
    is cut whole (#1498)
  - Before and after every sync attempt, publishes its collection status
    (`.mbsync-collection-status.json`) at the Maildir root: the latest
    attempt, the last terminal outcome with its withheld far-side folder
    count and failure class, and the last success, as counts and fixed
    tokens (#1592; see [Collection status](#collection-status))
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
  - Records mbsync's last sync and its own liveness in `ingestion_state`,
    and mbsync's latest collection outcome in `account_collection_state`
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
                                   ingestion_state,
                                   account_collection_state
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
| `maildir-volume` | Raw email in Maildir format | Optional for mail still in Proton, which mbsync can re-sync. Mail and folders deleted in Proton are kept only here (`Expunge None`), so back it up if you rely on them, as archive mode does |
| `mbsync-state` | mbsync's pinned Bridge certificate fingerprint and durable success-generation counter (*Index currency*) | No — the fingerprint is re-pinned from `BRIDGE_CERT_FINGERPRINT`, and the generation counter self-seeds forward from the retained stamp in `maildir-volume` on next use |
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
  does not. Labels are left out by project policy, not by a Bridge
  restriction: the index does not model label membership apart from
  physical folders, so a label copy would add duplicates it could not
  describe; labels are to come from message metadata (#1313), never from
  syncing these folders. A custom folder named `Starred` is
  `Folders/Starred` and syncs. The exclusions are the
  `PROTON_FOLDER_EXCLUSIONS` setting, which may add folders but is
  refused without these three (see "Bridge" below).
- **Names isync needs for itself:** a child folder named `uidvalidity`,
  `isyncuidmap.db`, `mbsyncstate`, `mbsyncstate.journal`, `mbsyncstate.new`
  or `mbsyncstate.lock` would be one of those files, so the channel's
  `Patterns` leave it, and everything below it, out. It is not synced and
  nothing reports it.
- **File mtime:** `CopyArrivalDate yes`. A file mbsync writes gets the
  message's IMAP INTERNALDATE as its mtime (#1081); see
  [Message time](#message-time) for what that is and is not.
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
  host through `host.docker.internal` (or another name `BRIDGE_HOST`
  accepts; see [Bridge](#bridge))
- `app-net` for `indexer` ↔ `mcp-server`. Both reach the
  operator-supplied embedder, inference, and (optional) reranker —
  either at remote URLs or, when the operator runs a host-side server,
  via `host.docker.internal`.

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
Proton to a searchable index passes through seven states, each reported
by a different signal, and each later state depends on the earlier
ones (#274). The last row, Bridge's own currency with Proton, is not
observed at all (#1317, #1592):

| State | Meaning | Reported by | Not proven by it |
| --- | --- | --- | --- |
| Bridge listening | Something accepts TCP on Bridge's IMAP port | mbsync's `Bridge IMAP port is reachable` line; it covers only mbsync's startup (the Bridge app runs outside Compose, so there is no Bridge health check) | TLS, a logged-in account, any sync |
| TLS handshake OK | Bridge completes implicit TLS with a certificate that matches mbsync's pin | mbsync startup: certificate extraction, `BRIDGE_CERT_FINGERPRINT` and the pin. A failure stops mbsync with a named error before any credential is sent. mbsync's health also requires the extracted certificate, so a healthy mbsync passed this on its current start | That isync accepts the certificate: its own validity and host name checks run on each sync, so an expired certificate that still matches the pin shows as failed syncs. A logged-in account, any sync |
| Bridge authenticated | An account is logged in to Bridge and accepts mbsync's `LOGIN` | No dedicated signal. Bridge listens, completes TLS and greets with no account logged in. The first proof is a successful sync; a rejected login is a failed sync in mbsync's log | — |
| mbsync syncing | mbsync's sync loop is alive | mbsync container health (liveness: a heartbeat touched around every attempt, or a sync or its permission repair running, up to the run's deadline) | That any sync succeeded: the loop is healthy between failed attempts until five consecutive failures exit it and Docker restarts it |
| Last successful sync | mbsync completed a sync | The success stamp `.mbsync-last-sync.json` at the Maildir root, written by mbsync. `get_mailbox_status` cannot read Maildir; its `last_sync_at` (and the reason `no successful mail sync has been recorded` or `last successful mail sync was ... ago`) is the last sync the indexer has acknowledged, after queuing that sync's mail, so it can lag the stamp | The stamp alone: that the indexer has read it. `last_sync_at`: that the queued mail is indexed yet |
| Latest collection outcome | How mbsync's latest attempt to collect from the Bridge app ended: succeeded, succeeded with far-side folders it could not open (a count), or failed (a class) | mbsync's collection status file, recorded by the indexer and reported as `get_mailbox_status` `collection` (see [Collection status](#collection-status)) | Which folders were withheld (names are never recorded). That mbsync is running: an attempt `in_progress` only says it started and nothing ended it. Anything about Bridge's own state |
| Index current | The indexer has acknowledged a recent sync, has reported within the last 10 minutes, has no pending, retrying, deferred or extraction-deferred jobs (parked trashed files do not count), and no failed collection waits for a later success delivered to it | `get_mailbox_status` `current` and its reasons (see [Index currency](#index-currency)); `make status` prints the same fields | That every message is indexed: dead-lettered jobs (the `dead` count) do not affect `current`, and their messages may be missing from search until `make requeue-dead`. Nor mail that reached Proton after the last sync |
| Upstream currency | Bridge has synchronized with Proton and serves current, complete content | Nothing: `get_mailbox_status` `upstream_currency` is always `unknown`. Bridge exposes no supported signal for it, and mail Bridge never delivers or delivers degraded is invisible here (#1317) | — |

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

The Bridge stays on this Mac (PLAN.md decision 46). `SOURCE_MODE`,
`BRIDGE_HOST`, `BRIDGE_IMAP_PORT`, `BRIDGE_CERT_HOST`,
`PROTON_FOLDER_EXCLUSIONS` and isync's
account, store and channel names are settings, each defaulting to the
value mbsync used before it was one, checked by one implementation,
`mbsync/proton-settings.sh`,
at two sites: `scripts/validate-env.sh` and the entrypoint's
`require_prerequisites`, before anything is rendered or connected. A
refused value stops both with fixed text that names the setting, never
the value.

- `BRIDGE_HOST` (Compose passes it empty by default; empty means
  `host.docker.internal`) is compared without surrounding whitespace,
  in lower case, against an exact list of names that forward to this
  Mac's `127.0.0.1`: `host.docker.internal`, and OrbStack's
  `host.orb.internal` and `host.internal`. A name is listed only with
  `mbsync/tests/host_alias_check.sh` (`make test-mbsync-host-aliases`,
  run on the Mac, not in CI) showing it reaches a listener bound to
  `127.0.0.1` alone; all three passed on OrbStack. Anything else
  (`gateway.docker.internal`, an IP address, a LAN or `.local` name, a
  suffix or trailing dot) is refused naming
  [#1499](https://github.com/marshalltech81/protonmail-local-ai/issues/1499),
  the decision on a Bridge elsewhere. The name is not resolved to check
  it: DNS cannot show that a name is local, so the container engine and
  its resolver are trusted to send it to this Mac's loopback.
- `BRIDGE_CERT_HOST`, the name rendered as `Host` (empty means
  `127.0.0.1`), has its own exact check, as written: `127.0.0.1`,
  `localhost`, another `127.0.0.0/8` address in canonical dotted
  decimal, or `::1`. The app's own certificate names only `127.0.0.1`,
  so the others need a CA certificate issued for the name and imported
  into the app. `tls_check.sh` shows isync accepting such a certificate
  for each of `localhost`, `127.0.0.2`, `127.255.255.254` and `::1`, and
  refusing a certificate for `127.0.0.1` under it before `LOGIN`.
  Certificate extraction (`openssl s_client`) does no name check: there
  the expected fingerprint and the pin check the certificate, and isync
  checks the name on each sync. The host name pattern in
  `validate_bridge_endpoint` still checks `BRIDGE_HOST`; for
  `BRIDGE_CERT_HOST`, which is never connected to, it is the exact check
  (`::1` holds colons).
- `PROTON_IMAP_ACCOUNT_NAME`, `PROTON_REMOTE_STORE_NAME`,
  `PROTON_LOCAL_STORE_NAME` and `PROTON_CHANNEL_NAME` default to the
  names the template always held (`protonmail`, `protonmail-remote`,
  `protonmail-local`, `protonmail`) and take 1 to 64 of
  `A-Z a-z 0-9 _ -`, starting with a letter or digit, the two stores
  different. The output filter that keeps folder names out of the log
  matches the configured channel and far store names as literal text,
  and cuts any channel line it cannot match (#1498). Each folder keeps
  its sync state in its own directory (`SyncState *`), so renaming them
  downloads nothing again (`layout_check.sh` check 9).
- `SOURCE_MODE` (empty means `proton-bridge`) is trimmed and lowercased,
  as every `*_MODE` setting is, and checked first: `proton-bridge` is
  the only value accepted. It is read once at startup; no control,
  sourced file or function name depends on it, so it selects nothing.
- `BRIDGE_IMAP_PORT` (Compose's default `1143`) must be a port from 1 to
  65535 without leading zeros; `validate_bridge_endpoint` keeps its own
  check of it as well.
- `PROTON_FOLDER_EXCLUSIONS` (empty means `All Mail|Labels/*|Starred`)
  is a list of folder patterns separated by `|`. The whole value is
  checked first: of ASCII, only `A-Z a-z 0-9`, spaces,
  `. / _ * % & + , ( ) @ -` and the `|` separator, with no empty pattern
  (a leading, trailing or doubled `|`). Non-ASCII characters are
  accepted, because isync 1.5 matches `Patterns` against folder names
  decoded to UTF-8 (`layout_check.sh` check 10 excludes a folder named
  in UTF-8). Each pattern must then have no space at either end, and
  `All Mail`, `Labels/*` and `Starred` must each be one of them: these
  stay excluded whatever else is added (see "Virtual folders" above).
  Each pattern is rendered as a quoted `!"…"` exclusion, in order, by
  parameter expansion alone (no `eval`, word splitting or globbing),
  before isync's own layout exclusions, which stay last. Quotes,
  backslashes, `!`, `$`, `#` and control characters are never accepted,
  so a pattern cannot end isync's quoting or re-include a folder, and a
  `.env` line Compose would read differently from `validate-env.sh` is
  refused. `layout_check.sh` check 10 shows an added exclusion leaving
  its folder out. An exclusion only stops further syncing: mail synced
  from the folder before it stays in the Maildir and the index
  (searchable, its text already sent to the embedder) until the operator
  removes the folder from the Maildir, after which isync does not create
  it again and the deletion reconciler handles its messages
  ([troubleshooting.md](troubleshooting.md#remove-mail-from-a-folder-you-have-just-excluded)).
  Nothing removes local mail automatically when a pattern is added.

The startup identity hash includes all of these settings, each as
resolved (an unset `SOURCE_MODE` and `proton-bridge` hash alike), except
that `PROTON_FOLDER_EXCLUSIONS` enters it as `default` or as its number
of patterns: an added pattern names a folder, which is mailbox content.

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

`BRIDGE_CERT_HOST` therefore defaults to `127.0.0.1`, and with the defaults the
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
extracted and checked against `BRIDGE_CERT_FINGERPRINT` and the
persistent pin (isync also loads the
system CA store, and public CAs do not issue certificates
for `127.0.0.1`), so a different
certificate at that address is refused at startup, by the fingerprint
and the pin, and by isync's chain check on every sync. `mbsync/tests/tls_check.sh` (`make
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
DeepInfra, OpenRouter, LM Studio, vLLM, TEI —
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
exposes none of the first three, and the chunk configuration hash and
label are not recorded yet. This is the first slice of the `vector_generations`
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

#### Account registry

The index records the account it serves (#1596, PLAN.md decision 47),
so a changed or lost account setting is caught at startup instead of
mixing two accounts' mail under one identity. `accounts` holds one row
per account, today exactly one: `scope_uuid` is `ACCOUNT_SCOPE` from
`.env` (a canonical lower-case UUID, not a secret and not a Proton
identifier), `provider` is the normalized `SOURCE_MODE` (only
`proton-bridge`), and `created_at` is the registration time, never a
collection observation. Triggers abort any `UPDATE` or `DELETE`, so a
row is never changed or removed: a different UUID is a different
account, never a rename.

`make init-secrets` (which `make up` runs) writes a random UUID to
`.env` when the `ACCOUNT_SCOPE` line is missing or empty, and never
replaces a value. `scripts/validate-env.sh`, the indexer and mcp-server
each refuse a missing or malformed value, and `SOURCE_MODE` under the
rule of `mbsync/proton-settings.sh`, with fixed text naming the setting
and the fix, never the value. Compose passes `${ACCOUNT_SCOPE}` bare, as
a required setting (`indexer/src/account.py`,
`mcp-server/src/lib/account.py`). mbsync gets it too and refuses a
missing or malformed value the same way: it names the account in its
collection status (#1592; see *Collection status*).

A fresh index inserts the row in its initial-schema transaction. An
existing index binds it in the v14 migration: the indexer registers
connection-local SQL functions holding the configured values before the
migration runner starts, so the row commits with the version stamp or
not at all. On every open the indexer (`Database`) and mcp-server (at
startup, through its `mode=ro` / `query_only` connection) require
exactly one stored account equal to the configured `(ACCOUNT_SCOPE,
SOURCE_MODE)` pair, and otherwise exit with fixed text naming no value
(docs/troubleshooting.md, "Account scope mismatch"); mcp-server also
exits, and its restart policy retries, while the indexer has not yet
created or migrated the index. The Maildir root is not part of the
account: moving it keeps the index.

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
`127.0.0.1` only. Containers reach them via `host.docker.internal`
(OrbStack and Docker Desktop) — this is the project's expectation. Binding a
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
    │                     accumulated body (capped)
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

**Evidence selection (#215, #858, #1246).** With evidence requested,
each surfaced thread's passages are chosen from all of its chunks, not
from the lanes' hits: the lanes keep one row per thread and do not
record which chunk matched. An FTS5 lookup scoped to the surfaced
threads finds which attachments' filename or MIME type match the
query. The threads' chunks with a valid vector (the candidates) are
then fetched with their text, and the query's words are matched
against that text alone: it goes into a contentless FTS5 table with
the chunk index's tokenizer (`porter unicode61`) on a private
in-memory connection, so a word matches a candidate exactly when it
matches that chunk in `message_chunks_fts`. Neither the mailbox index
nor `bm25()` is read, so the work grows with the surfaced threads'
chunks, not with the mailbox or its index segments (#1262).

The query's words (the units the FTS sanitizer quotes) are
deduplicated by their token sequence, so `Invoices` and `invoice`
count once. Within each thread, a word's frequency is how many
candidates hold it; a word every candidate holds is ignored. A
candidate holding any query word is keyword-matched; it ranks by the
frequencies of the words it holds, rarest first, compared as a list
padded to 16 entries with one more than the thread's candidate count,
then by vector distance, then by chunk ID. So the passage holding the
question's rarest word (an invoice number, a name) wins over nearer
passages holding only its common words, and a thread whose matches
are all uninformative keeps the nearest one. Only the first 16
distinct words are ranked; later ones still make a candidate
keyword-matched, and a query with more logs a rate-limited WARNING
and `keyword_units_unranked` on the timing line. Each thread's slice
is then, within its per-thread limit:

1. the first chunk of a matched attachment, if one matched;
2. the best-ranked keyword-matched chunk, unless it is the chunk in
   slot 1;
3. the rest: the matched attachments' other chunks, then the thread's
   other attachment chunks, then body chunks, each group by vector
   distance (by vector distance alone when no attachment matched).

Each passage records why it qualified as `selected_by`
(`keyword_match`, `attachment_match` or `vector`). A failed keyword
ranking keeps the order without slot 2.

The rerank stage is best-effort: a transient rerank-service failure
returns an empty result set from the reranker, and `hybrid_search`
falls back to RRF order truncated to the caller's `limit`. A ranking
with an out-of-range or repeated index falls back the same way. A rerank
outage degrades quality without failing the whole query.

Each candidate is sent to the reranker as `Subject: <thread subject>`,
then one `Reply subject: <subject>` line per distinct message subject
that differs from the thread's after reply-prefix normalization (a
reply that changed the subject), then its first evidence passage
(Evidence selection, above) or, without evidence, its snippet. The added subjects are read from `messages`
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
   The sender and the recipient must each appear in a message of the
   thread whose sender is assessed unambiguous, and a message with an
   ambiguous sender never joins by subject (see *Source authority*).
   The 10 most recently active same-subject threads in the folder are
   tried, newest first.
   Subjects are compared as stored, cut to `SUBJECT_MAX_CHARS` (2,000)
   characters (#541): two subjects that agree on their first 2,000
   characters match, and a `Re:` reply to a longer subject does not.
   Only crafted mail has such subjects
3. Each message's body is sliced into paragraph-packed chunks
   (`indexer/src/chunker.py`); each chunk is FTS-indexed and gets its
   own vector embedding stored in `message_chunks_vec`
4. The thread's vector in `threads_vec` is the **L2-normalized mean**
   of all its chunk vectors, body and attachment chunks alike (attachment
   weighting: #288) — coarse and precise retrieval derive from
   the same source, and storing a unit-norm vector lets cosine
   similarity downstream collapse to a dot product
5. New messages joining a thread emit new chunks (idempotent diff
   write keyed on deterministic chunk IDs) and rewrite the parent
   thread's vector

The indexer writes a thread in two phases. **Phase 1** commits the
thread row, `message_thread_map` entry, and `indexed_files` row in a
single `upsert_thread` call, with a seed thread vector chosen from a
three-case priority chain (chunks-mean of any pre-existing chunks,
derived from the thread's chunk-vector sum, preserved non-zero prior
`threads_vec` row, or a placeholder zero).
Threading state is durable at this point so the next message in the
batch sees this thread when computing its own assignment. **Phase 2c**
then commits the body chunks (`message_chunks` + `message_chunks_fts`
+ `message_chunks_vec`), attachment occurrences, attachment-extraction
cache rows, attachment chunks, and the final normalized thread vector
in one per-message transaction. The two phases are separate
transactions so a crash between them leaves a thread row + message map
+ indexed-file marker with the seed vector but no chunks or attachment
rows yet — the queue re-runs the message (Phase 1 again,
idempotently, then Phase 2) on the next pass and the seed-vector chain
converges. Within each phase, chunk and attachment
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

### Thread vector sums

A thread's vector is the mean of its chunk vectors, and it is derived
without reading them (#1356). `thread_vector_sums` keeps, per thread,
the exact sum `S` of the stored chunk vectors (the `message_chunks`
rows with that `thread_id` that have a `message_chunks_vec` row) and
their count. Each component is an integer in units of 2⁻¹⁴⁹, the
smallest float32 subnormal: every stored float32 value is a whole
number of those units, so the sums are exact and never drift, whatever
order the writes come in (`indexer/src/vector_sums.py`).

- **Updates.** Every write of chunk vectors applies `S += new − old` and
  the count change in its own transaction: `replace_message_chunks`
  (a slice's inserted and deleted chunks; a deleted chunk counts
  against its own row's thread) and `_delete_chunks_in_batches`, which
  every other chunk deletion uses (a reap's removed messages, a dropped
  attachment occurrence's slice). Deleting a whole thread drops its row. A
  new thread starts with an empty sum. Moving a message to another
  thread in `message_thread_map` does not move its chunk rows, so it
  changes no sum. A write that changes no chunk reads no vector.
- **Derived vector.** One rule, used by Phase 1, Phase 2c, the reaper
  and the check: each component of the mean, `S_i / (count · 2¹⁴⁹)`,
  is the exact quotient rounded once to float64 (ties to even), then
  the vector is L2-normalized in float64 and stored as float32 (round
  to nearest, ties to even). A thread with no chunk vectors keeps its
  chunkless fallback (the prior vector or the subject embedding).
  Phase 1's seed, Phase 2c's mean and the reaper's survivor mean read
  no chunk vector of the thread; the reaper subtracts the reaped
  messages' vectors and derives the survivors' mean in the reap
  transaction.
- **Inline fill.** A thread from before schema v10 has no row. The first
  write or read that touches it computes its sum and count from its
  chunk vectors in that transaction: one thread-wide read in the
  thread's lifetime.
- **Backfill.** Each main-loop tick fills up to 50 threads without a
  row, stopping once 2,000 chunk vectors were read (a thread is read
  whole), one transaction per thread. Progress is the rows themselves,
  so an interrupted sweep resumes. Each batch that fills something logs
  `thread vector sums backfill: filled=<n> vectors_read=<n>
  remaining=<n>`, and the sweep logs once when every thread is filled.
- **Check.** Every reconciliation interval
  (`INDEXER_DELETION_SWEEP_INTERVAL_SECS`, in archive mode too), the
  next filled threads (same bounds, in `thread_id` order, wrapping) are
  recomputed exactly from their chunk vectors and compared with the
  stored row. One that differs (or cannot be read) is repaired, with
  its thread vector, in one transaction, and a WARNING counts the
  repairs: a repair means some write missed its sum.
- **Encoding** (`encoding_version` 1). An all-zero sum is the empty
  blob; otherwise each component is `varint(k)`, `varint(n)` and `n`
  bytes of `m` (signed big-endian), where the component is `m · 2ᵏ`
  with `m` odd. Measured in the indexer image (Linux) on synthetic
  unit vectors, plain timing: a row is about 22 KB for one vector,
  27 KB for 100 and 29.5 KB for 1,000 or the part cap's 9,990 (a
  stored thread vector is 16 KB), so the table adds about 1.4–1.8 times
  the size of `threads_vec`. Converting one stored vector takes
  0.2 ms, adding it 0.1 ms, and encoding, decoding or deriving the
  thread vector under 1 ms each. On a 9,990-chunk thread a
  continuation pass's slice write and thread mean took 4.8 ms, against
  2.8 s for one thread-wide read and mean before (each pass did two);
  the one-time inline fill of such a thread took 5.3 s.

The stored `body_text` on `threads` still feeds FTS5 with the thread's
accumulated text, quoted text and signatures included (users
legitimately search them): each message's first 2,000 characters
(`PER_MESSAGE_BODY_CAP_CHARS`) in arrival order, cut to 4,000 tokens
(`THREAD_BODY_TEXT_MAX_TOKENS`), so a message added after the thread
reaches that cap adds nothing to it. A reap that rebuilds a thread
rebuilds this text from the survivors in date order, so after a reap
the cap keeps the oldest survivors' text. Chunk inputs are first passed through
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
and falls back to a vector of the thread's stored subject (its oldest
message's, `display_subject`).

**Message body assembly (#295, #298):** a message's body text is every
non-blank inline `text/plain` and `text/html` part outside attachments
(HTML through html2text, in the extractor child: "HTML conversion"
below), in document order, separated by a blank line.
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

Chunk IDs are `sha256(message_pk || index || chunk_text)`, the parts
NUL-separated and the index in decimal (`chunker._chunk_id`) — the same
body always produces the same ID set. The indexer's per-message chunk
write diffs the new chunk IDs against stored IDs, embeds only the new
ones, and deletes any that are no longer present. Re-running on
unchanged input with the same chunk budgets (`INDEXER_CHUNK_*`) is
therefore zero embed cost; a chunk that changed budgets cut
differently gets a new ID and is embedded. A body chunk's
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
file's size and mtime, and its path, which names the message in log
lines and the `indexed_files` record and locates its occurrence.
Everything below the parser
(threading, chunking, extraction, the database writer) sees the
`Message`, so another source (an mbox import, say) would plug in at
`parse_email_bytes` with its own adapter. Read state is the exception:
the writer takes it from the Maildir flags in the file's path
(`maildir.message_state`) onto its occurrence, and
`SourceMetadata.flags` is not read yet.

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
time, #541), `sent_at` (`Date:` header, NULL when it is missing or
unparseable) with `sent_at_status`, `occurred_at` (the top `Received:`
header's date, or NULL), `first_indexed_at` (see Message time),
`in_reply_to` /
references, attachment flag, and its bytes' `size_bytes` and
`content_hash` (SHA-256 of the raw `.eml`). Its folders, file locators
and read state are its copies' (see *Mailbox occurrences*; v17 dropped
the row's own `filepath`, `folder`, `seen`, `flagged` and `replied` and
`message_thread_map.filepath`, #1568). The MCP server returns the
representative copy's locator with the hash and size as `source_file`
on every message, evidence chunk, and attachment result (see
`docs/mcp-tools.md`). `message_participants`
normalizes From / To / Cc into one row per (message, role, address),
with `address` canonical and lowercased and the display name kept as
written; malformed entries with no recoverable address are skipped.
The row's `name` is the display name of the address's first
occurrence in that role (NULL when that occurrence has none). When the message writes the address under more
than one name (twice in one header, or again under a later role), every
distinct decoded name is kept in `message_participant_names`, one row
per (message, role, address, name), original casing kept and exact
duplicates once (#1140). Name matching (`sender` / `recipient` /
`participant` substrings and their matched-address report),
`find_contact` and entity aliases read that table, each name on its
own, so no match spans two names; display and per-passage attribution
keep the row's first name. The first name of each (role, address) is
always kept; every further one spends one per-message budget of
`MAX_EXTRA_PARTICIPANT_NAMES` (1,000) names and
`MAX_EXTRA_PARTICIPANT_NAME_BYTES` (64,000) UTF-8 bytes, and a name
past it is dropped and counted as the parser cap `participant_names`
(see `docs/troubleshooting.md`).

`messages.participant_names_complete` records, per message, whether
that name stage kept every distinct name: `1` when it finished within
the budget, `0` when the budget dropped one, `NULL` for mail not yet
reparsed since the v4 upgrade (and for a dead-lettered message until
`make requeue-dead`). It is written in the same transaction as the
participant and name rows. It covers the name stage only: whether
the participant rows hold every address is the per-role completeness
below. A `sender`, `recipient` or `participant` filter given as a name
or fragment is decided by a match on a stored address or name; a
message it does not match counts as a miss only when the flag is `1`,
and is otherwise unknown (`indeterminate` in `query_messages`). A full
address is matched exactly and never reads the flag. `find_contact` and
its aggregators list and match only the stored names, so until the
reparse reaches a message they see its first names only.

**Completeness**
([#1086](https://github.com/marshalltech81/protonmail-local-ai/issues/1086)).
A filter that finds nothing in a message's stored content can only say
"no" when that content is complete, so `messages` records it per
message, each column `1` complete within the documented indexing
semantics, `0` a known loss, `NULL` not assessed, with no default:

| Column | `0` when | Written |
|---|---|---|
| `subject_complete` | the subject was cut (`subject_length`) | phase 1, with the row |
| `from_addresses_complete`, `to_addresses_complete`, `cc_addresses_complete` | an `address_*` parser cap fired while that role's headers were read, or an entry yielded no storable address (`address_unparsed`); all three when the header scan stopped (`address_fields`); From also for a repeated `From`, whose later headers are not parsed | phase 1, with the row |
| `attachments_manifest_complete` | a cap stopped the walk or left an attached email unwalked (`attached_depth`, `attached_fields`, `transport_decode`, `transport_lossy`, `decoded_bytes`, `container_serialize`, `mime_parts`, `delivery_status_parts`) | phase 1, with the row |
| `body_complete` | text parts were left out of the body (`body_parts`, `html_body`, `mime_parts`); not yet detected, so still `1`: a body text part whose transfer decode lost bytes (#1360) and a `multipart` part the parser could not split (#1348) | phase 2c, in the transaction that commits the body chunks |

`_write_message_record` writes the phase-1 columns from the parse and
resets `body_complete` to `NULL` on every write, and
`Database.set_body_complete` sets it in phase 2c beside
`replace_message_chunks`, so it describes the committed chunks: `NULL`
while a message is between the phases, after a phase-2 failure (the
transaction rolls it back with the chunks) and for a message
dead-lettered there. `caps_json` holds the parse's nonzero
`PARSE_CAPS` counts as a JSON object of the fixed names to integers;
the writer refuses any other name or value, so no content can reach
it. The parser adds no new parsing for this: it attributes the
existing cap counts to the role being read and counts the entries
`_parse_addrs` already dropped as `address_unparsed`. The existing
flags (`sender_ambiguous`, `participant_names_complete`, `size_bytes`
and the clocks) record other facts and are unchanged. Attachment text
completeness is recorded per occurrence instead (below).

**Attachment text completeness**
([#1242](https://github.com/marshalltech81/protonmail-local-ai/issues/1242)).
`attachments.text_complete` records whether an occurrence's committed
attachment chunks hold all its text (`1`), whether text was lost or the
occurrence never certifies absence (`0`), or that it is not assessed
(`NULL`); `attachments.text_extractor` is the extractor stamp of the
result that applied (`docx@7`; `NULL` when no extractor ran). Both are
written by `apply_attachment_writes` in the phase-2c transaction that
writes the occurrence's chunks, so they roll back with them.

- `0` for a `failed`, `unsupported` (including "OCR disabled") or
  `too_large` result, and for a container attachment whose body was not
  serialized (`Attachment.payload_complete`: `_attachment_payload` kept
  the empty payload after a parse cap, a failure, or for a container
  nested inside another attachment), and for a part whose base64
  decode lost bytes (an invalid-character or invalid-length defect), for
  a quoted-printable part with an `=` that is neither a two-hex-digit
  escape nor a soft line break, and for a uuencode part (all four
  aliases) whose decode fell back to the transport text (#1288; one
  linear scan of the part's text, behind the same per-message byte
  budgets) or whose last non-blank line is not `end` (#1402: a body cut
  off before its `end` line, or with text after it that the decoder
  never reads; only the last 1,024 characters are read, so a trailing
  blank run or a last line longer than that also counts. A second
  complete block still ends with `end` and is not detected. Any other
  truncation that leaves valid encoding stays undetectable).
  For
  a base64 attached email, whose transport form the parser decodes
  leniently, the same text is decoded once more through the stdlib leaf
  decoder only to read those defects (one linear pass behind the
  decodable-bytes budget; the bytes kept are the lenient decode's).
  It also counts as lost when the parse dropped a line of the
  transport text it rebuilds from (a first line starting with
  whitespace, read as a header continuation, or with `From `, read as
  the mbox envelope; review round 8 on #1311).
  An attached email's loss found this way is also counted as the
  `transport_lossy` parse cap, so it is logged (review round 7 on
  #1311); a leaf attachment's is not.
  A quoted-printable attached email (`message/*`) keeps its lenient
  decode and is `0` (also counted as `transport_lossy`) when its
  transport text has an `=` that is neither an escape nor a soft line
  break (#1288), and one in any other
  transfer encoding that is not identity or base64 (uuencode and its
  aliases, or an unknown value) keeps the empty payload, counted as the
  `transport_decode` parse cap when an extractor reads the attachment:
  none of its transport text is extracted, its result is the `empty`
  one an unserialized container gets, and the message's
  `attachments_manifest_complete` is cleared (review round 4 on #1311).
  An email carried as a leaf part the `eml` extractor reads
  (`application/eml`, or any type named `.eml`) in an encoding nothing
  here decodes (not identity, base64, quoted-printable or uuencode)
  keeps its decode but is always `0`, and a leaf of any type whose
  base64, quoted-printable or uuencode decode lost bytes is `0` too;
  each is counted as `leaf_transport_lossy` (review round 8 on #1311,
  #1288), which leaves the
  attachment list complete, since a leaf is never walked for
  attachments. An attached email's `transport_lossy` clears it: a
  nested attachment whose boundary was lost is missing (round 13).
- For a `success` or `empty` result, the result's own
  `text_complete`, which the dispatcher sets: `0` when the attempt lost
  text (any `extractor_caps` cap, the `max_extracted_chars` cut, the
  PDF digital-page cap, a PDF page no reader recovered, the PDF or
  image OCR page cap, a PDF page under the digital-text floor while OCR
  is off, or a failed OCR fallback that kept the digital text). Each
  loss calls `extractors.note_text_lost` on the running attempt. The
  value is cached with the result (`attachment_extractions.text_complete`),
  so an occurrence served from the cache or from its batch gets it too.
- `NULL` when the result came from an older version of its extractor,
  or from a cached `-ocr` row with no record served while OCR is off.

A cached `success` or `empty` row with no record (every row cached
before schema v6) is not served: it is re-extracted once
(`attachment_indexing.completeness_unrecorded`), and the fresh result
always carries a record, so the row is served from then on (#1285). An
`-ocr` row is the exception while OCR is off, as for a stale row: a
refresh could only replace its text with "OCR disabled". Once OCR is on,
the startup sweep re-queues the messages using such a row and counts
them on its `re-queued` line.

At startup the extraction sweep first clears `text_complete` to `NULL`
on every occurrence whose `text_extractor` is an older version than
`EXTRACTOR_VERSIONS` (whatever the OCR or extraction setting, and for
dead-lettered messages too) and logs the count; only a later commit of
the occurrence's chunks sets it again. The 7-day retry of a `failed`
row is unchanged. `attachment_text_words` reads the occurrence flags:
a stored hit decides true; a miss decides false only with a complete
manifest and complete committed text for every occurrence. Otherwise
it is indeterminate.

The MCP leaves read the per-message `messages` flags above
(`docs/mcp-tools.md`, "Filter predicates"): a stored match decides a leaf; finding nothing decides it
false only under `1`, and `query_messages` counts the message as
`indeterminate` otherwise.
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
exception type (#942). Header bytes sent raw, without an encoded-word
(labelled `unknown-8bit`, which no codec names), are decoded as UTF-8
with no log line when they are valid UTF-8; otherwise with replacement
characters and one rate-limited WARNING per chunk (#1147). An
encoded-word beside raw bytes in Subject or the From fallback is kept
as sent, with its own rate-limited WARNING (#1186). A filename whose encoded-words still do not
decode is kept as sent, with
one rate-limited WARNING naming the exception type, and one that
decodes to nothing is kept as sent too, so the part stays an
attachment. Filenames stored before this are corrected only when their
message is re-indexed.

**Read state.** Each occurrence's `seen`, `flagged`, `replied` and
`trashed` are the `S`, `F`, `R` and `T` flags in its file's `:2,<flags>`
suffix (`maildir.message_state`, built on the same `parse_flags` as the
`T` trash check; other letters are ignored, and a file without the
suffix is unread). mbsync is pull-only, but it mirrors a read, star or
reply made in Proton by renaming the file (`:2,` → `:2,S`, and `new/` →
`cur/`), so the flags are written wherever the occurrence's path is: by
the ingest binding from the parsed file's path, and by an observed or
verified rename in the same `UPDATE` as the new locator (see *Mailbox
occurrences*), so a state change is one row update with no reparse or
re-embed, and the state cannot disagree with the stored path.
Thread-level questions (`list_threads(filter_type="unread")`) are
answered from these rows at query time, per copy; nothing per-thread is
stored. `threads.folder` stays the folder the thread's first message
was filed in when it was first indexed: a representative label, not
membership.
An index on `(address, role)` makes "every message from / to X" an
exact indexed lookup — the basis for exhaustive enumeration, as
opposed to relevance search. The MCP server's `query_messages`
enumerates over these tables (count plus keyset pages ordered by
`(effective_at, claimant_id)`), and `find_contact` aggregates
`message_participants` instead of parsing each thread's participant
JSON.

`query_threads` evaluates the same complete expression per claimant, then
groups by `messages.thread_id`. It returns definite matching threads with
separate undecided-message and unknown-only-thread counts. One grouped SQL
statement reads each page and its counts in one snapshot; the thread-ID
keyset order is stable while membership changes between pages remain visible
limitations. Whole-thread metadata includes out-of-query messages and Trash;
participant counts and attachment absence require complete source evidence.

Both are written inside `upsert_thread`'s transaction, after the
message's `message_thread_map` row. `messages` references
`message_thread_map`, `message_participants` references `messages`,
and `message_participant_names` references its
`message_participants` row, all `ON DELETE CASCADE`, so every existing removal path — reaper,
whole-thread delete, rebuild — cleans them up without separate code.

Every message-level filter the tools accept (`sender`, `recipient`,
`participant`, `subject`, `text`, `folder`, the effective-time bounds,
the attachment, read, flagged and replied flags, the size bounds,
`authority_class`) is a leaf of one
predicate module, `mcp-server/src/lib/predicates.py` (#1084): each
leaf has a name, a value shape, one SQL compiler over a `messages` row
or, for the folder, flag, default-scope and Spam terms, over one
occurrence (all of a query's occurrence terms are conjoined in a single
`EXISTS`, so they hold on the same copy, #1568), and an evaluability
rule, and adapters build the leaf list for
`query_messages` (conjoined on one message, with the keyset cursor
bound to a digest of the list), for the evidence-scope labels (the
same, as a label per message) and for `search_emails`' thread filters
(each leaf decided on its own against the thread, as before). A new
predicate is written once there; `docs/mcp-tools.md`, "Filter
predicates", lists the leaves.

### Mailbox occurrences

Each Maildir copy of a message is its own occurrence
(`message_occurrences`, #1566, PLAN.md decision 47), so two
byte-identical files in different folders keep their own folder, flags
and observation times; since v17 the `messages` row holds none of them
(#1568). An active occurrence is keyed by the
account (`accounts`, see *Account registry*), its folder relative to the
account, the file's Maildir uniq (the name before `:2,`, which flag
renames and `new`/`cur` moves keep; `maildir.get_uniq`) and the claimant
ID. Its path is relative to the account's Maildir root, unique among
active rows, and computed structurally, component by component
(`indexer/src/occurrences.py`): a path outside the root, a sibling that
merely shares its spelling as a prefix or one with `..`, is refused with
fixed text, never given the leaf folder name `_derive_folder` falls back
to. Relocating the root alone keeps every occurrence.

- **Ingest.** Every parsed file binds its occurrence in the Phase 1
  transaction (`Database._bind_occurrence`): the active row with its key
  takes the path, the S/F/R/T flags and the observation time, or a new
  row is created. The claimant ID comes from the file's full content
  hash, so a file binds only to the occurrence of its own bytes; any
  other active row at that path is retired first, and a new file never
  inherits a retired row.
- **Observed renames.** A watcher rename event (`on_moved`, through
  `Reconciler.handle_moved` in mirror mode) carries the occurrence in
  the same transaction as `update_filepath`: a flag rename or a
  `new`/`cur` move keeps it (path, flags and last observation updated);
  a move to another folder, or any change of uniq, retires it and
  creates the destination's for the same claimant, so the move is
  atomic. A renamed file that no occurrence held but whose
  `indexed_files` record carries an indexed message's hash binds the
  destination to that message, once the destination's size, mtime
  *and* hashed bytes all match the record (#1141, #1612): size and
  mtime agreement alone is not proof — mbsync sets mtime from IMAP's
  INTERNALDATE, so two different messages can legitimately share one.
- **Missed renames, found copies, vanished files.** The reconciliation
  scan (*Occurrence reconciliation* under *Deletion Reconciliation*,
  #1567) follows a rename the watcher missed once the file's bytes are
  verified, binds a byte-identical copy no occurrence held (provenance
  `scan`), and retires an occurrence whose file a complete scan did not
  find; a partial scan retires nothing.
- **Columns.** `state` (`active` / `retired`) with `retired_at` as the
  retirement mark; a retired row keeps its locator for diagnostics.
  `provenance` records how the row was created (`parse`, an observed
  `rename`, a reconciliation `scan` that verified a found copy's bytes,
  or the `migration` seed: v14's, and v17's retired row for a message
  the v14 seed could not bind); `first_observed_at` /
  `last_observed_at` when the indexer saw the file (NULL on a seeded row
  until a parse sees it, so no observation is invented); `internal_at`
  stays NULL, unknown, until #1081 writes it. Rows go with their message
  (`ON DELETE CASCADE`).

The reconciler and the reaper read occurrences (#1567): a message is
live while it has an active occurrence that is not `T`-flagged, and its
tombstones in `pending_deletions` are keyed by occurrence (see *Deletion
Reconciliation*). Every message keeps at least one occurrence: v17 gave
a message the v14 seed could not bind a retired one at its stored path,
since no file was verified to hold its bytes. A message is re-read (a
re-extraction sweep, a reap's survivor) from an active occurrence, a
live one first; a startup sweep queues a message with none at its latest
retired occurrence's path, its last known one, where the job waits for
a rename like any missing indexed file. A reap unmarks a retired
occurrence's path only while `indexed_files` records the message's own
bytes there, since a freed path can hold another message's file.

The MCP server reads occurrences for every folder, flag and locator
(#1568): filters match within one active occurrence; the default scope
(`mcp-server/src/lib/sources.py` `default_scope_sql`) is an active
occurrence outside the account provider's Trash role, counting only the
message's live occurrences when it has one; and a message's single
folder, flags and `source_file` locator are its representative
occurrence's (active first, then not `T`-flagged, then outside Trash,
then the oldest). Folder roles (Trash, Spam, Recovered Messages) and
each occurrence's source category are dispatched by the occurrence's
account provider (`PROVIDER_POLICIES`). The category (`mailbox`,
`unsent`, `recovered`, or `unknown` for a folder or provider the policy
does not name) is derived from the folder at read time and never
stored, so a policy change needs no migration (#1569, decided in
#1317): Drafts and Scheduled are unsent evidence whose folder does not
certify that they are still unsent, and Recovered Messages are
searchable but untrusted, never counting toward an `authority_class`.
Folder membership records where mbsync last filed a copy, not its
current state upstream. Each occurrence is returned with its account,
category and provenance (`provenance`, `first_observed_at`,
`last_observed_at`).

### Top-level header evidence

`message_headers` keeps top-level fields in their source order, duplicates
included, keyed by `(claimant_id, ordinal)`. It stores the original name,
its lowercase `name_normalized`, `raw_value`, and nullable `decoded_value`.
The `(name_normalized, claimant_id)` index serves header predicates.
Raw values map each source byte to its Latin-1 character, preserving
folding and encoded words without SQLite surrogate characters; the raw
message remains the original byte evidence. Decoding uses the existing
stdlib header path; a failure or degraded decode keeps `decoded_value`
NULL and predicates use the raw value.

Before decoding, one per-message guard limits fields to 256, total name
and value bytes to 1 MiB, and one name plus value to 16 KiB. It stops at
the first overflow, retains the earlier fields, and records
`headers_budget=1` through the existing rate-limited parser warning.
Stdlib defects that discard a header or stop its header scan also
record `headers_parse=1` without logging defect text.
`messages.headers_complete` is 1 for a complete capture, 0 for a cap or
header parse loss,
and NULL until assessed. Header predicates decide true on any stored
match; a miss is false only under 1. Header rows and their flag are
replaced in the same message-write transaction and cascade on deletion.
A synthetic macOS measurement on 2026-10-12 used 50,000 messages with
nine common fields, a tenth field on 500 messages, and duplicate
X-Metadata fields: 450,500 header rows occupied 122,167,296 bytes
(116.5 MiB), including both indexes. Full `query_messages(limit=1)`
calls, including unknown counts and leaf diagnostics, took median
50.7 ms for selective presence (500 matches), 176.1 ms for containment
in a common field (500 matches), and 122.4 ms for common-field presence
(50,000 matches), over five sequential warm-cache calls. Both header
plans used `idx_message_headers_name_claimant`; these figures describe
this synthetic corpus, not a live mailbox or a general latency bound.

Migration 0013 queues the existing reparse path; unchanged chunks keep
their IDs and vectors. `get_message` does not return this table.

## Proton identity evidence

Bridge writes Proton's message ID into the messages it builds as
`X-Pm-Internal-Id`, and Gluon (Bridge's IMAP library) writes its own
internal ID as `X-Pm-Gluon-Id`. The index extracts neither as an
identity: they are bytes of the file like any other, so they change its
size and the claimant ID's hash, and they count toward the stored-header,
address-field and sort-time read caps (T1). Like every top-level field
they are kept as header evidence of their own claimant in
`message_headers` (above) and removed with it on reap (T2), so the
`header_exists` and `header_contains` predicates can match them. That
match is a caseless substring test of each stored field's decoded value
(its raw value only when decoding failed), not a validated logical
lookup: it counts claimants (byte-identical copies are one, T6, and
byte versions are several), forged and duplicated fields
match as readily as genuine ones, and a value matches every ID that
contains it (T9).
#1313 proposes accepting `X-Pm-Internal-Id` as a logical identity
under a validated policy; this section is the evidence that proposal
waits on (#1316). Four kinds of evidence are kept apart:

- **Source review, not executed:** reading Bridge
  `b9c5dac1651437100c40896dacd778a0518a26f2` and its pinned Gluon
  `7e800978ab4a9942829eb6116e33ea0278c03e91` (and the `go-message`
  fork Bridge pins). Says what the code is written to do, not what a
  running Bridge delivers.
- **Compiled harness:** that upstream code built from source and run on
  synthetic input in an isolated container (#1583), against Gluon's
  fake connector or the go-proton-api fake server, never Proton.
- **Tests of this repository's code**, on synthetic files.
- **Unresolved:** needs a live synthetic account (#1584), or a
  mechanism this repository does not have yet (#1313, #1317, #1566).

No row describes an actual Bridge binary or a live account.

**Source review, not executed.**

| # | Observation | Where |
|---|---|---|
| S1 | Default builds set `X-Pm-Internal-Id` to the Proton ID. When the topmost existing field already equals it, every existing field is kept, so further, different fields can stay below it; otherwise all are replaced by one field. | Bridge `pkg/message/build.go` `setHeaderIfNeeded`, `internal/services/imapservice/sync_build.go` `defaultMessageJobOpts`; `go-message` `Header.Set` (delete, then add) |
| S2 | A built message without a Message-ID gets a synthesized one. | `build.go` `setMessageIDIfNeeded` |
| S3 | Multipart builds use random boundaries, so one Proton message can be built as different bytes. | `build.go` `newBoundary` |
| S4 | A created message gets one Gluon ID shared by all its mailboxes; `X-Pm-Gluon-Id` is prepended to the literal without removing existing fields of that name. | Gluon `internal/backend/connector_updates.go` `applyMessagesCreated`, `rfc822/header.go` `SetHeaderValue` |
| S5 | A content update equal to the stored literal, byte for byte or by `CompareLiterals` (a normalized comparison of content and of every part's headers that ignores MIME boundaries and `X-Pm-Gluon-Id`), keeps the Gluon ID and the memberships it stays in (so their UIDs), adding and removing only the mailboxes the update changes; any other gets a new Gluon ID. The comparison reads the stored literal from Gluon's cache: when it is not cached, any update gets a new Gluon ID (H7). | `connector_updates.go` (`applyMessageUpdated`, `applyMessageMailboxesOnlyUpdate`), `rfc822/hash.go` |
| S6 | Label and flag events update mailboxes only; draft and sent content events rebuild the literal. A failed build, at initial sync or on an event, publishes nothing. | Bridge `internal/services/imapservice/service_message_events.go`, `internal/services/syncservice/stage_build.go` |
| S7 | A failed APPEND or import whose literal Gluon can parse is stored unchanged in `Recovered Messages` (listed only when non-empty), with no `X-Pm-Gluon-Id` added, unless a literal with the same `GetMessageHash` is already there; that hash leaves out `Date`, `Message-Id` and `X-Pm-*`. A literal Gluon cannot parse is not stored there: the recovery returns the parse error first. Nor is an APPEND that fails as over the size limit (`ErrMessageSizeExceedsLimits`): `Append` returns before any recovery. | Gluon `internal/state/mailbox.go` `Append`, `internal/state/actions.go` `actionCreateRecoveredMessage`, `internal/utils/message_hashmap.go`, `internal/state/state.go` `List` |
| S8 | A lost recovery literal is an error (never refetched). Any other lost literal is refetched from Bridge, given `X-Pm-Gluon-Id` (S4) and stored; a failed fetch, header write or store is an error and returns no literal. Moving or copying out of recovery imports it: when Bridge returns a remote ID already known locally, the existing message is used and nothing is stored; otherwise the literal Bridge returns is parsed, given `X-Pm-Gluon-Id` and stored, and a failure at any of those steps is an error that stores nothing. | `state.go` `getLiteral`, `actions.go` `actionImportRecoveredMessage` |
| S9 | An APPEND outside Drafts carrying an existing, undeleted message's `X-Pm-Gluon-Id` adds that message to the mailbox without comparing bytes; Drafts erases the header and creates a message, and when the erase fails it logs the error and creates the message from the original literal, header included. | Gluon `internal/state/mailbox.go` `AppendRegular` |
| S10 | Any body-section FETCH, `BODY.PEEK[HEADER.FIELDS ...]` included, loads the full literal; `PEEK` does not set `\Seen`. `EXAMINE` is read-only; `UNSELECT` does not expunge; `CLOSE` expunges unless read-only. | `internal/state/mailbox_fetch.go`, `state.go`, `internal/session/handle_close.go`, `handle_unselect.go` |
| S11 | A mailbox snapshot takes UID, `\Recent` and `\Deleted` per mailbox, other flags from a table keyed by message. | `internal/db_impl/sqlite3/read_ops.go` |

**Compiled harness, not live.** Run on 2026-10-12 by
`mbsync/tests/upstream/run.sh` (#1583, owner decision of that date),
which also records how every input was obtained. Gluon and Bridge are
built from the commits above with Go 1.26.7, the toolchain both
`go.mod` files name (linux-arm64 archive SHA-256
`5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37`,
pinned in `run.sh` and matching go.dev's published record). Modules are
checked against each unchanged `go.sum` (SHA-256 Gluon
`57ec6e6d7201faca75cf616fdd1d691d45065b6d5730c6400fa6af7a9637e76a`,
Bridge `e41dcc08911ed850cf72a4f5d2f5a754e1e907ac5046a696d35e5a92a3ae8dfd`)
and by `go mod verify`. The container starts from the
`debian:trixie-slim` digest `mbsync/Dockerfile` pins
(`sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f`;
arm64 image ID
`sha256:c3f38b5c1e1e5b416d5b48fb6b3edd3f6ed640b56f1dbf956548df03e66b4c95`;
both as docker-library/repo-info commit
`631ff15a31ac5d63781079d695ade706e65708c5` records them) and adds 89
Debian packages from snapshot `20261005T000000Z`, among them isync
1.5.1-1 (SHA-256
`a6a579e7a72831c6c22344d31d37bb1657f25b2f7c92ebd215297b65305940d3`, the
version the mbsync image installs). Each package is checked against an
index whose `InRelease` signature gpgv verified with the base image's
archive keyring. Builds
and runs have no network (`GOPROXY=off`, `-mod=readonly`,
`GOTOOLCHAIN=local`) and run as a non-root user with all capabilities
dropped. Gluon runs its own test server with its fake (`Dummy`)
connector, wrapped to count connector calls and store reads; Bridge runs
its message builder and its integration setup against the go-proton-api
fake server, delivering to isync over TLS. Inputs are synthetic and
results are counts, byte comparisons, timings and fixed tokens. Limits:
the fake connector is not Bridge's connector, the fake server is not
Proton, and the results hold for these commits, not a released binary.
A result's match against the harness's committed `manifest.json` is an
in-process observation, not an attestation: code running inside the
harness's own locked-down process could in principle forge or empty
its own results, and no check running in that process can rule that
out by construction (owner decision 2026-10-13, accepted as a residual
risk rather than further in-process mitigation).

| # | Item (#1583) | Result | Rows |
|---|---|---|---|
| H1 | 1, builder | Bridge's builder with the sync's job options, given `X-Pm-Internal-Id` fields in the parsed headers: none, one equal to the Proton ID, one or two other values, or two fields with the other value topmost all give one field equal to the ID; two fields with the ID topmost keep both, so a different value stays below it. | S1 |
| H2 | 1, fake API | Five imported literals (no identity header, one or two `X-Pm-Internal-Id` fields with other values, an `X-Pm-Gluon-Id` field, both): each of the 5 files isync wrote has one `X-Pm-Internal-Id` equal to its Proton ID and one `X-Pm-Gluon-Id`, the topmost field, and none of the imported values. The fake server keeps only an import's standard fields, so this path cannot reach H1's keep branch or show whether Proton keeps sender-supplied `X-Pm-*` fields (U2). | S1, S4 |
| H3 | 1, delivery | Each file differs from the literal Bridge served over IMAP: isync wrote LF line endings and added one `X-TUID` field; with both undone the 5 files equal the served literals. A second sync of the same unchanged messages into a new, empty Maildir (new sync state) gave 0 of 5 byte-identical files, 5 of 5 identical without `X-TUID`. A file's bytes, and so its claimant ID, are not the served bytes, and a message isync writes again into an empty Maildir gets different bytes. An incremental sync into the same Maildir was not measured. | U3 |
| H4 | 2 | Eight APPENDs the connector refused, all answered `NO`: a literal with an `X-Pm-Internal-Id` field, an exact repeat, and copies differing only in `Date`, `Message-Id`, `X-Pm-Internal-Id`, an added `X-Pm-Gluon-Id`, `Subject` or body. `Recovered Messages` stored 3 (the first, the `Subject` and the body variant), each row the APPEND added byte-equal to that APPEND's own literal, the first with its `X-Pm-Internal-Id` field and no `X-Pm-Gluon-Id`. The target mailbox stayed empty. | S7 |
| H5 | 3 | A recovery literal removed from the store: `FETCH` answers `NO` with 0 connector fetches, and the message stays listed. `COPY` out of recovery: 1 connector create, recovery keeps its copy (still byte-equal to the submission), the target gets the submitted bytes with one `X-Pm-Gluon-Id` prepended. `MOVE`: the same, and recovery is left empty. | S8 |
| H6 | 4 | APPEND to an ordinary mailbox of a different literal carrying an existing message's `X-Pm-Gluon-Id`: 0 connector creates, 1 add, and the mailbox serves the existing message's bytes, not the appended ones. To a `\Drafts` mailbox: 1 create and a new Gluon ID. An unknown, well-formed Gluon ID: 1 create, and the served literal has 2 `X-Pm-Gluon-Id` fields. | S9, S4 |
| H7 | 5 | Cache refill (stored literal removed, 1 connector fetch each): when the connector returns the same bytes, the UID and bytes are unchanged; when it returns a rebuild with another MIME boundary (S3), the UID and Gluon ID stay and the served bytes change. An update equivalent by `CompareLiterals` keeps UID, Gluon ID and the stored bytes when the literal is cached, and gets a new Gluon ID and UID when it is not; an update with exactly the stored bytes (Gluon's `X-Pm-Gluon-Id` included, literal cached) keeps UID, Gluon ID and bytes, and so does the connector repeating the bytes it last sent (without that field, which Gluon adds before comparing); a content change gets a new Gluon ID and UID. | S5, S8 |
| H8 | 6 | One message in two mailboxes, `\Seen \Flagged \Deleted` stored in the first: the second shows `\Seen` and `\Flagged`, not `\Deleted`. isync wrote one file each under `cur`, flags `FST` and `FS`. | S11 |
| H9 | 7 | `EXAMINE` answers `[READ-ONLY]`; `UID FETCH ... BODY.PEEK[HEADER.FIELDS (X-Pm-Internal-Id)]` leaves `\Seen` on 0 of 3 messages; `STORE` answers `NO`; `UNSELECT` keeps all 3 (one `\Deleted`), with 0 connector mutation calls and 0 connector fetches. `CLOSE` after `EXAMINE` keeps 3 with 0 mutations; `CLOSE` after `SELECT` leaves 2, with 1 connector remove call. | S10 |
| H10 | 8 | 200 messages of about 256 KiB (52,457,470 bytes from the connector); one pass is `EXAMINE`, `UID FETCH 1:* (UID BODY.PEEK[HEADER.FIELDS (X-Pm-Internal-Id)])`, `UNSELECT`. The figures below cover the `UID FETCH` alone (counters start after `EXAMINE` and are read before `UNSELECT`; H9 covers those two commands). Every `FETCH`: 200 responses, 20,518 bytes to the client, or 20,517 when the command takes under 10 ms (6,890 of them header literals; the count includes Gluon's tagged `OK` line, which states the elapsed time in microseconds), 200 full-literal loads, 0 connector mutations, `\Seen` on 0. Warm (literals cached): 200 store reads of 52,468,070 bytes, 0 connector fetches. Cold (store emptied): 200 store misses, then 200 connector fetches and 200 store writes of 52,468,070 bytes. Warm again: as warm. `FETCH` time over two runs: warm 28.8 and 32.1 ms, cold 18.6 and 22.0 ms, warm again 26.7 and 27.6 ms. Peak RSS of the test process (server and client together) over the same runs: warm 203,992 and 193,312 KiB (from 139,772 and 150,596 before the pass), cold 190,880 and 222,728, warm again 221,568 and 276,416. The probe counts store reads and writes without buffering them. The fake connector serves from memory, so cold time leaves out Bridge's API fetch and build. | S10 |

**Tests of this repository's code.** Test files are under
`indexer/tests/` unless named otherwise.

| # | Supported claim | Test |
|---|---|---|
| T1 | Parsed with the same source metadata, a message with identity headers (at the top of the header block or below the others) differs from one without only in its size and content hash (so its claimant ID) and in its `headers` list, which keeps every identity field as an ordinary header at its source position, within the header budget (at most `MAX_STORED_HEADER_FIELDS`, 256, top-level fields, none over 16 KiB of name and value, 1 MiB in all); every other compared `Message` field is equal, and `Message` has no provider-identity field. Past any of the three, header capture stops at the first field over it: the earlier fields are kept, the rest (`From` and `Date` too, when identity headers fill the capture above them) are not stored, `headers_complete` is false, and the cap is counted as `headers_budget` in `parse_caps` and logged as a WARNING; the other `Message` fields are unchanged up to `MAX_ADDRESS_HEADER_FIELDS` (10,000) fields. Past that many, wherever the identity headers sit, the From/To/Cc scan stops as well (#1144): `address_fields` is counted too, the sender is flagged ambiguous and all three address roles incomplete, and From, To and Cc headers after the cut-off are lost (all of them when identity headers fill the scan above `From`). Identity headers above `Date` that push it past the first `SORT_HEADER_MAX_BYTES` (64 KiB), in a file with no `Received:` within that read, leave the sort time of a first full index unknown, so the file is ordered at the walk's start time; a `Received:` above them still gives it. | `test_provider_identity_evidence.py` `TestParserIgnoresIdentityHeaders` |
| T2 | Two files whose bytes differ (byte-identical files share one claimant, T6) with equal, forged, duplicated, conflicting or absent identity headers, at the top of the header block or below the others, with the same or different Message-IDs, in either order, are two claimants with their own rows. Trashing one tombstones only its claimant ID (never the bare Message-ID or the other claimant) and, within the grace window, removes no row; reaping it removes all of its rows except the `reaped_messages` record it adds, and leaves every row of the other unchanged. Every table with a `claimant_id` column in the schema is seen holding rows. A file whose identity headers fill the address scan (T1) is still its own claimant, stored with both caps recorded, `headers_complete` 0, its first 256 fields and no participant rows. No identity value is logged; the only stored copies are each claimant's own `message_headers` rows, exactly the fields its file carries (within the header budget), and reaping the claimant removes them. | `TestClaimantsStayIndependent` there; `test_main.py` `TestMessageIdClaimants` (#217) |
| T3 | Two byte versions of one message (S3: same text and identity headers, new boundary) have disjoint chunk IDs and each its own identity header rows; reaping the older removes only its chunks and header rows and records it as reaped. | `TestByteVersionsOfOneLogicalMessage` |
| T4 | While its reap record is inside the retention window (`REAPED_RECORD_RETENTION_DAYS`, 30 days), a reaped version's claimant ID reports the reap, never the surviving version; after it, a lookup by that claimant ID reads as not found; a bare Message-ID naming several live claimants lists them rather than choosing. | `mcp-server/tests/test_retrieval.py` `test_reaped_version_is_not_answered_by_a_surviving_version`, `test_bare_message_id_with_several_claimants_lists_them`, `TestReapedSources` `test_expired_record_reads_as_not_found` |
| T5 | A file without a Message-ID (as recovery can hold, S7) is dead-lettered with fixed text whatever identity headers it carries (every T2 shape, at the top of the header block or below the others). Today's behaviour, apart from the #1082 decision. | `TestIdLessRecoveryStyleInput` |
| T6 | Byte-identical copies share one claimant and one row; removing one copy keeps the message (#1102). | `test_synthetic_source.py` `TestByteIdenticalCopiesAcrossFolders`, `test_reconciler.py` `TestByteIdenticalCopies` |
| T7 | A source with no Proton headers and non-Proton folder names parses normally and is indexed through the queue and writer into its folder; a missing send date stays unknown. | `test_synthetic_source.py` (#1483); `TestGenericSourceIndexes` |
| T8 | A claimant ID depends only on the file's bytes: flag renames and folder moves keep it. | `test_parser.py` `TestClaimantId` |
| T9 | `header_contains` on `X-Pm-Internal-Id` matches each claimant (byte-identical copies are one, T6) holding a field whose decoded value (raw value when decoding failed) contains the value, so an encoded word matches the ID it decodes to and not its wire text: two byte versions of one message count as two, a claimant with duplicated fields matches on any of them, a shared prefix in another case matches every ID containing it, and a claimant whose header capture is incomplete or not yet assessed is indeterminate when no stored field matches, not a miss; a stored match counts even then. The indexer stores an encoded word's wire text as the raw value and the decoded ID as the decoded value. | `mcp-server/tests/test_provider_identity_headers.py`; `test_provider_identity_evidence.py` `TestEncodedIdentityHeaderThroughTheWriter` |

**Observations an identity policy must reject.** These follow from
the rows above; they bind #1313's policy, which does not exist yet.

- `X-Pm-Gluon-Id` as a logical identity: it is Gluon-local, changes on
  unequal content updates, on equivalent ones whose literal is not
  cached, and on recovery imports Bridge does not map to a known
  message (S4, S5, S8, H5, H7), and a client can use it to file an
  existing message without a byte check (S9, H6).
- Any identity header in a `Recovered Messages` file, which keeps the
  submitted bytes (S7, H4); recovery is an untrusted category (#1317).
- A file's bytes, or its claimant ID, as the bytes Bridge served or as
  stable when a message is written again (for example into a rebuilt,
  empty Maildir): isync writes LF line endings and adds an `X-TUID`
  field whose value differed between two such writes (H3).
- A header's presence in a file whose provenance is not established,
  and a folder name as provenance.
- Equal bytes or an equal Message-ID as proof of one logical message
  (PLAN.md decision 45).
- Several `X-Pm-Internal-Id` fields in one file are ambiguous, not
  accepted. S1 and H1 mean a Bridge-built file can carry them when the
  topmost matches, so duplication alone does not prove forgery either.
- A `header_exists` or `header_contains` match on an identity header,
  or its count, as a logical identity or a count of logical messages:
  it is a text match over each claimant's stored fields (T9), and a miss on a
  file whose header capture stopped (T1) is unknown, not absence.

**Unresolved.**

| # | Question | Needs |
|---|---|---|
| U1 | The compiled-harness rows (H1–H10) with Bridge's own connector and Proton in place of the fakes: whether Proton keeps sender-supplied `X-Pm-*` fields (H2), what a real cache refill or event rebuild serves under an existing UID (H7), header-only cost when reads miss Gluon's cache and go to Proton (H10), and whether a released Bridge binary behaves as these commits do. | #1584 |
| U2 | Proton ID stability across moves, labels, draft replacement, send and imports; whether Proton keeps sender-supplied `X-Pm-*` headers; cache rebuild, resync, account re-add, split or combined address mode and primary-address changes; header-only reads of a real label set and races with moves. | #1584 (owner-approved plan first) |
| U3 | Account namespace, the acceptance rule, binding an observation to the exact retained bytes through the mbsync handoff (the file is not the served literal, H3), logical lookup and counts (header predicates match stored text only, T9), membership generations and partial scans. | #1313, #1317 |
| U4 | Independent occurrences of equal bytes (own folder and flags); stored flags still come from the file name. | #1566, #1485 |

## Message time

The index records two times per message, `sent_at` and `occurred_at`,
and derives one effective time from them. `query_messages` and
`query_attachments` let `date_basis` select effective (the default),
sent or occurred time for bounds, ordering and cursors. A missing
selected sent or occurred clock is indeterminate even without bounds;
changing clocks rejects the cursor. Other tools use effective time.
Outputs return both stored fields. Selected-clock pages use partial indexes
on `(sent_at, claimant_id)` for parsed send dates and
`(occurred_at, claimant_id)` for delivery dates (#1123). Page-only guards
permit raw clock ordering while evidence expressions and count SQL stay
unchanged. The delivery index uses the exact predicate
`(occurred_at IS NOT NULL) = 1` so ordinary date bounds do not imply its
partial-index predicate on the tested SQLite versions. Plan tests detect
changes to that matching behavior. Attachment joins can still sort, and
exact counts still depend on the population examined.

**`sent_at`** is the message's `Date:` header as parsed by
`parsedate_to_datetime` (`indexer/src/parser.py` `_parse_date`),
converted to UTC and stored as an ISO 8601 string
(`2024-03-05T12:00:00+00:00`). A header without a zone (`-0000`) is
read as UTC. It is the sender's claim, not a delivery time: a sender
can backdate or future-date it. **`sent_at_status`** says what the
header gave (#1080): `parsed`, `missing` (no `Date:` header) or
`invalid` (one that does not parse, an empty one included). `sent_at`
is NULL unless `parsed`: an unknown send date is stored as unknown,
never as another time, and a table CHECK holds the two together. The
parser logs each unknown date at WARNING (rate limited) with the
reason and the file's path, never the header text.

**`occurred_at`** is the delivery time: the date of the topmost
`Received:` header, which the last receiving server adds (expected to
be Proton's; not recorded as checked against real mail). The parser takes the text after that header's last
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

**Effective time** is `COALESCE(occurred_at, sent_at, first_indexed_at)`,
stored as the virtual generated column `messages.effective_at`
(indexed on its own and with `thread_id`, `folder` and `message_id`).

*Undated mail.* `first_indexed_at` is when the indexer first stored
the message. It is kept when the message is reprocessed or its thread
rebuilt (`_write_message_record` keeps the stored value on conflict;
`Database.keep_persisted_first_indexed_at` gives the in-memory message
the same value before threading, #297). It is the effective time only
of a message with neither date, and there it is an ordering and display
fallback, not evidence: it orders the message among the rest, places it
in its thread's span, and no date bound reads it. A date bound is
unknown (indeterminate) for such a message, and for one whose
`sent_at_status` is still NULL with no `occurred_at` (below). Only
undated mail without a readable `Received:` date (undated sent mail)
falls back to it; when the index is rebuilt from empty, it is the
rebuild time.

*Rows from before schema v8.* The v7 indexer stored the time it first
parsed an undated file in `sent_at`. Migration 0008 rebuilds
`messages` (SQLite cannot drop a NOT NULL or change a generated column
in place; the participant rows are copied aside and back, since the
drop would delete them by cascade), keeps every existing value, sets
`sent_at_status` NULL (not assessed) and `first_indexed_at` to the
row's `indexed_at`, and queues the reparse. Until a row is re-parsed
its `sent_at` may be that made-up time, so no date bound and no `sent`
order reads it (indeterminate) unless the row has an `occurred_at`,
and no tool returns it: mcp-server selects `sent_at` only when the
status is `parsed`, so the outputs, prompts, citations and
`brief_issue`'s as-of date show `sent_at` and its status as null. The
reparse also removes that time from the thread's FTS text, where the v7
writer had put it in a `Date:` line after the message's `From:` line
(only when that pair occurs once; a dead-lettered message keeps it until
`make requeue-dead`, #1379). The reparse stores the
status, and for an undated row moves the old fallback in `sent_at` to
`first_indexed_at`, so no message moves in the ordering. Nothing is
re-embedded: the date is in no chunk.

Where the times are stored:

| Stored as | What it holds |
|---|---|
| `messages.sent_at` / `messages.occurred_at` | The message's own times (authoritative), NULL when unknown; `sent_at_status` says why for `sent_at` |
| `messages.first_indexed_at` | When the indexer first stored the message: the ordering fallback of an undated message, not evidence |
| `messages.effective_at` | `COALESCE(occurred_at, sent_at, first_indexed_at)`, generated, never written |
| `threads.date_first` / `date_last` | The earliest and latest effective time among the thread's messages, recomputed from its `messages` rows on every upsert so a re-dated message moves the range; the reap rebuild derives them from the survivors the same way |

A chunk stores no date of its own (#575): a passage's dates, body and
attachment chunks alike, are read from its message's `messages` row
through the claimant ID. A reprocess commits a re-dated message in
Phase 1, before its chunks are rewritten, so a stored chunk copy could
lag the message whenever Phase 2 failed.

`messages.indexed_at`, `messages.first_indexed_at`,
`message_chunks.chunked_at`, `attachments.seen_at` and
`indexed_files.indexed_at` are indexer bookkeeping, not message time,
and no tool returns them as a message date. Bitemporal modeling (when a claim was made versus when the event
it describes happened) waits for Phase 5.

*Maildir file mtime.* Since `CopyArrivalDate yes` in
`mbsync/mbsyncrc.template` (#1081), the mtime of a file mbsync writes
is the message's IMAP INTERNALDATE as Bridge reports it, the server's
arrival time that IMAP `SINCE` / `BEFORE` search on (isync's manual:
IMAP does not guarantee the internal date is the arrival time, but it
is usually close). The flag rename isync performs for a flag change
and the entrypoint's post-sync `chmod go+r` move only the file's
ctime, so the mtime stays; `mbsync/tests/layout_check.sh` check 8
syncs a message with a known far-side date through the shipped image
and reads the mtime back after each step. Its far side is a Maildir
store standing in for Bridge, whose date isync takes from the far
file's mtime. The IMAP half is `mbsync/tests/tls_check.sh` check 1a
(`make test-mbsync-tls`, #1132): the shipped image, with the config the
entrypoint renders, pulls one synthetic message from the test IMAP
server (`mbsync/tests/imap_stub.py`, implicit TLS) whose INTERNALDATE
is `02-Jan-2020 05:04:05 +0200`. The check shows isync asks for
INTERNALDATE in its `UID FETCH`, parses the offset, and gives the file
the mtime 2020-01-02T03:04:05Z; without `CopyArrivalDate` the check
fails. The server refuses any command that would change the far side
(APPEND, STORE, EXPUNGE and the like), and the check fails if the pull
sends one. What stays unverified is Bridge itself: which date the
Proton Mail Bridge app reports as INTERNALDATE (Proton's arrival time,
or something else such as the `Date:` header) has not been checked
against the app, so the mtime is the date Bridge reports, not a proven
arrival time. A file synced before the option carries the time
mbsync wrote it, the first sync for the existing corpus, and nothing
tells the two apart from the file alone. The indexer does not read
mtimes yet: `indexed_files.mtime_ns` is identity metadata, written at
parse time and carried across renames, and no tool returns it.
Persisting the arrival time as `internal_at`, with a stamp that marks
pre-option files unavailable, is #1081's remaining work.

**Outputs.** Every per-message and per-passage result returns
`sent_at`, `sent_at_status` and `occurred_at` (a date null when
unknown, and `sent_at` with its status null when not yet assessed), in the stored string
form: message headers (`get_message`, `get_thread`, `query_messages`),
attachment occurrences (`query_attachments`), evidence chunks
(`get_evidence`), citations (`ask_mailbox`, `brief_issue`,
`check_conclusion`, `extract_from_emails`) and attachment hits
(`search_attachments`, the carrying message's times). The prose says
why a send date is unknown, or that it is not yet checked; no output
shows `first_indexed_at`. Thread results carry `date_first` /
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
| Message (`query_messages`) | Its effective time is in the range; indeterminate without a delivery or assessed send date | Effective time, newest first |
| Message group (`aggregate_messages`) | Counts the messages `query_messages` would return; `year` and `month` groups are the UTC year or month of the delivery date, else the assessed send date, and the no-value group without either | Messages per group, most first |
| Attachment (`search_attachments`) | The carrying message's effective time is in the range | Relevance; with no query, effective time, newest first |
| Attachment occurrence (`query_attachments`) | The carrying message's effective time is in the range; indeterminate as for `query_messages` | Effective time, newest first |

Thread admission and message dates agree because both use the
effective time: a message sent on 31 January and delivered on
1 February is in a February range under every tool, and its thread's
span starts on 1 February. They differ for a message with neither
date: the per-message filters count it as indeterminate, but a
thread's span and `search_attachments`' bound still read its
`first_indexed_at` (#1373).

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
the file the query names, and the slot kept for a keyword-matched
passage, are not date-scoped either: they order passages within a
qualifying thread. Ranking lanes are not date-scoped
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
  (every one a message stores in `message_participant_names`, #1140)
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
message, authors first (a repeated address is one entity, with every
display name the message stores for it, and does not count again), so a crafted header listing thousands of recipients
cannot drive unbounded writes; later participants still get their
`message_participants` rows, just no new entity. The MCP server's
`find_contact` reports each contact's organization.

Entities are pruned with the mail that mentions them (#464). When the
reaper removes messages (a partial reap or a whole-thread delete), the
same transaction deletes, among the entities those messages mentioned
only:

- a person with no `message_participants` row left for its address,
  with its aliases;
- an alias that no remaining message stores as a display name of its
  address (`message_participant_names`), while the person stays;
- an organization of a deleted person once no person belongs to it.

Entities and aliases still mentioned by surviving mail are untouched.
The sweep examines only the reaped messages' addresses that own an
entity (so recipients past the per-message entity cap cost nothing
beyond the one read that filters them out), each with an indexed
lookup (`idx_message_participant_names_address_name` serves the
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
DMARC-failing mail in Spam, and Bridge-local recovery mail keeps the
headers its client wrote (#1317), so a copy in the `Spam` or `Recovered
Messages` folder never counts toward an `authority_class` filter (the
provider's roles, `authority_excluded_sql` in
`mcp-server/src/lib/sources.py`; `AUTHORITY_EXCLUDED_FOLDERS` in
`predicates.py` names them for prose, #1569). Spoofed mail Proton leaves in the
inbox still matches; gating on DKIM/DMARC verdict headers is deferred
until Bridge's headers have been checked on real mail (#463).

A second guard covers a message whose sender cannot be told (#1144).
The parser reads only the first of repeated `From` headers and flags the
message, as it does one whose header scan stopped at its 10,000-field
budget, and the indexer stores the flag as
`messages.sender_ambiguous`: 0 for one `From`, 1 when the attribution
is unsafe, NULL when not yet assessed (rows from before schema v2,
until the reparse reaches them). Only 0 qualifies for authority
(`_compile_authority_class`, `mcp-server/src/lib/predicates.py`): 1
and NULL are "can't tell", an unknown leaf that `query_messages`
counts as `indeterminate` outside those folders (#1161; a message only
in them stays a decided miss), so the `authority_class` filters are empty straight
after the v2 upgrade and fill in as the reparse drains, and a message
whose job is dead-lettered stays out of them until `make requeue-dead`.
The `from` participant rows are kept, so sender filters and
`find_contact` are unchanged; the threader skips the subject fallback
for a flagged message, since that check trusts its author. Ambiguous
messages cannot join by subject alone or supply correspondent evidence
for another subject-only merge. NULL supplies no evidence; assessed
messages in mixed threads can still qualify: the fallback's
correspondent check reads the author and another recipient from the
candidate thread's messages with `sender_ambiguous = 0` only
(`Database.thread_has_assessed_correspondents`), each from any such
message, and logs a rate-limited INFO count of candidates it turned
down for that reason alone.

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
as an `authority_class` filter on `search_emails`, `query_messages`
and `aggregate_messages` (a message with a copy outside Spam and
Recovered Messages matches when one of its From senders carries the
class; a thread when one of its messages with such a copy does), as an `aggregate_messages` grouping dimension with the
same rule, and on `find_contact` results, which stay per contact and
ignore folders.
Filtering removes results without reordering or rescoring the rest.

### Operator identity

The operator's own addresses come only from an operator-written file,
`config/identity.toml` (`addresses = [...]`, mounted read-only at
`/config`; see `config/identity.toml.example` and `docs/setup.md`). They
are exact canonical addresses, normalised as `canonical_addr` normalises
a participant address (`message_participants.address`), so a stored
participant address is in the set exactly when the operator listed it.
Nothing is inferred from a domain, a plus-alias or the Sent folder.
They are for message direction (#824); nothing reads them yet.

The indexer loads the file at startup (`load_operator_identity`,
`indexer/src/entities.py`; at most 64 KiB and 1,000 addresses, errors
naming the entry's position, never an address) and replaces, in one
transaction, both tables (`Database.set_operator_identity`):

| Table | Holds |
|---|---|
| `operator_addresses` | One row per listed address. |
| `operator_identity` | One row: `state` (`configured`, or `unconfigured` when the file is absent), `address_count` and `address_digest`, the SHA-256 (hex) of the sorted addresses, each followed by a newline. Unconfigured with no addresses has the digest of the empty string. |

An absent file clears the set and records `unconfigured`; an empty or
malformed one stops the indexer. A fresh index, or one migrated to
schema v7, reads `unconfigured` until the indexer's first start writes
the file's state. Editing the file takes an indexer restart, with no
reindex: nothing per message depends on it. The MCP server will read
the tables from SQLite and gets no `/config` mount.

## Attachment Indexing

Email attachments flow through the same chunker and embedder pipeline
as message bodies. Three extra tables sit alongside `message_chunks`: `attachments_fts`
(contentless FTS5 over filename and MIME type, joined by
`attachments.fts_rowid`) and the two below:

| Table | Keyed by | Purpose |
|---|---|---|
| `attachments` | attachment_occurrence_id | Per-occurrence row capturing filename + MIME + size as it appeared on a specific email. The occurrence id includes the message, payload hash, filename, and attachment slot so duplicate same-payload files in one email are still represented. `extractor_module` names the extraction row the occurrence uses (see below; '' when its label selects no extractor). `text_complete` and `text_extractor` record whether its committed chunks hold all its text, and the stamp of the result that applied (#1242; see *Attachment text completeness*). When one message carries the same bytes under labels that run different extractors, the texts share the message's chunk slice for the payload, and every text's chunks are kept. A text hit in `search_attachments` is attributed to an occurrence whose row is a success; with two such occurrences in one message, to the first by occurrence id. |
| `attachment_extractions` | (attachment_id, extractor_module) | Cache of extracted text + status, keyed by the payload's sha256 and the extractor namespace the occurrence's MIME type and filename select for those bytes after the constant-size prefix check (OOXML bytes labelled `.doc` are keyed `docx` and share the `.docx` row, PDF bytes under an image label are keyed `pdf` and share the `.pdf` row (#1415); '' when the label selects no extractor) (#928). An OLE2 or ZIP payload runs the extractor its directory names whatever the key, recorded in the `extractor` stamp (see *Extractor dispatch*, #1416). Dispatch from a label and the bytes is deterministic, so every occurrence with the same key would extract the same result, and an occurrence is served only what an extraction under its own label gives, whatever labels of the same bytes arrived before it: an OLE2 `.doc` first seen as `.txt`, or a PowerPoint file first sent as `.doc` (#986), no longer decides the later occurrences' result. The same bytes under two labels that run different extractors store two rows; the cost is one extraction per module the bytes arrive under and a second copy of the text, which is negligible next to the mail itself. The expensive work (Tesseract OCR, pypdf parse, DOCX walk) runs once per payload and cache key until the row is refreshed, including within one indexing batch, where results not yet committed are shared by the same key. Non-success rows are also honored: `empty` short-circuits unconditionally; `too_large` short-circuits while the payload still exceeds `INDEXER_ATTACHMENT_MAX_BYTES`, and is re-extracted once the operator raises the cap far enough for it to fit (#693); `unsupported` short-circuits while it holds: an "OCR disabled" row until OCR is turned on, except an image one recorded with no stamp (before #1415), which never holds; an "OLE2 compound file" row (an OLE2 payload under an OOXML label, #694, #936), a "binary payload labelled as text" row (#932), a "not an OLE2 compound file" row under the `ppt` module (#957), a "not an OLE2 or OOXML container" row under a legacy `doc` / `xls` label (#1227) and a "no extractor" row for good, since the label and the bytes decide them; an encrypted-PDF or pypdf-limit row under `pdf`, an eager-part-budget row under `xlsx`, a pre-open package-budget row under `pptx` or `docx`, an encrypted legacy `.ppt` row under `ppt` (#983), an image over the image child's pixel ceiling (#1401) and the container results (#1416) for good too, since the same bytes would be declined again (`PERMANENT_FAILURE_ERRORS`, #931, #1032); any other (an extractor not importable in the image) only under the '' module; `failed` short-circuits within a 7-day retry window so a chronic failure stops re-running on every reappearance, but a real fix landed via dependency upgrade can pick the payload up later. The `extractor` column carries a version (`docx@5`); a row written by an older version of a fixed extractor (`extractors.EXTRACTOR_VERSIONS`) is re-extracted by the next occurrence that uses it, and the indexer re-queues every message with an occurrence using it once at startup so their chunks are rebuilt, except dead-lettered messages, which keep their stale chunks until `make requeue-dead` rescues them. Rows from a newer version (after a rollback) are kept. A row is deleted with the last `attachments` row that uses it (see *Cascade on message removal*). A stale row an OCR extractor wrote (`image-ocr`, `pdf-ocr`) is kept and served while `INDEXER_OCR_ENABLED=false`, since a refresh could only replace its text with "OCR disabled"; it is refreshed once OCR is on. Likewise, once OCR is on, the startup sweep re-queues each message with an occurrence using an "OCR disabled" row, and, whatever the OCR setting, one using an image "OCR disabled" row with no stamp, keyed `image` or '' (#1415). The sweep also re-queues, whatever the OCR setting, each message whose occurrence uses a "no extractor" or "OLE2 compound file" row but whose label now selects another module, as when a release starts routing an extension such as `.heic` (#691), `.dotx` (#937), `.pptx` (#936), `.ppt` (#957), `.pptm` / `.ppsx` / `.potx` (#947) or `.ppsm` / `.potm` (#1042); the reprocess writes the occurrence's own row and points the occurrence at it, so each is re-queued once. Likewise, after `INDEXER_ATTACHMENT_MAX_BYTES` is raised, it re-queues every message with an occurrence using a `too_large` row whose size (`attachments.size_bytes`) now fits; the re-run rewrites the row, so each is re-queued once, and bytes still over the cap are never re-queued (#693). `ocr_pages_skipped` holds the scanned pages the PDF OCR page cap left unread (0 when none, NULL when unknown: a non-PDF result, a `failed` or `unsupported` one, or a row cached before schema v3), so an occurrence served the row is still counted as capped once its message commits (#891); the text served is the same. `text_complete` records whether the result lost no text (#1242); a `success` or `empty` row with none (cached before schema v6) is re-extracted once, except an `-ocr` row while OCR is off (#1285). `ocr_pages_cap`, `digital_pages_cap` and `extracted_chars_cap` record the configured limit that cut a `success` or `empty` result (#1418): `INDEXER_OCR_MAX_PAGES` (scanned PDF pages, TIFF frames; recorded by `pdf` and `image`), `INDEXER_PDF_MAX_DIGITAL_PAGES` (`pdf`) and `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` (the dispatcher's cut for every module, and the `.doc` / `.ppt` tools' output bound when the setting, not the 40 MiB ceiling, set it): the limit's value when it cut, 0 when it did not, NULL when it does not apply or the row predates schema v11. A hardcoded limit (an image's 10,000,000-character text budget, a TIFF frame that cannot be read past the cap, the tools' ceiling, the page-pixel budgets) records nothing; a fix to one is an `EXTRACTOR_VERSIONS` bump. A row a limit cut is not served once that limit is raised (higher, or `0` for the two that have no-limit values; lowering never counts), and an incomplete row (`text_complete` 0) cached before schema v11 is re-extracted once to record its caps. While OCR is off, a row an OCR extractor wrote (`-ocr` stamp) or whose `ocr_pages_cap` or `ocr_pages_skipped` is above 0 is kept out of both, so a refresh cannot record "OCR disabled" over it or forget its unread pages; it is refreshed once OCR is on. The lookup (`attachment_indexing.cap_raised`, `cap_bootstrap_due`) and the startup sweep (`Database.find_cap_refresh_attachment_filepaths`, the same test in SQL) agree; the sweep queues every message using such a row before the drain replaces it, and logs one INFO line with the three settings and its counts (`docs/troubleshooting.md`). A cut that remains on a result served from the cache is counted and logged at commit, as a fresh one is. `identifier` (#1416) records how the payload's container was certified: `container@<n>` (the identification version, `extractors.CONTAINER_IDENTIFICATION_VERSION`) for an OLE2 or ZIP payload whose directory was identified before dispatch, `none@<n>` (the signature-set version, `extractors.CONTAINER_SIGNATURE_VERSION`, owner decision on #1416) for one with neither signature, NULL for a `too_large` row (no dispatch reached it) and for every row cached before schema v12. A row with none, a `container@` row of an older identification version, or a `none@` row of an older signature set is not served: checked beside the stale stamp, before any status (an `unsupported` one included) is honoured, the lookup stamps `none@<n>` on a row whose payload has neither signature (a constant-size check; the result is kept) and identifies and re-extracts the rest. A newer version (after a rollback) is kept, and so is a row an OCR extractor wrote while OCR is off. The lookup (`attachment_indexing.identification_due`, `identification_refreshes`) and the startup sweep (`Database.find_identification_refresh_attachment_filepaths`, the same test in SQL, without the prefix check, which needs the bytes) agree; the sweep queues every message using such a row, clearing its occurrences' `text_complete`, before the drain replaces it, and logs one INFO line with its counts (`docs/troubleshooting.md`). The v12 migration also queues a reparse of every indexed file, which passes every occurrence through the lookup once; chunks whose text is unchanged keep their IDs and vectors, and only text a re-identified container now yields (an Office file sent under another type, recorded `failed` or `unsupported` before) is new chunks, embedded by the configured embedder. A container sent under a `message/*` label also changes attachment ID (its payload was the serialized email, now the bytes as sent): the reparse removes the old occurrence with its FTS row, chunks and vectors (see *Cascade on message removal*, #1375) and embeds the new one's text once. Schema v1 introduced the key; see *Schema versions*. |

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
image/* prefix → image
   ↓ not image/*
filename ext → _EXT_DISPATCH (.pdf, .docx, .xlsx, .pptx, .png, ...)
   ↓ no match
status="unsupported" (still searchable by filename via attachments_fts)
```

Per-format modules live under `indexer/src/extractors/` and are
lazy-imported so a missing optional dependency (e.g. `python-docx`
not in this image) downgrades to `unsupported` rather than crashing
the indexer at startup. A library missing from the extractor child
(pypdf, pytesseract, xlrd) is recorded `failed` by type name instead.

Container identification (#1416, owner decision on #1416, 2026-10-10):
a payload that starts with the OLE2 signature (`D0 CF 11 E0 A1 B1 1A
E1`) or a ZIP lead signature (`PK\x03\x04`, `PK\x05\x06` for an empty
archive, `PK\x06\x06` for an empty ZIP64 archive, the split / spanned marker `PK\x07\x08`, or the temporary
spanning marker `PK00` before a local header) is identified by its directory, whatever its label (image,
Office, text, PDF, HTML, email or none) and before the OCR gate, and
runs the one extractor its directory names. `extractors/container.py`
starts `extractors/container_child.py` in the extractor child through
`run_tool`, counted against the per-message extraction budget like any
launch, and the child returns one fixed token; nothing is extracted to
decide.

- OLE2: olefile 0.47 reads the header, the FAT and the directory; the
  root storage's stream names (never a storage's) decide: `WordDocument` runs `doc`,
  `Workbook` or `Book` runs `xls`, `PowerPoint Document` runs `ppt`.
  `EncryptionInfo` with `EncryptedPackage` (a password-protected OOXML
  file) is `unsupported` ("encrypted Office file (open password
  required)"), none of them is `unsupported` ("OLE2 compound file with
  no Word, Excel or PowerPoint stream": an Outlook message, a Visio
  drawing, an installer), and more than one of these four kinds (the
  encrypted pair beside a Word stream, for one) is `unsupported`
  ("container holds more than one Word, Excel or PowerPoint document"),
  never guessed, so a decoy stream is never read in place of an
  encrypted package.
- ZIP: zipfile reads the central directory. An archive with no
  `[Content_Types].xml` is not an OOXML package. For a package, the
  child reads the two OPC metadata members, `[Content_Types].xml` and
  `_rels/.rels`, with defusedxml (no DTD, no entities), and applies the
  extractors' own rules (owner decision on #1416, 2026-10-10): `docx` or
  `pptx` when the single root `officeDocument` relationship targets a
  member whose content type (its Override, else the Default for its
  extension, compared case-insensitively) is a Word or PowerPoint main
  type that extractor reads, `xlsx` when openpyxl's workbook lookup in
  the content types finds a member. A renamed main part
  (`word/document2.xml`) is found this way; no other member is opened.
  More than one kind is ambiguous, as above; a package naming none
  (missing or malformed metadata included) keeps the label's OOXML
  extractor (a legacy label's counterpart included), or is
  `unsupported` ("ZIP archive with no Word, Excel or PowerPoint
  document") under any other label, as is an archive with no
  `[Content_Types].xml`.
- One guard bounds the work: at most 50,000 directory entries (two and
  a half times the PPTX extractor's 20,000-member budget, so that
  extractor's own budget still decides a package between the two), an
  OLE2 header may declare no more FAT and DIFAT sectors than the file
  holds; the two metadata members together at most 8 MiB compressed
  (each charged just before it is read) and 8 MiB decompressed (counted as
  zipfile hands each chunk over, so a member that understates its size
  is bounded too) and at most 100,000 XML elements, with no text kept;
  and the classification is one pass over the names or the metadata
  entries. olefile's `loadfat` and `loaddirectory` are hooked:
  the table size is checked before any table sector is read, and the
  directory's sector chain is counted in the loaded FAT, so an
  over-budget or looping chain is rejected before the directory stream
  is read, its entry list allocated or its tree built. The child runs
  under 256 MiB of address space and 5 s of CPU, killed after 10 s.
  Measured plainly in the image: benign containers take 31 to 92 MiB and
  0.06 to 0.22 s (the largest an OLE2 directory at the entry budget,
  which needs 112 MiB of address space); a 250,000-entry directory, a
  looping chain and a header declaring 2^31 FAT sectors are rejected in
  0.06 s; a 5,000-entry sibling chain raises `RecursionError` in olefile
  (a `failed` row); a central directory filling the 32 MiB payload cap
  fails at the address-space limit in 0.6 s. The metadata reads: a
  package of 50,000 members listing a content type for each takes 94
  MiB and 0.42 s (112 MiB of address space); a deflate bomb is rejected
  by the guard in 0.16 s; 8 MiB of crafted metadata in any other shape
  takes under 96 MiB and 0.63 s; and one element of a million attributes
  (expat builds them before the element is reported) fails at the
  address-space limit (`MemoryError`) in 2.8 s. expat running out of
  memory is reported as that limit, never as malformed metadata.
- A malformed container, a limit hit or a guard rejection is `failed`
  by type name (`NotOleFileError`, `BadZipFile`,
  `ContainerDirectoryBudgetError`, `MemoryError`, ...), retried after 7
  days like any failure; the four `unsupported` results above are
  permanent (`PERMANENT_FAILURE_ERRORS`). Each is logged in one
  rate-limited WARNING with fixed text (`extractor container declined`
  or `extractor container failed`).

Identification receives the attachment's preserved, transfer-decoded
bytes before any nested-email interpretation (owner decision on #1416).
The parser reads a `message/*` part as an email, so an OLE2 or ZIP
container under such a label is kept as sent: a base64 or
quoted-printable part's decoded bytes are checked for a signature before
they are parsed as an email; an identity-encoded `message/rfc822`,
`message/global` or `message/external-body` part's bytes are recovered
exactly from the one header-less child the parser makes of them (it
records a missing header/body separator; that defect, not an empty
header block, is the guard, so a genuine delivery report with trailing
blank lines stays a report); and an identity-encoded
`message/delivery-status` part, which the parser splits into blocks at
blank lines, is read from a second stdlib parse of the message in which
that label is a leaf, matched part for part, done at most once per
message and only when such a part's first block has the defect (about
one more parse of the message; 0.19 s for a 23 MB message). Such an
attachment is a leaf: nothing inside it is walked or recorded, and it is
complete unless its transfer decode lost bytes. A differential catalogue
of every label, transfer encoding and container or genuine content,
top-level and inside an attached email, checks exact recovery and pins
the genuine parses (`tests/test_message_label_containers.py`). Other
`message/*` labels (`message/partial`, `message/disposition-notification`,
...) are not recovered yet (#1455), and the body-only walk the `eml` extractor renders an attached email with does not apply this yet (#1456).

The cache key does not move: a row is keyed under the label's module
after the constant-size prefix check (`extractors.extraction_module`),
and its `extractor` stamp records the module that ran, so a Word file
sent as `image/png` is keyed `image` and stamped `doc@2`. Keying it by
the identified module would need a child launch before every cache
lookup, outside the per-message budget. The result is deterministic in
the key, so the key stays sound; the same bytes under labels of
different modules store a row each. Each row records how its payload was
certified in `attachment_extractions.identifier` (see the cache row
above and *Schema versions*).

Legacy Office types (#694, #935, #957): `application/msword` / `.doc`,
`application/vnd.ms-excel` / `.xls` and `application/vnd.ms-powerpoint`
/ `.ppt` select the `doc`, `xls` and `ppt` extractors' namespaces. An
OLE2 or ZIP payload under them runs what its directory names (above):
a genuine `.doc`, `.xls` or `.ppt` its legacy extractor, an OOXML file
mislabelled as a legacy type its OOXML extractor. A payload that is
neither OLE2 nor ZIP (an RTF file, for one) under a `.doc` / `.xls`
label is recorded `unsupported` ("not an OLE2 or OOXML container",
#1227) without running anything, and its row is keyed under the legacy
module, so an occurrence with an OOXML label of the same bytes still
runs its own extractor. Under a `.ppt` label it is recorded
`unsupported` ("not an OLE2 compound file (labelled legacy .ppt)"),
and that row is served for later `.ppt` occurrences. Before #1416 an
OLE2 payload whose label selected an OOXML extractor or none was
recorded `unsupported` ("OLE2 compound file (legacy .doc / .xls or
encrypted Office file)") without any subtype check; such a row has no
`identifier`, so it is identified once (the cache row above). A row v0
wrote for such a payload carried no extractor and was migrated under the
'' module, so the startup sweep re-queues once each message whose
occurrence of it now selects a module.

- **`.doc`** text comes from `catdoc` (Debian's `catdoc` package in the
  indexer image), run with `-d utf-8 -w` (UTF-8 output whatever the
  locale, no line wrapping), under 64 MiB of address space and 10 s of
  CPU, killed after 60 s (#995). catdoc streams the document: measured
  in the image, the fixture and LibreOffice-written documents of 8.8 and
  21.9 MB take 0.002 to 0.11 s of CPU and complete under 4 to 5 MiB of
  address space.
- **`.xls`** is read by `xlrd` 2.0.2 in the extractor child
  (`extractors/extractor_child.py` running the walk in
  `extractors/xls_child.py`, started as `python -I`). xlrd's work on
  opening a workbook is not bounded by the payload size: its
  shared-string loop trusts a declared count and a signed skip length
  (6 KB of workbook can loop until memory runs out), and its OLE2
  directory walk recurses with no cycle check (`RecursionError`). So
  the child runs under 512 MiB of address space and 30 s of CPU, set
  before it starts, and the parent kills it after 45 s. Inside
  those limits it applies the XLSX extractor's cell and text budgets,
  plus a sheet budget of its own, counted while reading: at most 1,024
  sheets, loaded one at a time and unloaded after
  each; 5,000,000 cells visited (padding included, plus 64 per row),
  checked before the next sheet loads; 10,000,000 characters. A budget
  that cut the text is logged through the extractor-cap WARNING
  (`xls_sheets`, `xls_expanded_cells`, `xls_text_chars`). The child
  costs a Python start-up, the extractors package and an xlrd import
  per workbook, about 0.06 s and 39 MiB of address space in the image
  on the fixture workbook (0.02 s and 21 MiB while the child imported
  xlrd alone, before #1291); a 20 MB workbook of
  1,000,000 cells takes about 1.2 s and 145 MB. An xlrd error, or the
  child's own `MemoryError` or `RecursionError`, is recorded `failed`
  under its type name (`CompDocError`, `XLRDError`, ...).
- **`.ppt`** is read by Apache POI 5.5.1's HSLF reader
  (`SlideShowExtractor`: slide text, placeholders and text boxes alike,
  and speaker notes; masters and comments are left out) in a Java
  process (owner decision 2026-10-07, #957). `indexer/java/PptText.java`
  runs on a Java runtime trimmed with `jlink` to five modules
  (`java.base`, `java.desktop`, `java.xml`, `java.logging`,
  `jdk.unsupported`); both live under `/opt/ppt` in the image. The
  build's `ppt-builder` stage fetches the jars pinned in
  `indexer/java/pom.xml` with Maven (strict checksums on download, into
  a BuildKit cache mount with its own id that a later rebuild reuses,
  #1070; every artifact Maven resolves, from the mount or from Maven
  Central, must match its SHA-256 in the committed
  `indexer/java/checksums/checksums.sha256`, or the build fails, so an
  artifact another build on the same builder wrote to the mount is not
  used, #1117), compiles the
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

Attached emails (#922): `message/rfc822`, `application/eml` and
`.eml` select the `eml` extractor; `message/delivery-status` (a
bounce's machine-readable report) selects none and is recorded
`unsupported` ("no extractor"), unless its file name selects an
extractor. The payload is the attached email itself (the parser's
serialized form of a `message/rfc822` part, or an `.eml` file's
bytes). Its text is, for the attached email and then each
`message/rfc822` email nested in it, attached or inline, depth first
and in document order: a `[Attached message, depth N]` line for a nested one, the
first `Subject`, `From`, `To`, `Cc` and `Date` as labelled lines
decoded with the parser's header decoder, and the body the parser
would choose for that message, with no quote stripping. The inner
`From` is a claim inside a claim: it is searchable attachment text
only, never a participant, an authority input or a direction (#1235).
Each nested email is read once, where the walk meets it, so the
64 MB budget of transfer-encoded nested-email bytes goes in document
order. An inline nested
email under a `multipart/alternative` or `multipart/related` takes
part in that container's choice by its real content, as the default
parser's body does: it is rendered (label, headers, body) where it
sits only when chosen, and not at all when set aside, together with
any email inside it. One that could not be read (past the depth cap,
in an encoding no decoder reads, or not decodable) counts as
`eml_nested_messages` only when the body could have chosen it (review
round 12 on #1311).
The email's own attachments are not extracted here: inside a
`message/rfc822` part the parser records each as an occurrence of its
own. A nested `.eml` or `application/eml` file is such a leaf
attachment; the attachments inside an `.eml` file are not walked by
the parser at all. Every message of one payload shares one budget: 10,000 parts
walked and 200 text parts decoded (the parser's per-message caps), 20
levels of nesting, 64 MB of transfer-encoded nested emails decoded, 2,000
characters per header and 10,000,000 characters of text; a budget
that cut the text is logged through the extractor-cap WARNING
(`eml_*`) and marks the text incomplete, and so does a decode that
lost bytes: a body text part's (`eml_body_decode`), a nested email's
base64 or quoted-printable (`eml_nested_messages`; a quoted-printable
`=` that is neither an escape nor a soft line break, #1288), and a body
text part's quoted-printable or uuencode (the same `=` rule, or a
uuencode decode that fell back to its transport text or whose last
non-blank line is not `end`, #1402). A body text
part in any other encoding that is not identity (an unknown value) is
kept as decoded but counted as `eml_body_decode` too, since a
malformed one comes back as its transport text. A body
text part counts only when the body keeps it, or would have kept it
had it decoded whole: a loss in an alternative rendering set aside is
not counted, and neither is its charset fallback (review round 8). A
nested email's base64 also counts as lossy when the parse dropped a
line of its transport text (a first line starting with whitespace or
`From `). A part
declared `multipart/*` that the standard library could not decompose
(no boundary parameter, or a start boundary that never appears) is
counted as `eml_body_structure`, since none of its text is read, when
the body could keep it (not an alternative set aside, nor an
attachment); the same gap in the default parser is #1348. A message
header line the parse dropped (a first line starting with whitespace,
or a `From ` line after the first; a leading `From ` envelope line is
not counted) is counted as `eml_header_lines`, and so is the first
line of a body text part the body keeps, when the part has no blank
line after its boundary and that line starts with whitespace. A nested email in any
other transfer encoding (uuencode and its aliases included) is not
decoded: only its depth label is indexed, and it counts as
`eml_nested_messages`. The decoders' fallbacks are
counted as `eml_headers_degraded` (a header: an unknown charset, raw
8-bit bytes that are not UTF-8, encoded-words kept as sent),
`eml_filenames_degraded` (a part's filename) and
`eml_charsets_degraded` (a body text part's charset: an unknown label,
or bytes it replaced), reported by the parent's `degraded in the
child` line (#1314) and the attachments aggregate; they replace
characters rather than drop text, so they do not mark the text
incomplete (#1315). The extraction runs in the
extractor child (decision 42) under 1 GiB of address space and 60 s of
CPU, killed after 75 s: the standard library's parse of crafted
structure (800,000 parts or 4 million header fields at the 32 MB
`INDEXER_ATTACHMENT_MAX_BYTES` default) peaked at 688 to 995 MB and
took 3 to 5 s in the image, and html2text on 32 MB of HTML took 18 to
24 s; a 27 MB email with ten attachments took 0.3 s and 226 MB. A
payload nested past Python's recursion limit (about 1,000 levels) is
recorded `failed` as `RecursionError`.

All three, and the extractor child below (OOXML, `.xls`, images,
PDFs, attached emails, HTML and container identification), run through one subprocess runner (`extractors/_runner.py`).
It starts every tool through `extractors/_launcher.py` (`python -I`),
which lowers its own address space (`RLIMIT_AS`) and CPU time
(`RLIMIT_CPU`) to the limits the extractor passes, caps glibc at two
malloc arenas (the JVM needs it to start under its limit), and
`execve`s the tool, so the limits hold before the tool reads a byte;
`run_tool` has no default limits, and a test checks that each caller
passes both (#995). A process the tool starts inherits both limits.
The tool runs in its own session, and its whole process group is
killed with `SIGKILL` when the run ends (a timeout, the output cap, an
error or a normal exit), before the tool is reaped, so no process it
started outlives it (#1291). Each run gets a scratch directory the
runner owns, mode 700 under `/tmp` (tmpfs), which is the tool's
`TMPDIR` and holds the payload as a mode-600 file; it is removed with
everything in it once the group is dead, so a killed tool leaks no
files. The tool gets an argument list with no shell, no stdin and an
environment of `LC_ALL=C.UTF-8` and `TMPDIR` only; its stderr is
discarded, since it can quote the document; and its stdout is read
incrementally up to a byte cap. For catdoc and the `.ppt` reader the
cap is four bytes per character of `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`
(UTF-8's worst case, so the character cap decides the stored length),
never more than 40 MiB, which is also the cap when the character cap is
disabled (#1308). It bounds the tool's raw output, before surrounding
whitespace is stripped. The 40 MiB ceiling bounds the indexer's own
memory: an extraction holds about five bytes per output byte at its
peak (measured in the image), so about 200 MiB. Text past the cap is
not indexed, is reported as `doc_output_bytes` / `ppt_output_bytes` and
leaves the attachment's text marked incomplete. A timeout, a death by signal (a crash, or the
CPU limit) or a non-zero exit (a tool that fails an allocation under
the address-space limit exits with an error; the `.ppt` reader's
reserved encrypted-deck status is the one exception, below) records
`failed` with a fixed error type (`ToolTimeoutError`,
`ToolCrashError`, `ToolExitError`); nothing the tool printed reaches a
log or `last_error`.

The Python extractor child (`extractors/extractor_child.py
<module>`, for the OOXML formats, `.xls`, images, PDFs, attached
emails, HTML and container identification) reports its result in a
framed protocol the runner parses as it arrives (#1291): `P` lines for
progress, passed to the dispatcher's progress callback as they are
read so a long extraction can refresh the heartbeat; a `C <name>` line
per budget that cut the text, checked against the extractor's own
list; `N <name> <count>` for a count the extractor allows; an
`R <type name>` line per error the extraction caught and degraded from
(the PDF OCR fallback, #1293); and then
either `E <type name>` (the extraction raised) or `T <length>` and
exactly that many bytes of UTF-8 text, with nothing after. An extractor
that chooses its own name (`pdf`: `pdf-digital`, `pdf-ocr` or the
OCR-disabled sentinel) gets it on the text line, `T <length> <name>`,
checked against its own list. The
degradation an extraction records in the child (its text loss, OCR
pages skipped and the extractor counters) lives in the child's memory,
and the lines it logs go to the discarded stderr, so after the
extraction the child sends each non-zero value as an `N`
line under a fixed key (#1314); the parent adds the counts to its own,
marks the result's text incomplete when the child lost text, and logs
one rate-limited WARNING, `extractor <module> degraded in the child:`
followed by each key and its count. The `C`, `N` and `R` lines are
sent before an `E` line too, and an extractor that asks for them
(`pdf`) applies them before the error is recorded, so what happened
before a failure is counted and logged as it was in process (#1293).
No log line, format string or
argument from the child crosses. Output
that breaks this (an unknown frame or name, a short or long text,
bytes after the last frame, no result frame) or that the byte cap cut
is recorded `failed` as `ChildOutputError`, never cached as text. The
child's byte cap is its text budget at UTF-8's worst case of four
bytes a character plus the frames, so a working child never meets it. catdoc has no Homebrew formula, so its tests skip on a
Mac without it; CI installs it and fails if it is missing, and the
image carries it. The `.ppt` runtime is built for Linux, so the tests
that run the real reader skip on a Mac unless `INDEXER_TEST_PPT_HOME`
points at an exported `/opt/ppt`; CI exports it from the Dockerfile's
`ppt-runtime` stage and fails if it is missing.

The limits on every external program the indexer runs:

| Program | Address space | CPU time | Wall clock |
|---|---|---|---|
| catdoc (`.doc`) | 64 MiB | 10 s | 60 s |
| extractor child, container identification (OLE2 / ZIP payloads) | 256 MiB | 5 s | 10 s |
| extractor child, xlrd (`.xls`) | 512 MiB | 30 s | 45 s |
| Java with Apache POI (`.ppt`) | 512 MiB | 30 s | 45 s |
| extractor child, OOXML (`.docx`, `.pptx`, `.xlsx` and their variants) | 1 GiB | 30 s | 45 s |
| extractor child, attached emails (`message/rfc822`, `application/eml`, `.eml`) | 1 GiB | 60 s | 75 s |
| extractor child, HTML (`html` attachments, and a message's HTML body parts) | 1 GiB | 60 s | 75 s |
| extractor child, PIL and Tesseract (images), each process | 1,605 MiB | 4 × `INDEXER_OCR_TIMEOUT_SECONDS` + 30 s (270 s) | pages × (OCR timeout + 10 s) + 30 s (1,430 s) |
| extractor child, pypdf, Poppler and Tesseract (PDFs), each process | 2 GiB | the longer of 2 s × `INDEXER_PDF_MAX_DIGITAL_PAGES` and 4 × `INDEXER_OCR_TIMEOUT_SECONDS`, + 30 s (1,030 s) | 30 s + 2 s × digital pages, and with OCR on 2 × OCR timeout + OCR pages × (OCR timeout + 10 s) (2,550 s) |

Within the PDF child, Tesseract still has its per-page
`INDEXER_OCR_TIMEOUT_SECONDS` and Poppler the OCR render deadline
(see `INDEXER_OCR_TIMEOUT_SECONDS`); the child's limits bound both
(#1021, #1293).

OOXML extraction runs in a child process (owner decision 2026-10-08,
#1040). The DOCX and PPTX pre-open package budgets, the XLSX
eager-part budget and the dispatcher's ZIP guard all trust the member
sizes the ZIP central directory declares, and on Python 3.14 a member
read through `zipfile` decompresses the member's whole stream (up to
2 GiB per read) before it cuts the result to the declared size and
checks the CRC, which the sender also controls. A member that
understates its size therefore passes every budget and is still
expanded: a synthetic 0.5 MB document whose main part declares a few KB
but decompresses to 512 MiB peaked the indexer at about 1.1 GB. So the
whole DOCX, PPTX and XLSX extraction, the pre-open budgets, XLSX's
eager member reads and the walk included, runs in a child Python
process (the extractor child, `extractors/extractor_child.py`, started
as `python -I` through the runner) under 1 GiB of address space and
30 s of CPU, killed after 45 s; the dispatcher's ZIP guard, which reads
only the central directory, stays in the indexer (#1438), and container
identification runs in its own child first (#1416). The child reports in the runner's framed protocol
(above): the names of the walk budgets that cut the text, which the
parent checks against the format's own list and logs through the
extractor-cap WARNING as before, and the text; or the type name of the
exception the extraction raised. A package-budget or eager-part-budget
rejection is raised again in the parent and recorded `unsupported` as
before; any other type name (`BadZipFile`,
`DocxRelationshipChainError`, ...) is recorded `failed` under that
name, as it was in process. Output that breaks the protocol or is cut
at the 48 MiB byte cap is `failed` (`ChildOutputError`), never cached
as text. A `MemoryError` or
`RecursionError` in the child is the child's limit, not host pressure:
it is recorded `failed` by type, where in process the dispatcher
re-raised it; lxml reports a failed allocation as `XMLSyntaxError`, so
the address-space limit can also surface under that name. A timeout or
a death by signal (including the CPU limit) is `ToolTimeoutError` or
`ToolCrashError`, as for the legacy formats, and a failure's WARNING
is rate limited.

The limits were measured plainly in the indexer image (child peak RSS
and time): one-paragraph, one-slide and one-cell files take 44 to
48 MB and about 0.08 s, nearly all of it starting the child and
importing the library (0.2 s before the image shipped compiled
bytecode for the standard library, the dependencies and `src`, #1230;
the limits below were measured then); the largest benign cases were a synthetic
1,500-page report (220 MB, 0.7 s), 150,000 empty text boxes on one
slide (273 MB, 1.6 s), 1,000,000 spreadsheet cells (91 MB, 5.4 s), and
an XLSX shared-string table and stylesheet together just under the
eager-part budget (508 MB, 10.3 s). The 512 MiB understated member
(about 1,070 MB in any of the three formats) and 47 MiB of stored
element-dense XML inside the DOCX or PPTX package budgets (about
1,140 MB) fail under the limit. The extractor versions are not bumped
(`docx@7`, `pptx@3`, `xlsx@6`): a file inside the limits returns the
same text and status as before, and failures keep their type names.

Image extraction runs in the extractor child (PLAN.md decision 42,
#1292): `image.py` starts `extractor_child.py image <pages> <OCR
timeout> <Tesseract path>`, whose `image_child.py` decodes the image with Pillow (and
pillow-heif) and runs Tesseract through pytesseract, as the indexer did
before. Each Tesseract is a process the child starts, so it inherits
the child's limits (each process has its own) and is killed with the
child's process group when the run ends; pytesseract's temporary files
go to the run's scratch directory, which the runner removes. The child
has its own pixel ceiling, 48,000,000 pixels (#1401; the package's
30,000,000-pixel cap stays the cap of every other PIL consumer); the
indexer itself no longer imports pytesseract or pillow-heif for images.
A frame at or under the ceiling is OCR'd at full resolution. A JPEG or
MPO over it (up to 192,000,000 pixels) is decoded at half scale with
`Image.draft` before any pixel is decoded or rotated, the returned size
checked, as a lossy fallback (measured loss below): the parent logs the
`image_pixel_ceiling` extractor cap at WARNING with the factor
(`image decoded at 1/2 scale (lossy) to fit the pixel ceiling`), the
text is marked incomplete, and when the scaled-down image yields text an
INFO line gives its character count. Any other format over the
ceiling, a JPEG that half scale cannot fit, and a later TIFF frame over
it (Pillow checks each frame as it is loaded) are recorded
`unsupported` ("image exceeds the pixel ceiling", a permanent error).
While the header is read, Pillow's own limit is four times the ceiling;
its `DecompressionBombWarning` (promoted to an error) above that and its
`DecompressionBombError` above twice that are caught by class and
recorded the same way, so their messages go nowhere. A multi-picture
JPEG (MPO), such as a phone photo with a second picture, is OCR'd from
its primary picture only; when its header lists more than one, the
`mpo_frames` extractor cap reports the omission and the text is marked
incomplete. The ceiling and the half-scale factor are fixed, not
settings, so they record no cap value on the row (#1418); a change to
either is an `image` version bump. A
`P` frame after each OCR'd page refreshes the heartbeat; the frame cap
(`ocr_frames`, or `ocr_frames_unreadable` when the probe frame cannot
be read) crosses as a `C` frame, and the parent logs and counts it as
before (`ocr_capped_images`); the probe's exception type stays in the
child. Any other degradation recorded in the child crosses as `N`
frames like every child module's (#1314). The child strips the
joined text, as the dispatcher does, and then cuts it at 10,000,000
characters (`image_text_chars`, an extractor cap) so its output, read
whole by the parent, has a fixed bound (40 MiB plus 1 MiB of frames);
the indexer keeps at most `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`
(2,000,000 by default) anyway.
pytesseract saves each frame as PNG before Tesseract runs, and Pillow
cannot write some modes (CMYK, YCbCr, HSV, F, LAB, `RGBa`, `RGBX`) as PNG, so
the child converts a frame in such a mode to RGB, frame by frame, after
the EXIF rotation; modes PNG holds (`1`, `L`, `P`, `RGB`, the alpha
modes, ...) go through unchanged (#1400). That fix bumps the version
to `image@4` (owner approved): a CMYK image was recorded `failed`, a
failed row is re-run only when its bytes are extracted again, and the
startup sweep keys on the recorded version, so without a bump nothing
re-queues it. The bump re-OCRs every cached image payload once and
clears `text_complete` on them until their messages are re-indexed.
`image@5` (#1415, owner approved) stamps the "OCR disabled" result of
an image (see *PDFs labelled as images*); like 4, it re-OCRs every
cached image payload once while OCR is on, and re-runs the `failed`
image rows whatever the OCR setting.
`image@6` (#1401, owner approved 2026-10-10) is the pixel ceiling
above: images from 30,000,000 to 48,000,000 pixels, recorded `failed`
under `DecompressionBombWarning` before, are read at full resolution,
larger JPEGs at half scale, and the rest are `unsupported`. Like 4 and
5 it re-OCRs every cached image payload once while OCR is on; with OCR
off it re-runs the `image@5` "OCR disabled" rows once (no OCR runs;
they are re-stamped).
The version stayed `image@3` through #1292 (owner exception, 2026-10-08): a row cached
before this change may hold more text when the stripped OCR output
exceeds 10,000,000 characters and the character cap is off or above
10,000,000. An error in the child (`TesseractError`, `RuntimeError`
for a Tesseract timeout, its own
`MemoryError` or `RecursionError`) is recorded `failed` under its type
name; in process a `MemoryError` or `RecursionError` was host
pressure.

The limits were measured plainly in the indexer image (Tesseract 5.5.0,
which runs up to four OpenMP threads), through the real launcher, on
synthetic images (#1401). "Minimum" is the smallest address-space limit,
in 16 MiB steps with no OCR timeout, under which the extraction still
succeeds; the other columns are one run at the 1,605 MiB limit with the
60 s OCR timeout: wall clock, CPU time of the child's tree, the largest
resident size of the child and Tesseract together (both run at once,
each under its own limit) and the largest size of the run's scratch
directory (the payload and pytesseract's temporary PNG).

| Image (synthetic) | Payload | Minimum | Wall | CPU | Tree RSS | Scratch |
|---|---|---|---|---|---|---|
| 48 MP text page, JPEG (baseline, progressive or EXIF-rotated) | 10 to 12 MiB | 688 MiB | 29 s | 43 to 46 s | 1,050 to 1,052 MiB | 54 to 55 MiB |
| 48 MP MPO, two pictures (`mpo_frames`) | 23 MiB | 688 MiB | 30 s | 46 s | 1,063 MiB | 67 MiB |
| 48 MP CMYK JPEG (converted to RGB) | 36 MiB | 688 MiB | 35 s | 53 s | 1,075 MiB | 96 MiB |
| 48 MP HEIC (512-pixel grid tiles) | 23 MiB | 688 MiB | 36 s | 57 s | 1,083 MiB | 68 MiB |
| 48 MP PNG, RGB | 3 MiB | 672 MiB | 20 s | 40 s | 1,044 MiB | 6 MiB |
| 48 MP PNG, palette (`P`) | 2 MiB | 336 MiB | 19 s | 39 s | 420 MiB | 3 MiB |
| 48 MP PNG, all-white RGBA | 0.2 MiB | 672 MiB | 2 s | 2 s | 1,110 MiB | 0 MiB |
| 48 MP TIFF, LZW RGB | 9 MiB | 672 MiB | 18 s | 33 s | 1,051 MiB | 11 MiB |
| 48 MP TIFF, 32-bit integer (`I`) | 9 MiB | 464 MiB | 17 s | 32 s | 728 MiB | 11 MiB |
| 48 MP TIFF, three pages | 26 MiB | 672 MiB | 56 s | 103 s | 1,069 MiB | 29 MiB |
| 48 MP JPEG of random noise | 22 MiB | 944 MiB | 11 s | 11 s | 1,353 MiB | 159 MiB |
| 108 MP JPEG at half scale (baseline, progressive, rotated, MPO) | 18 to 41 MiB | 464 to 496 MiB | 23 to 26 s | 39 to 43 s | 680 to 702 MiB | 42 to 65 MiB |
| 108 MP noise JPEG at half scale | 36 MiB | not searched | 7 s | 6 s | 794 MiB | 108 MiB |
| 192 MP JPEG at half scale (baseline, rotated, MPO) | 33 to 66 MiB | 672 MiB | 31 to 32 s | 46 to 51 s | 1,076 to 1,109 MiB | 71 to 104 MiB |
| 192 MP progressive JPEG at half scale | 28 MiB | 832 MiB | 33 s | 51 s | 1,072 MiB | 67 MiB |
| 192 MP noise JPEG at half scale | 28 MiB | 864 MiB | 12 s | 12 s | 1,310 MiB | 148 MiB |
| 108 MP PNG or TIFF, 192 MP PNG or three-page TIFF | 4 to 61 MiB | (refused) | 0.1 to 0.2 s | 0.1 to 0.2 s | up to 101 MiB | up to 61 MiB |

At full resolution the same images need more than the old 1 GiB from
108 MP on (1,296 MiB for text and 2,032 MiB for noise at 108 MP;
2,112 MiB for text and over 3,072 MiB for noise at 192 MP), which is
why only JPEG and MPO, which decode at half scale, go past the ceiling.
1,605 MiB is the smallest limit that gives the largest case inside the
default 32 MiB byte cap, the 48 MP noise JPEG at 944 MiB, the 1.7 times
margin the 30,000,000-pixel limit was sized with. Past the default byte
cap, a 192 MP noise JPEG of 87 MiB needs 1,008 MiB (1.6 times) and
reaches a 1,466 MiB tree; a 108 MP one of 49 MiB, 592 MiB. The
32-bit TIFF row is a page whose samples span the 16-bit range; one
whose samples use only the 8-bit range is read as an empty page
(#1435). At the previous 30,000,000-pixel cap a text page needed
441 MiB and noise 606 MiB.

Memory under the indexer's 6 GiB `mem_limit`: the scratch directory is
on the `/tmp` tmpfs, which counts against the limit with the child and
Tesseract. The largest tree above inside the byte cap (1,353 MiB) plus
its scratch (159 MiB) is about 1.5 GiB; the bound is two processes at
1,605 MiB each plus the scratch, about 3.4 GiB. On the live indexer
(running `image@4` OCR under the old 1 GiB limit), the container used
1.6 GiB between extractions (the indexer process 1.5 GiB resident), and
its peak since its start was 4.65 GiB, children and tmpfs included.
Adding the larger tree's 0.5 GiB to that peak, or the 3.4 GiB bound to
the 1.6 GiB, stays under 6 GiB.

The half-scale fallback is lossy. On a synthetic catalogue (one font,
rendered at 0.7 of the line height, at one line spacing; 12 lines of
48 characters per image; lines 40, 60, 80, 120 and 200 px high; clean,
Gaussian blur, a keystone perspective, low-contrast ink and JPEG
quality 50; on 48, 108 and 192 MP canvases), scored by character
accuracy against the rendered text, half scale kept 58 of the 62 cases
that read at full resolution to within 2 points and read nothing of the
other four: low-contrast lines of 60 px (48 MP) and 120 px (192 MP),
and 40 px lines, clean and compressed (108 MP). A sweep of ink
contrast at 48 MP (lines of 50 to 90 px) lost 2 of the 12 cases that
read at full resolution, both low-contrast lines of 50 and 60 px.
Quarter scale lost every 40 px case at 48 MP and is not used. The
catalogue is one synthetic font and layout, so it shows where the
fallback loses text, not that it keeps it.

A page of dense text that needs more than the 60 s OCR timeout fails on
the timeout under the limit as without it (364 MB peak, measured at the
old limit). CPU time counts every thread, so
each process may use four times the OCR timeout plus 30 s of CPU, and
the timeout fires first; with the timeout off (`0`) the 60 s default's
limit applies. The wall clock allows every page its OCR timeout (with
none, the CPU limit) plus 10 s, plus 30 s: a 21-frame TIFF of text
pages at the cap took 140 s for its 20 pages. Starting the child adds
about 0.09 s per image (0.23 s against 0.14 s in process for a small
screenshot). The text is byte-identical to the in-process extraction,
so #1292 did not bump `image`.

**HTML conversion (#1294).** html2text has no per-input bound of its
own, so every HTML conversion the indexer runs goes through the
extractor child (PLAN.md decision 42): `html.py` starts
`extractor_child.py html <length> ...`, whose `html_child.py` converts
each document with a fresh converter (`ignore_links`, `ignore_images`,
no body wrap). The `html` attachment extractor sends one document, its
payload. The parser sends every `text/html` body part of one message
to one child once its MIME walk ends, so a message costs one launch
whatever its number of HTML parts; the attached-email extractor's body
walk already runs in its own child and converts there. The documents
cross as one payload file with their byte lengths as the child's
arguments (one to 200, ASCII digits, adding up to the file's size;
anything else is `ValueError` in the child), and come back in the text
frame, each stripped (as the dispatcher and the parser strip them) and
prefixed by its length in characters and a colon; a broken prefix or a
count that does not match is `ChildOutputError`. The stripped texts
share a budget of 10,000,000 characters: the text that crosses it is
cut there and the documents after it are not converted. A lone
surrogate, which UTF-8 cannot carry (a UTF-7 part can decode to one),
is replaced by `?` before a body part crosses, and the count is logged
at WARNING (`HTML body text held <n> lone surrogates; converted as ?`),
rate limited; like any decoder replacement it does not mark the body
incomplete (#1315). In process the surrogate stayed in the body and
the chunker's UTF-8 encode then failed the message.

An attachment the child cannot convert is recorded `failed` by type,
as for the other child extractors, and one the budget cut reports the
`html_text_chars` cap (the dispatcher's default 2,000,000-character
cap cuts first). For a message body, a conversion the child cannot
finish (one of its limits, an error in html2text, broken output) leaves
every HTML part of the message without text, and the budget leaves the
part that crossed it cut and the later ones without text. Either is
logged at WARNING, rate limited, with the type name and counts only
(`HTML body conversion failed (<type>); the text of <n> HTML parts is
left out`); the parts the body could have kept are counted under the
`html_body` parse cap, which clears `body_complete`; and the rest of the
message is indexed. An `OSError` while starting the child (no process
or scratch space left) fails the parse instead, so the queue retries
the message.

The limits are those of the attached-email child, whose body walk runs
the same html2text: 1 GiB of address space, 60 s of CPU, killed after
75 s. Plainly measured in the indexer image, child peak RSS and CPU
time for one synthetic 50 MB HTML body (the `INDEXER_PARSE_MAX_BYTES`
default) of each shape: plain paragraphs 247 MB and 2.3 s, tables
357 MB and 11.7 s, nested tables 195 MB and 16.7 s, dense entities
249 MB and 13.9 s, CJK text 205 MB and 1.4 s, `<pre>` 410 MB and 6.5 s,
unclosed `<b>` 322 MB and 37 s, unclosed `<div>` 692 MB and 29 s, list
items 553 MB and 25 s, `<br>` lines under 200 nested blockquotes
352 MB and 36 s. All of these complete, most at the text budget (50 MB
of plain paragraphs is about 51,000,000 characters). 50 MB of `<hr>`
(about 92,000,000 characters of output) and 50 MB of bytes that are
not UTF-8 fail with `MemoryError` at the limit, and so does a 32 MB
attachment of bytes that are not UTF-8 (950 MB in process). 1 GiB is
about 1.5 times the largest peak of the shapes that complete
(unclosed `<div>`, 692 MB); 60 s is 1.6
times the slowest.

Starting the child adds about 58 ms to the parse of a message with any
HTML part (median of 30 in the image; 71 ms for twenty HTML parts in
one message), where a small body converted in process in well under
1 ms, so a full reparse of 33,000 messages costs up to about 32 more
minutes, less for messages with no HTML part. The text of a document
inside the limits and the budget is byte-identical to the in-process
conversion: `indexer/tests/test_html_child.py` compares the two over a
catalogue of synthetic shapes through the real child, and the parser
pin and `make baseline` are unchanged, so `html`
keeps no version and nothing is re-extracted or re-parsed.

PDF extraction runs in the extractor child (PLAN.md decision 42,
#1293): `pdf.py` starts `extractor_child.py pdf <OCR on> <OCR pages>
<OCR timeout> <digital pages> <PATH>`, and `pdf_child.py` does the
whole extraction the indexer did before: pypdf's parse and digital text
walk, the page sizes that set the OCR DPI, Poppler's `pdfinfo` and
`pdftoppm` (through pdf2image) and Tesseract (through pytesseract). The
child checks its five arguments against one table (`pdf.OPTIONS`)
before any work. Each Poppler and Tesseract process is one the child
starts, so it inherits the child's limits and is killed with its
process group; pdf2image's copies of the payload and its rendered pages
and pytesseract's temporary files go to the run's scratch directory,
which the runner removes. The child's wall clock also bounds the
`pdfinfo` that pdf2image runs before each render with no timeout
(#868). The indexer's `PATH` is passed so the child finds the tools as
the indexer did. Each rendered page is closed once it is OCR'd, so the
child holds one decoded page at a time whatever `INDEXER_OCR_MAX_PAGES`
is. What the in-process extractor logged crosses as fixed tokens: the
digital-page and OCR page caps as `C` frames, whose configured limits
the parent records on the result (#1418) and logs as before
(`extractor cap pdf_digital_pages: ...`, `pdf OCR capped at ...`); the
page counters, the skipped pages and the text loss as `N` frames; the
lowered OCR DPI as `N pdf_ocr_dpi`, logged as the `pdf_ocr_dpi`
extractor cap; and an OCR fallback that failed as `R <type name>`
(`PDF OCR fallback failed: ...`). These are applied whether the child
then returns text or raises. The child strips the text, as the
dispatcher does, and cuts it at 10,000,000 characters
(`pdf_text_chars`, an extractor cap), as for images (owner decision
2026-10-08 on #1325), so its output, read whole by the parent, has a
fixed bound. A `FileNotDecryptedError` or `LimitReachedError` reported
by the child (matched by exact type name) is raised in the parent as
the repo-owned `PdfEncryptedError` or `PdfLimitError` and recorded
`unsupported` (#931), so the indexer process never imports pypdf
(#1464); any other error, the child's own `MemoryError` and
`RecursionError` included, is `failed` under its type name, where in
process those two were host pressure that stopped the indexing pass.
The move takes no version bump (`pdf@6` stays; owner exception
2026-10-10, as for `image` on #1325): on a catalogue of synthetic PDF
shapes (digital, scanned, mixed, encrypted, broken pages, over each
page cap, OCR on and off, OCR failing, a lowered DPI;
`indexer/tests/test_pdf_catalogue.py`) every stored field, count and
progress callback is the same as the in-process extractor's, and
`make baseline` is unchanged. A result is stored differently only for
a PDF whose parse passes the child's limits, and in two text-cap
cases: with `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` set to `0` or
above 10,000,000, a PDF whose stripped text passes 10,000,000
characters is stored cut there and marked incomplete; and a text with
a whitespace run from below the configured cap through the
10,000,000-character cut is stored without that whitespace and with no
configured cap recorded (`text_complete` 0, `extracted_chars_cap` 0),
where in process it kept the whitespace up to the cap and recorded the
setting. The hardcoded cut records
no configured cap, so raising a setting does not refresh such a row,
and cached `pdf@6` rows over 10,000,000 characters keep their fuller
text (`TestTextCeiling` in `indexer/tests/test_pdf_child.py`).

The limits were measured plainly in the indexer image (pypdf 6.19,
Poppler and Tesseract 5.5.0 from Debian trixie), through the real
launcher, on synthetic PDFs inside the 32 MiB byte cap. "Minimum" is
the smallest address-space limit, in 16 MiB steps, under which the
extraction still succeeds; the other columns are one run at the 2 GiB
limit with the 60 s OCR timeout and the default page caps: wall clock,
CPU time of the child's tree, the largest resident size of the
processes running at once, and the largest size of the run's scratch
directory.

| PDF (synthetic) | Payload | Minimum | Wall | CPU | Tree RSS | Scratch |
|---|---|---|---|---|---|---|
| 500 pages of dense text, OCR off | 0.3 MiB | 96 MiB | 1.6 s | 1.6 s | 58 MiB | 0 MiB |
| 500 pages of kerned text (a `TJ` entry per glyph) | 0.4 MiB | 112 MiB | 14 s | 14 s | 76 MiB | 0 MiB |
| one page of 32 MiB of path operators | 13 MiB | 832 MiB | 7.3 s | 7.3 s | 792 MiB | 12 MiB |
| one page of 71 MiB of path operators | 28 MiB | 1,776 MiB | 16 s | 16 s | 1,743 MiB | 28 MiB |
| 20 scanned letter pages of text at 200 dpi (the OCR cap) | 11 MiB | 144 MiB | 39 s | 93 s | 171 MiB | 236 MiB |
| 14 scanned letter pages of noise | 30 MiB | 144 MiB | 9.4 s | 9.3 s | 211 MiB | 201 MiB |
| one scanned page of text at the 10 MP render budget | 0.9 MiB | 208 MiB | 2.3 s | 4.8 s | 234 MiB | 58 MiB |
| one scanned page of noise at the 10 MP render budget | 5.7 MiB | 240 MiB | 1.7 s | 1.6 s | 309 MiB | 62 MiB |
| a letter page holding a 48 MP scan of text | 3.3 MiB | 144 MiB | 2.1 s | 4.5 s | 151 MiB | 24 MiB |
| a letter page holding a 48 MP scan of noise | 28 MiB | 144 MiB | 1.1 s | 1.1 s | 213 MiB | 65 MiB |

pypdf decodes at most 75,000,000 bytes of one page's content, and its
text walk holds about 25 bytes per byte of path operators, so the 71 MiB
page is close to the most one benign page can ask of it; 2 GiB is 1.15
times that. Tesseract and Poppler stay under 256 MiB because the
10,000,000-pixel page budget bounds the raster they work on. A content
stream of text-showing operators costs pypdf more than linear time
(1, 2 and 4 MiB of `(ab) Tj` took 1.1, 2.9 and 7.9 s at 83 to 200 MiB,
from payloads of 2 to 10 KB), so a crafted one is bounded by the CPU
limit, the wall clock or the address-space limit and recorded `failed`; a page
tree that declares 2^20 pages in a few objects is refused by pypdf
(`LimitReachedError`, `unsupported`) before any page is read, and a
20,000-page PDF is read to the digital-page cap only. The wall clock
allows 2 s per digital page (a kerned page took 0.03 s, the 71 MiB page
16 s) and, with OCR on, the render deadline twice (the timed page count
and the renders, then pdf2image's untimed page counts) and every OCR
page its timeout plus 10 s, plus 30 s; with a page cap off its default
is budgeted. The CPU limit is the longer of the digital walk's
allowance and four times the OCR timeout (Tesseract's four threads),
plus 30 s, so the per-page timeouts fire first.

Memory under the indexer's 6 GiB `mem_limit` (#1450): the child parses
under 2 GiB, and before it starts the first Poppler or Tesseract
process it lowers its own address-space limit, soft and hard, which
each tool inherits, to the larger of what it maps then plus 256 MiB and
768 MiB (`pdf_child._limit_for_ocr`). The tools' own minimums are
240 MiB (Tesseract, a 10 MP page of noise), 80 MiB (`pdftoppm`) and
48 MiB (`pdfinfo`). A child whose limit would pass 1,280 MiB (it maps
more than 1,024 MiB when OCR would start) starts no tool: the OCR
fallback fails (`PdfOcrMemoryBudgetError`) and a mixed PDF keeps its
digital text. Each Poppler call renders at most 5 pages
into its own scratch directory, removed once they are OCR'd, so the
scratch on the `/tmp` tmpfs holds the payload twice (the runner's copy
and pdf2image's) and at most 5 rendered pages of up to 30 MB each,
whatever `INDEXER_OCR_MAX_PAGES` is. Measured in the image with pypdf
6.20 (wall and CPU times were not re-taken): the 71 MiB page still
needs 1,776 MiB; the child maps at most 274 MiB when OCR starts, after
that page peaked at 1,829 MiB, and grows by at most 17 MiB while it
OCRs; every tool ran at 768 MiB; two mixed PDFs, a 64 MiB path page
followed by four 10 MP scans or by 40 scanned letter pages (OCR page
cap 100), peaked at 1.58 to 1.60 GiB of tree RSS during the digital
walk, with no tool running, and at most 171 MiB of scratch; 60 scanned
pages (OCR page cap 100) held at most 121 MiB of scratch. The bound in the OCR phase is the
indexer's own 1.6 GiB (image section above), the child and one tool at
1.25 GiB each and about 0.25 GiB of scratch, about 4.35 GiB; in the
digital phase it is 1.6 + 2 GiB plus the payload copy, about 3.6 GiB;
both are under the 6 GiB limit.

PDFs labelled as images (#1415): a payload under an image label
(an `image/*` type or an image extension) that starts with `%PDF-`
runs the PDF extractor, not the image extractor, which cannot read it.
The dispatcher checks that fixed prefix before the OCR gate, so with
OCR off such a PDF is still read from its digital text layer (a
scanned one is recorded as a scanned PDF, "OCR disabled ... scanned
PDF"). Only the prefix is checked; the row is the `pdf` module's,
shared with a `.pdf` occurrence of the same bytes (#928). With OCR off,
an image's "OCR disabled" result is stamped `image@5`; a row recorded
with no stamp (before this routing, possibly a PDF) is re-run once
whatever the OCR setting, by the cache check and by the startup sweep
(see the `attachment_extractions` cache and *OCR*), and the re-run
writes a stamped row or a `pdf` one, so it is not matched again.

Binary payloads labelled as text: the text extractor decodes whatever
it is given, so a PDF, ZIP (or OOXML), OLE2, PNG, JPEG or GIF file sent
as `text/plain`, or named `.txt` / `.csv` / `.md` with no usable
Content-Type, was indexed as replacement characters. When a payload
bound for the text extractor starts with one of those formats' fixed
signatures (`%PDF-`, `89 50 4E 47 0D 0A 1A 0A`, `FF D8 FF`, `GIF87a` /
`GIF89a`), the dispatcher records it `unsupported` ("binary payload
labelled as text") without decoding it (#932). An OLE2 or ZIP payload
under a text label is identified by its directory instead (#1416,
above); before that, it too was recorded "binary payload labelled as
text", and such a row has no `identifier`, so it is identified once. Only those fixed prefixes are checked; the
bytes decide, and the row is the text module's. An occurrence
labelled with the real type (`.pdf`, an image) runs that type's
extractor and has its own row (#928). A binary file labelled only as
text is therefore not extracted (#969).

Word templates: the template MIME type
(`application/vnd.openxmlformats-officedocument.wordprocessingml.template`)
and `.dotx` route to the DOCX extractor (#937), with the same ZIP
guard, container identification, walk and caps as `.docx`. python-docx's
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
keeps its own type, and since the extraction runs in a child process
(#1040, above) it is recorded `failed` by that type rather than
re-raised as host pressure. No row was cached for such a payload
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
compressed sizes, declare more than 48 MiB together, number more than
5,000, or hold more than 4 MiB of relationship (`.rels`) parts. A
synthetic 500-page formatted document with 2,000 pictures and 10,000
hyperlinks has about 2,000 members, 16 MiB of expansion and 2.1 MiB of
relationships. The declared total (#1033) counts members stored
uncompressed, which do not expand, so python-docx parses at most 48 MiB
of XML whatever `INDEXER_ATTACHMENT_MAX_BYTES` is (plainly timed,
stored element-dense XML raised peak memory by about 1.1 GB at 48 MiB).
Pictures count against it too: real-shaped synthetic documents near the
default 32 MiB payload cap declared 29 to 35 MiB, so 48 MiB is the
default cap plus the 16 MiB of expansion above. With a raised
`INDEXER_ATTACHMENT_MAX_BYTES`, a document declaring more than 48 MiB
(large photos) is recorded `unsupported`. An over-budget document is recorded
`unsupported` ("document exceeds a pre-open package budget"), not
`failed`, since the same bytes trip the budget on every run (#1032; see
*Permanent extractor failures* below). Neither the budgets (#1036, nor
the declared total, #1033) nor the mapping (#1032; PR #1068's bump to
`docx@6` was reverted by #1075) bumped the DOCX version, because the
walk after the open had no budget yet; the walk budgets below (#1031)
came with the bump to `docx@7` (6 stays taken by the reverted bump).
So at the next start every cached DOCX row stamped `docx@5` or
`docx@6` is re-queued once and re-extracted through the budgeted walk:
a document read in full before reads the same text unless it is over
a walk budget, and a package-budget row recorded `failed` before the
mapping is re-recorded `unsupported`. Dead-lettered messages are
skipped and keep their old rows until `make requeue-dead`.
`DocxRelationshipChainError` (#945)
still applies to a chain under these budgets.

After the open, the DOCX extractor walks the document's XML itself,
one child element at a time (#1031). python-docx's
`iter_inner_content()` and `Paragraph.text` select a container's
paragraphs and tables, a paragraph's runs and hyperlinks, and a run's
text elements with XPath unions, which libxml2 merges in time
quadratic in the number of siblings: plainly timed, 1 MiB of a
paragraph alternating runs and hyperlinks took 4.2 s and a synthetic
1,500-page report 15 s, and 31 MiB of either shape ran past five
minutes. The walk keeps the same elements in the same order
(`indexer/tests/test_docx_walk.py` pins the text on a catalogue of
shapes) and reads the report in about half a second. It counts its
work against four budgets per document: 500,000 blocks (each
paragraph and table in the body, headers, footers and table cells,
plus 20 for each section's header and footer references; a header
part several sections define is read, and charged, once per section),
500,000 table rows and cells, 2,000,000 text elements (runs,
hyperlinks, and the text, tab and break elements in runs) and
10,000,000 characters. The first budget to run out keeps the text read
so far and logs its extractor-cap WARNING (`docx_blocks`,
`docx_table_cells`, `docx_text_elements`, `docx_text_chars`). The
synthetic report uses about 75,000 blocks, 18,000 table cells and
270,000 text elements. The worst synthetic shapes inside the package
budgets now extract in about a second, and the walk adds little to the
memory the open already peaks at (about 780 MiB for 31 MiB of empty
paragraphs, where the old walk peaked at 1.3 GB).

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
(#1042), with the same ZIP guard, container identification, pre-open
package budgets, walk and caps as `.pptx`. `pptx.Presentation` refuses every main-part
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
opened, a password-protected legacy `.ppt` ("encrypted legacy .ppt
(open password required)"; #983): `PptText.java` exits with a reserved
status (10) for POI's `EncryptedPowerPointFileException`, matched by
exact class in Java, and only the `ppt` extractor reads that status,
and an image over the image child's pixel ceiling that half scale
cannot fit ("image exceeds the pixel ceiling"; #1401), decided from the
image's header.
Each is matched by exact exception class;
anything else stays `failed`. The row is keyed by the module that
raised the error (#928), so it is served only to occurrences that run
that module on the bytes; an occurrence whose label runs another
extractor has its own row and extracts. It is stamped with the
extractor version (`pdf@6`), so a later version bump, for example one
that raises a budget, refreshes it; the bumps that came with the
`pdf`, `xlsx` and `pptx` mappings (`pdf@5`, `xlsx@6`, `pptx@3`)
refreshed the `failed` rows the previous versions wrote, since the
startup sweep re-runs only stale rows, never aged `failed` ones. `docx`
was not bumped with its mapping, while its walk was unbudgeted; the
bump to `docx@7` with the walk budgets (#1031; see the DOCX budget
paragraph above) refreshes those rows the same way. `ppt` was not bumped (#983): the deck's text is
the same (none) either way, and a bump would re-run every cached deck
at the next start to reclassify the encrypted ones, so an encrypted
deck recorded `failed` (`ToolExitError`) before the mapping converted
the same way, on its first re-run more than 7 days on, until the bump
to `ppt@2` for the derived output cap (#1308) re-ran every cached deck
once. Each logs a
rate-limited WARNING
(`extractor <module> declined ...; recorded unsupported, not retried`).
A pypdf limit hit inside one page's text extraction (a `/ToUnicode`
map over its size limit, for example) is not one of these: like any
per-page error, that page is counted in `pdf_pages_failed` (and OCR'd
when OCR is on) and the other pages' text is kept.

The PDF extractor is version 6 with pypdf 6.20.0: stream decoding limits
and malformed page-tree recovery changed, so cached PDF results from
earlier extractor versions are re-extracted once. The usual OCR-disabled
and dead-letter rules still apply.

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
Pillow plugin (#691). They go through the same byte cap, the image
child's pixel ceiling and decompression-bomb handling as other images
(a HEIC is not a JPEG, so over the ceiling it is `unsupported`); Pillow checks the size in
the header before anything is decoded. Only the primary image is OCR'd;
thumbnails, depth maps and auxiliary images are not decoded. The HEIF
image-sequence extensions `.heics` / `.heifs` are not routed by name;
a sequence sent with an `image/` MIME type reaches the image extractor
like any image.

`pillow-heif` bundles its own copies of the native libheif and libde265
(HEVC) decoders, plus a libx265 encoder that is never used. Both
decoders have a long record of memory-safety CVEs and run on
attacker-supplied files. They run in the image extractor child
(`image_child.py`, #1292), under its address-space and CPU limits, not
in the indexer process; process separation is not filesystem or network
confinement (PLAN.md decision 42). The owner accepted that risk on
2026-10-04, when they still ran inside the indexer process. Debian security updates do not cover the bundled
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
itself, since nothing selects it. There is no setting for this.

With OCR off, images, and PDFs whose whole text layer is below the
threshold, are recorded as "OCR disabled". When the indexer starts
with OCR on, it re-queues once each message carrying such an
attachment as an image or a PDF, except dead-lettered messages. An
image row recorded with no stamp (before #1415, when a PDF under an
image label was recorded this way) is re-queued once whatever the OCR
setting. A PDF
with usable digital text is indexed from it while OCR is off, and its
scanned pages are not re-read when OCR is turned on later.

### Cost bounds

| Knob | Default | Purpose |
|---|---|---|
| `INDEXER_ATTACHMENT_EXTRACTION_ENABLED` | `true` | Master switch — turns the whole pipeline off if needed |
| `INDEXER_OCR_ENABLED` | `true` | Disables all OCR paths (image + PDF fallback) |
| `INDEXER_ATTACHMENT_MAX_BYTES` | `33554432` (32 MiB) | Skip very large attachments — bounds CPU/memory for huge zips. Sized for the 10–30 MB scanned PDFs common in real mail; an `.eml` under the default `INDEXER_PARSE_MAX_BYTES` (50 MB) carries at most ~36 MB of base64-encoded attachment. Raising it re-queues, once at startup, the messages whose attachments were cached `too_large` and now fit |
| `INDEXER_OCR_MAX_PAGES` | `20` | Cap pages OCR'd per PDF or multipage TIFF. Raising it re-queues, once at startup, the messages whose cached results it cut (#1418). |
| `INDEXER_OCR_TIMEOUT_SECONDS` | `60` | Per-page Tesseract timeout — bounds runaway OCR on a crafted high-noise image — and the deadline for rendering a scanned PDF's pages with Poppler. pdf2image's own page count before each render takes no timeout, so the indexer first times one bounded page count: one over half the deadline is an OCR timeout, and each render's timeout holds back that time. A page count much slower on pdf2image's call than on the timed one is bounded by the PDF child's wall clock (#868, #1293). Set `0` to disable both. Image OCR also runs under a CPU limit per process of 4 × this value + 30 s (270 s with `0` or the default), which `0` does not lift (#1292); the PDF child's CPU limit and wall clock are sized from it too (#1293). |
| `INDEXER_PDF_MAX_DIGITAL_PAGES` | `500` | Cap pages walked by the digital pypdf path — protects against text-only PDFs with thousands of pages. Set `0` to disable. A PDF cut here logs the `pdf_digital_pages` extractor-cap WARNING (#903). Raising or disabling it re-queues, once at startup, the messages whose cached results it cut (#1418). |
| `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` | `2000000` (~500 pages) | Truncate extracted text before persisting in `attachment_extractions`. Bounds SQLite row size for very long OCR'd PDFs. Set to `0` to disable. Every extractor that runs in the extractor child (DOCX, PPTX, XLSX, `.xls`, attached emails, HTML, images and PDFs) also stops at 10,000,000 characters of its own, whatever this is set to (for XLSX, so shared strings repeated across many cells cannot expand without limit, #294). The legacy `.doc` and `.ppt` tools' output is read up to four bytes per character of this cap, never past 40 MiB, whatever it is set to (#1308). Text cut by any of these logs an extractor-cap WARNING (`extracted_chars`, or the format's own `*_text_chars` cap such as `xlsx_text_chars`) and counts in the attachments line's `extractor_caps` (#903). Raising or disabling it re-queues, once at startup, the messages whose cached results it cut; the 10,000,000-character and 40 MiB limits are not settings and are not refreshed (#1418). |

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
opened, as does one whose members declare more than 48 MiB together
(#1033: members stored uncompressed do not expand, and media count too,
so a deck with a large video past it fails; real-shaped synthetic decks
near the default 32 MiB payload cap declared about 30 to 32 MiB), one
with more than 20,000 members or more than 8 MiB of
relationship (`.rels`) parts, since python-pptx builds a part for every
related member and walks every relationship as it opens; the dispatcher
records it `unsupported` ("presentation exceeds a pre-open package
budget", #1032), not `failed`. The declared total did not bump the
`pptx` version, so a deck read in full before it keeps its cached text.
It then counts its walk against four budgets
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

### Per-message extraction budget (#1236)

Every attachment the cache and the batch cannot serve is extracted in
a process of its own (the extractor child, catdoc, the POI JVM, and the
Poppler and Tesseract processes a scanned PDF starts), and the
extraction cache is keyed by content hash, so a message with thousands
of distinct small parts would otherwise hold the single ingestion
worker for one launch each, with the stall guard's clock restarted per
attachment. One pass of one message is therefore given a budget,
counted in `_resolve_extracted_text` after the cache lookup and before
each dispatch: **64 process launches** and **5 seconds** of extraction
(`EXTRACTION_TURN_LAUNCHES`, `EXTRACTION_TURN_SECONDS` in
`indexer/src/attachment_indexing.py`; fixed, always positive, no off
switch). Launches are every `subprocess.Popen` the indexer process
starts, counted from Python's `subprocess.Popen` audit event, so a
library's own processes count too, plus the ones an extractor child
reports it started (`N launches`: Tesseract per image frame; Poppler's
page counts, version check and render and Tesseract per page for a
scanned PDF). A child
killed before it reports adds only its own launch; its run still counts
in the seconds.

- **Turn, not ceiling.** The first dispatch of a pass is always
  admitted, so each pass resolves at least one attachment. An admitted
  attachment runs to its own bounds (the extractor's timeouts and
  limits), so a pass can end past the budget by one attachment's run.
  For the tiny payloads measured below that overrun was at most 0.25 s
  (a one-page scanned PDF, five launches); its upper bound is the
  slowest extractor's own timeout.
- **Deferral, never loss.** Past the budget, each remaining uncached
  attachment is deferred: its occurrence gets
  `attachments.extraction_deferred_at` and `text_complete = 0`, keeps
  the chunks it had, and nothing is written to `attachment_extractions`
  for it. While one copy of a payload is deferred, no write in that
  message deletes chunks from the payload's shared slice.
- **Continuation.** The message is not marked succeeded: Phase 2c
  continues it with `queue.defer` at stage `extract`
  (`EXTRACTION_DEFERRED_ERROR`), due at once and keeping its reason (a
  reparse stays a reparse), in the same transaction as the pass's
  results, deferral marks, chunks and vectors, so they commit or roll
  back together. A lone survivor's attempt charge is refunded in that
  same transaction and stays watched by the stall guard until it
  commits, so a crash there leaves the message charged and marked
  `interrupted`. A rolled-back pass leaves no mark and no continuation,
  and is charged as an ordinary `db_write` failure. An occurrence the
  message's current parse no longer has (a parser change dropped it) is
  deleted in the pass's commit, deferral mark included (#1375; see
  *Reparse in place*). The continuation
  sorts behind the jobs already due, so continued messages and new mail
  take turns. It spends no attempt; a failure in a later pass does.
- **Progress.** A continuation (a claimed job still carrying the
  `extract` stage and its fixed text) resolves only the pending
  occurrences: no recorded `text_complete`, or a deferral mark. An
  occurrence whose result already applied is skipped outright: no cache
  read, chunking, chunk-ID read or write, whatever its cached row's age,
  so an expired `failed` row is not re-run and a message always gets
  closer to done. Its committed chunks stay, and its payload's shared
  slice is neither replaced nor cleared while another copy of the same
  bytes is pending. In the pass where a copy resolves and none is
  deferred, the payload settles: each copy resolved earlier is read once
  per extractor module from its cached row as it stands, and the slice
  is rewritten from every copy's current text, so chunks kept for a
  since-refreshed copy do not survive. A payload settles once, so this
  stays linear. A pending occurrence gets one cache read per pass, and one
  deferred again with its mark already written is not rewritten. Every
  pass updates the thread vector in its commit from the thread's
  chunk-vector sum (see *Thread vector sums*), reading no chunk vector
  of the thread. An `EXTRACTOR_VERSIONS`
  bump clears `text_complete` on exactly its occurrences, which makes
  them pending again. The startup sweep does the same for every other
  refresh class: it clears `text_complete` to NULL as it finds them, in
  bounded batches of one transaction each (holding one batch of IDs at
  most), on the occurrences of a `no extractor` or
  OLE2 result their label now routes to a module, a `too_large` result
  that now fits, an "OCR disabled" result once OCR is on (an unstamped
  image one whatever the OCR setting, #1415), a cached result whose
  container certification is due (#1416), and a cached
  result with no completeness record (whatever the occurrence's own
  record). A queued continuation then resolves them as pending; its row
  (stage, error, attempts, due time) and the deferral marks are left
  as they are, and an unrelated resolved occurrence (an expired
  `failed` row) is still not reopened. The sweep also re-queues a
  message that carries a mark but has no job (a pass with attachment extraction switched off
  marks it succeeded). New
  intent to index the file (a watcher event, a startup re-queue) resets
  the job, and that pass resolves every occurrence by the usual cache
  rules, under the same budget. A pass interrupted by a crash is
  replayed the same way.
- **Visibility.** The attachments line counts `deferred` occurrences,
  `deferred_messages` and `deferred_resumed` (a WARNING while any are
  deferred); a continuation pass does not count again the occurrences
  resolved in earlier passes. The queue heartbeat counts continued jobs
  in `extraction_deferred`, and logs `attachment extraction caught up`
  once when none remain. `get_mailbox_status` reports them as
  `extraction_deferred` (they keep the index non-current);
  `query_attachments` and `search_attachments` report the occurrence as
  `deferred` from its own mark, not the payload's row (and the
  extracted-text lane reports no copy of a payload while one is
  deferred), and `get_attachment` gives a fixed reason and no text.
  The chunks kept meanwhile stay in the evidence that `ask_mailbox`,
  `get_evidence`, `extract_from_emails`, `brief_issue` and
  `check_conclusion` read, flagged `extraction_deferred` on each
  passage, carrier and citation, with the fixed note "retained indexed
  text; extraction refresh pending" in the prompt headers and prose,
  and counted on each tool's timing line (owner, 2026-10-09).

**How the values were chosen.** Plain `time.perf_counter` timings in
the indexer image (Linux, `docker build indexer`), synthetic payloads
only, 20 distinct payloads per format through `extractors.extract`,
one untimed warm-up each:

| Payload | Median | Max | Launches |
|---|---|---|---|
| DOCX / XLSX / PPTX, valid, one line | 0.080–0.082 s | 0.097–0.102 s | 1 |
| DOCX, ZIP that is not a package (failed) | 0.073 s | 0.104 s | 1 |
| `.doc`, OLE2 prefix and noise (catdoc, failed) | 0.006 s | 0.006 s | 1 |
| `.xls`, OLE2 prefix and noise (xlrd child, failed) | 0.060 s | 0.072 s | 1 |
| `.ppt`, OLE2 prefix and noise (POI JVM, failed) | 0.106 s | 0.130 s | 1 |
| `message/rfc822`, one line (eml child) | 0.060 s | 0.073 s | 1 |
| PNG, OCR on (image child) | 0.106 s | 0.124 s | 1 |
| scanned PDF, one page, OCR on | 0.213 s | 0.247 s | 5 |

64 launches is about 5 seconds of the cheapest child launches
(0.06–0.08 s), so the launch count binds for floods of tiny parts and
the seconds for slow ones. A message with up to 64 child-format
attachments still finishes in one pass. These timings predate container
identification (#1416): an OLE2 or ZIP payload now costs one more
launch (0.06 to 0.22 s, measured in the image) before its extractor, so
a message of up to 32 Office attachments finishes in one pass, and the
OLE2 rows above, whose noise is not an OLE2 file, now fail at
identification instead.

The budget spreads a large message's work over passes, with other mail
between them, and each continuation pass replays part of the message:
it parses the whole file, reads each pending occurrence's cache row once
and chunks the body. Before #1356 it also read every chunk vector of
the thread twice (the Phase 1 seed and the Phase 2c mean); the table
below was measured then. The whole path, budgeted against
unbudgeted (one pass), measured in the indexer image with the real
extractors, a stub embedder (embedding cost excluded), synthetic
messages with realistic text (one chunk of about 40 or 430 words per
part) and plain timing:

| Message | Unbudgeted | Budgeted | Passes | Budgeted ÷ unbudgeted | Per continuation pass (median): wall, parse, body chunking, thread-vector rows read (time) |
|---|---|---|---|---|---|
| 9,990 attached-email parts, 8.4 MB (one child launch each) | 646 s | 1,042 s | 157 | 1.61 | 6.6 s, 0.19 s, under 0.01 s, 10,050 rows (1.4 s) |
| 8,500 attached-email parts, 45.2 MB (near the 50 MB parse cap) | 549 s | 889 s | 133 | 1.62 | 6.6 s, 0.43 s, under 0.01 s, 8,514 rows (1.2 s) |
| 400 one-page scanned PDFs, 24.6 MB (OCR, five launches each) | 199 s | 229 s | 40 | 1.15 | 5.7 s, 0.21 s, under 0.01 s, 420 rows (0.02 s) |

Resolved occurrences cost a pass nothing. The thread-wide vector reads,
which grew with the parts already resolved (up to about 20,000 rows on
the last pass at the part cap), are gone (#1356, *Thread vector sums*):
a continuation pass at the part cap now spends about 5 ms on its
thread vector instead of about 2.8 s per read. What remains is the
re-parse and one cache read per pending occurrence; removing them
(staged payloads) is the rest of #1356.

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

The indexer stamps the schema version in `schema_version`, and a fixed
`PRAGMA application_id` (`SCHEMA_APPLICATION_ID`) that marks the
schema after the v0 renumbering. A fresh install creates the current schema
directly; an existing index runs the forward migrations in
`indexer/src/migrations/` at startup, each in its own transaction, so a
failure leaves the last applied version stamped and the next start
retries it. A version above the code's, a missing migration file, or a
database without that application ID stops startup (`docs/troubleshooting.md`).

Before deploying a release that adds a migration, take a snapshot with
`make backup-index BACKUP_DIR=<directory outside the checkout>`: a
migration that commits but turns out wrong is then undone with
`make restore-index BACKUP=<file>`, after recreating the containers
from the release before the migration, instead of a full rebuild from
Maildir (`docs/troubleshooting.md`, "Back up and restore the index").

A migration that adds parser-produced per-message data ends with the
shared reparse statement, so the worker fills the new data for mail
already indexed without re-embedding unchanged chunks (see *Reparse in place*).

| Version | Migration | Change |
|---|---|---|
| 0 | (initial schema) | First deployed schema (2026-10-03). |
| 18 | `0018_account_collection_state.sql` | `account_collection_state` (#1592; see *Collection status*): mbsync's latest collection outcome per account, empty until the indexer first reads mbsync's collection status file. Nothing the parser produces is added, so no reparse is queued. |
| 17 | `0017_occurrence_readers.sql` | The readers move to the occurrences (#1567, #1568, #1569). `message_occurrences` is rebuilt, rows and ids unchanged, to accept the `scan` provenance; a message the v14 seed could not bind gets a retired occurrence at its stored path (a WARNING counts them), so every message keeps one; `pending_deletions` is keyed by occurrence, each message's old tombstones carried over as one row with their latest mark, which the next sweep clears for a message that is still live; `messages.content_hash` is indexed; `messages.filepath`, `folder`, `seen`, `flagged` and `replied` and `message_thread_map.filepath` are dropped (each drop rewrites its table once). Nothing the parser produces is added, so no reparse is queued; claimant, chunk and thread IDs and vectors are unchanged. On a synthetic 36,500-message index (14,600 threads, 160 MB) the startup open including the migration took 0.4–0.5 s (three runs, Apple M5 Max). |
| 16 | `0016_selected_clock_indexes.sql` | Partial send/delivery clock and claimant-ID indexes for definite-match listing pages (#1123). No columns, evidence rows, reparses or embeddings change. |
| 15 | `0015_attachment_inventory_current.sql` | `messages.attachment_rows_current` (#1338; see the attachment-inventory discussion below): NULL until reparsed, then 1 when extraction is enabled or 0 when disabled, set by phase 2c in the same transaction as the occurrence writes. The migration queues the standard reparse, keeping unchanged chunks and vectors. |
| 14 | `0014_account_occurrences.sql` | `accounts` (#1596; see *Account registry*): the configured `ACCOUNT_SCOPE` and `SOURCE_MODE` provider, bound in the migration's own transaction through connection-local functions; update and delete triggers. `message_occurrences` (#1566; see *Mailbox occurrences*), seeded with one occurrence per message whose `indexed_files` record carries its content hash, relative to the configured `MAILDIR_PATH`; a stored path outside that root fails the migration closed with fixed text, and a message whose path holds other bytes gets none (a WARNING counts them). The migration ends with the shared reparse statement: the reparse observes each seeded occurrence and binds the files the seed cannot see (an unmapped byte-identical copy), with no embedding call; dead-lettered jobs stay dead. Claimant, chunk and thread IDs and vectors are unchanged. |
| 13 | `0013_message_headers.sql` | Bounded ordered `message_headers` and nullable `messages.headers_complete` (#1079); existing rows start unknown and a queued reparse fills them. |
| 12 | `0012_extraction_identifier.sql` | `attachment_extractions.identifier` (#1416; see the `attachment_extractions` row above and *Extractor dispatch*): how the payload's container was certified, `container@<n>` or `none@<n>`, NULL (unknown) on every row cached before it. The bytes are not in the database, so the migration ends with the shared reparse statement (see *Reparse in place*): every occurrence passes through the lookup once, which records `none@<n>` on a row whose payload has neither signature and identifies and re-extracts the rest. Unchanged chunks keep their IDs and vectors; text a re-identified container now yields is new chunks, sent to the configured embedder, so the upgrade embeds the newly readable Office attachments once. A container sent under a `message/*` label is kept as sent from this release, so its occurrence gets a new attachment ID on the reparse: the old occurrence, FTS row, chunks and vectors are removed and the new one is extracted and embedded. The startup sweep's identification arm queues the same rows and clears their occurrences' `text_complete` first, so a message whose extraction continuation the reparse leaves queued refreshes them too. |
| 11 | `0011_extraction_cap_record.sql` | `attachment_extractions.ocr_pages_cap`, `digital_pages_cap` and `extracted_chars_cap` (#1418; see the `attachment_extractions` row above): the configured limit that cut the result, 0 when it did not, NULL (unknown) on every row cached before it. The extractor writes them, so no reparse is queued; the startup sweep re-queues once the messages using an incomplete `success` / `empty` row with no record (the bootstrap arm), and the re-extraction records the columns. |
| 10 | `0010_thread_vector_sums.sql` | `thread_vector_sums` (#1356; see *Thread vector sums*): each thread's exact chunk-vector sum and count, so the thread vector is derived without reading every chunk vector. The migration writes no row: the first write that touches a thread fills its row, and the backfill sweep fills the rest in bounded batches. Nothing is re-parsed or re-embedded. |
| 9 | `0009_attachment_extraction_deferral.sql` | `attachments.extraction_deferred_at` (#1236; see *Per-message extraction budget*): when the budget deferred the occurrence's extraction to a later pass, NULL otherwise, with the partial index `idx_attachments_deferred` on `(claimant_id, attachment_id)` over the deferred rows (the MCP server's per-chunk flag reads it). No message was deferred before it, so every row starts NULL and no reparse is queued. |
| 8 | `0008_unknown_sent_dates.sql` | Unknown send dates (#1080; see *Message time*): `messages` is rebuilt with a nullable `sent_at`, `sent_at_status` (`parsed` / `missing` / `invalid`, NULL until assessed) and `first_indexed_at`, and `effective_at` becomes `COALESCE(occurred_at, sent_at, first_indexed_at)`. Every existing value is kept, the participant rows are copied aside and back in the same transaction, and the migration queues a reparse, which stores an unknown date as NULL and moves the old fallback to `first_indexed_at` without embedding calls. Until the reparse reaches a message without a delivery date, a date bound counts it as indeterminate. |
| 7 | `0007_operator_identity.sql` | `operator_addresses` and `operator_identity` (#824; see *Operator identity*), seeded `unconfigured` with no addresses until the indexer's next start loads `config/identity.toml`. No per-message column, so no reparse is queued. |
| 6 | `0006_attachment_text_complete.sql` | Attachment text completeness (#1242): `attachments.text_complete` (0 / 1, NULL until assessed, no default) and `attachments.text_extractor`, and `attachment_extractions.text_complete` (NULL when unknown). Every existing row starts NULL and the migration queues a reparse (see *Reparse in place*), which re-extracts each cached `success` or `empty` result once, since none has a record yet, except an `-ocr` row while OCR is off, refreshed once OCR is on (#1285). |
| 5 | `0005_message_completeness.sql` | Per-message completeness on `messages` (#1086): `subject_complete`, `from_addresses_complete`, `to_addresses_complete`, `cc_addresses_complete`, `attachments_manifest_complete`, `body_complete` (0 / 1, NULL until assessed, no default) and `caps_json`. Every existing row starts NULL and the migration queues a reparse, which fills them without embedding calls; a dead-lettered job keeps its message NULL until `make requeue-dead`. Until the reparse reaches a message, a subject, text, attachment, address or authority filter that does not match it counts it as indeterminate. |
| 4 | `0004_participant_names.sql` | `message_participant_names` and `messages.participant_names_complete` (#1140), seeded with each participant's first name; the migration queues a reparse (see *Reparse in place*). |
| 3 | `0003_extraction_ocr_pages_skipped.sql` | `attachment_extractions.ocr_pages_skipped` (#891): the scanned pages the PDF OCR cap left unread, NULL when unknown, no default. Every existing row starts NULL and counts as nothing; the column is the extractor's, not the parser's, so no reparse is queued, nothing is re-extracted and no `EXTRACTOR_VERSIONS` entry is bumped. |
| 2 | `0002_messages_sender_ambiguous.sql` | `messages.sender_ambiguous` (#1144): 0 / 1, NULL until assessed, no default. Every existing row starts NULL and the migration queues a reparse of every indexed file, which fills it without embedding calls; a dead-lettered job keeps its message NULL until `make requeue-dead`. Until the reparse reaches a message it matches no `authority_class` filter. |
| 1 | `0001_extraction_cache_per_module.sql` | `attachment_extractions` keyed by (content hash, extractor module); `attachments.extractor_module` (#928). Each v0 row keeps its result and stamp and is keyed by its stamp's module (`docx@5` -> `docx`, `pdf-ocr@4` -> `pdf`), or '' when it has no stamp (`unsupported`, `too_large`); each occurrence is pointed at its payload's row, as before. No `EXTRACTOR_VERSIONS` bump comes with it, so nothing is re-extracted for the re-keying alone. An occurrence whose label selects another module than its row's moves to its own row the next time its message is reprocessed. |

## Deletion Reconciliation (mirror by default)

`mbsync` is configured `Sync Pull` + `Expunge None`, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP `\Deleted` (Maildir `T`)
flag. Without reconciliation, the local SQLite index keeps those messages
forever.

Each Maildir copy of a message is its own occurrence (see *Mailbox
occurrences*). A message is **live** while it has a live occurrence: an
active one that is not `T`-flagged. A `T`-flagged copy never counts as
live, whatever folder it is in, and a Trash copy that is not `T`-flagged
does (PLAN.md decision 47, #1567). Losing the last live occurrence is
claimant-wide: it counts every occurrence of the message, in every
account.

The indexer runs a reconciler that handles this in two phases. It is on
by default (mirror mode: upstream deletions leave the local index);
`INDEXER_DELETION_ENABLED=false` turns it off (archive mode: the index is
append-only and keeps deleted mail). An unrecognized value fails indexer
startup rather than picking a mode.

1. **Tombstone** — a sweep at startup and every
   `INDEXER_DELETION_SWEEP_INTERVAL_SECS` (default 1 h) first reconciles
   the occurrences with the Maildir (*Occurrence reconciliation* below),
   then records a tombstone in `pending_deletions`, keyed by occurrence,
   for each occurrence of every message with no live occurrence left,
   and removes the tombstones of every message that has one again. The
   live `on_moved` watchdog handler does the same for the message whose
   file it saw renamed: a rename that adds `T` to its last live copy
   tombstones it at once, and a rename that drops `T` (a restore)
   removes its tombstones in the same write. Tombstones exist only while
   a message has no live occurrence, and a sweep keeps the marks a
   message already has, so the earliest mark is when the loss was first
   seen. No primary data is mutated at tombstone time, so the soft-delete
   is fully reversible if mbsync un-flags the file on a later pull.
2. **Reap** — once a message's earliest tombstone is older than the grace
   window (`INDEXER_DELETION_GRACE_DAYS`, default 7 days) and it still has
   no live occurrence, the reaper removes its rows from
   `message_thread_map` (its `messages` row, occurrences and tombstones
   go by cascade), unmarks every path holding its bytes in
   `indexed_files` (its active occurrences' paths, a retired one's while
   `indexed_files` still records its bytes there, and every other
   recorded path with its content hash, so a copy that comes back after a
   transient outage is re-indexed by the next Maildir walk) and deletes
   any indexing job still queued for those paths, all in one transaction,
   which first re-checks that every message it removes still has no live
   occurrence and a tombstone past the grace window, and that none of
   those paths, nor a file with the same uniq in its folder's `cur` or
   `new` (an upstream restore renames it), is back on disk without the
   `T` flag (`stat` calls first, then the pass's directory listings, each
   directory listed at most once per pass, #336), so a restore, a copy bound by a sweep, a
   restore the sweep has not seen yet (logged at INFO), or a restore and
   a fresh loss after the reaper read its candidates is left for a later
   pass. It then either rebuilds
   the parent thread from the surviving messages on disk (re-parsed from
   an active occurrence, a live one first; the thread vector is the
   survivors' chunk mean, derived from the stored sums with no
   re-embedding, #1356) or deletes the thread entirely when nothing
   remains. A survivor with no active occurrence (it is losing its last
   copy too, within the grace window) cannot be re-parsed, so its thread
   waits, visible in the blocked-thread count and WARNING, like a thread
   whose survivor is unreadable (#1159). Only when no chunk survives does
   the rebuild embed a subject (the first non-empty subject among the
   survivors, oldest first, or `(empty thread)` when none has one); an
   embedding failure there makes the reaper back off and retry on the
   next pass.

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

**Occurrence reconciliation.** The watcher carries an occurrence
through every rename it observes. What it misses (renames while the
indexer was down, a lost event) the reconciliation scan finds
(`indexer/src/occurrence_scan.py`, #1567). It walks the account's
Maildir root once and compares the files with the account's active
occurrences:

- **Missed renames.** An active occurrence whose file is gone, while its
  folder holds a file with its Maildir uniq (a flag change, a `new`/`cur`
  move), moves to that file once the file's bytes are verified to be the
  message's: by the `indexed_files` identity (size and mtime match the
  record, which a rename keeps, and the recorded hash is the message's),
  else by hashing the file. A same-uniq file with other bytes is counted
  as `unverified` and changes nothing.
- **Found copies.** A file no active occurrence holds, whose bytes are an
  indexed message's (looked up by `messages.content_hash`), is bound to
  it as an occurrence with provenance `scan`: a byte-identical copy the
  migration seed or the parse never bound, such as a `T`-flagged file the
  drain parks, a dead-lettered job, or a copy moved across folders while
  no occurrence held it (#1141). Its bytes come from its `indexed_files`
  record when size and mtime match. Otherwise only a `T`-flagged or
  dead-lettered file is hashed, once per process for each path, size,
  mtime and inode, by every caller of the scan that passes a hash cache
  — every one of them does, mirror mode's periodic sweep and archive
  mode's rename sweep alike (#1612; before it, archive mode's call
  sites passed none, so this branch and the content backstop below
  silently never ran there); any other unindexed file is new mail the
  Maildir walk queues, and its parse binds it.
- **Vanished files.** An active occurrence with neither is retired only
  when the scan is complete: the walk read every directory under the
  root. An unreadable directory or a missing root makes the scan partial:
  it retires nothing (the occurrence stays active and is counted as
  unreachable) and logs a WARNING with the number of unreadable
  directories. A partial or failed scan never establishes absence, so it
  cannot start a message's grace period. Completeness is per account: a
  scan covers one account's root and never touches another account's
  occurrences.
- **Content backstop.** A held path (its file did not move this pass,
  so none of the above read its bytes) is hash-verified against its
  occurrence's claimant, bounded per pass and cached by exact identity
  (size, mtime, inode) so a later pass does not re-read an already-
  verified file — see "The content backstop" below for the budget and
  its cost. A verified mismatch retires only that occurrence (the file
  is still there, unlike a vanished one) and requests a reparse,
  counted `stale`.

Each write re-checks, in its own transaction, that the occurrence or
path is as the scan saw it, and a retire re-checks the disk for the file
or a file with its uniq, so a rename the watcher records meanwhile wins.
A move or bind also re-checks that the file still has the size, mtime
and inode it had before its bytes were verified, so a file replaced in
between is refused.
The rename sweep (`sweep_paths`), which runs at startup, before each
periodic rescan's Maildir walk and on folder-watch recovery, in archive
mode too, runs the same scan without tombstoning anything; recording the
renames first keeps the walk from re-indexing them as new mail.
Retiring removes a locator, never a message: archive mode retires the
occurrence of a vanished file but never reaps. The INFO lines of both
sweeps count what they did (`renamed`, `retired`, `bound`, `unreachable`,
`unverified`, `stale`, and for the reconciler `tombstoned`, `cleared` and
`hashed`). A failure of the rename sweep before a periodic rescan is
logged at WARNING with its type and does not stop that rescan's walk,
and its recovery is logged like the other recurring steps'.

Byte-identical files share one claimant ID (see *Claimant IDs*), so
removing one copy retires its occurrence and the message stays indexed
through the other; trashing one copy marks its occurrence trashed and
the message stays live while another copy is live. Only when every copy
is `T`-flagged or gone is the message tombstoned, and the reap then
unmarks every path with its bytes.

A **mass-delete brake** (`INDEXER_DELETION_MAX_BATCH_PCT`, default 5%) stops a
reap pass outright, reaping nothing, when the messages past the grace
window outnumber that fraction of total indexed messages; it does not
reap a slice. It counts messages (claimant IDs), however many
occurrences each lost. Transient Bridge outages (vault rebuilds, folder
renames, auth glitches) can cause mbsync to `T`-flag a huge batch at
once; the brake stops the reaper from acting, while still recording
tombstones that will clear themselves if mbsync reverts the flags. Up to
10 messages per pass are always allowed regardless of the percentage so
that routine cleanup on small mailboxes is not gated by the 5% default.
For an intentional bulk cleanup, `make force-deletion` (#1609) runs one
pass past the brake: it stops the indexer, runs `src/force_deletion.py`
in a one-shot indexer container (the indexer's own sweep and reap, with
the configured grace window and `force` set in memory, logged as
`brake=forced`, then the WAL scrub), and starts the indexer again.
Nothing is stored, so every later pass has the brake on; each thread's
reap is its own transaction, so a pass that stops midway leaves the rest
tombstoned. The persistent `INDEXER_DELETION_FORCE` setting was removed,
and `scripts/validate-env.sh` refuses it. Beside that unchanged global
brake, a **per-account brake** is computed only while more than one
account has an active occurrence (#1491 enables several; today the
registry holds one, #1596): a message belongs to every account holding
an occurrence of it, and an account whose past-grace messages outnumber
`max(10, INDEXER_DELETION_MAX_BATCH_PCT` × its messages`)` has all of
them held back for that pass, logged at ERROR with the account's
numeric id and the counts, so a mass deletion in a small account is
caught though the mailbox-wide count stays under the global brake.
`make force-deletion` overrides it too.

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
`T`-flagged, so its occurrence is no longer live, but a Trash copy
arrives and is indexed: with the same bytes it is another occurrence of
the message and keeps it live; with other bytes it is a message of its
own, and the INBOX one is tombstoned. Either way the mail is reaped only
after it is purged from Trash (plus the grace window). To keep it out of results
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
- the main loop, which runs the queue drain (every pipeline write) and
  the periodic reconciler, WAL and recovery passes

`make reparse` and `make requeue-dead` write from a separate process on
their own connection, serialized by SQLite's file lock.

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
problem and the always-null join. `message_chunks_fts` and
`attachments_fts` follow the same pattern, through
`message_chunks.fts_rowid` and `attachments.fts_rowid`.

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
time (`next_attempt_at`; reparse jobs form a second class with one
slot per batch, see "Reparse in place" below), and the initial scan
queues each unindexed
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
  1. Thread has chunk vectors → their mean, derived from the thread's
     chunk-vector sum (see *Thread vector sums*). Canonical seed for
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
- Phase 2b (embed) error → a 429 on the whole batch is not any
  message's fault and splitting it would multiply the request rate
  against a throttling provider: the whole batch is deferred without
  a probe or a split, `attempts` unchanged, a WARNING with the
  message count is logged, and the breaker opens for its usual
  backoff or the provider's `Retry-After` (seconds form), whichever
  is longer, capped at the breaker's 10 min cap. Any other error: a one-string health probe
  decides whose fault it is:
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
in two SELECTs under one lock (fresh mail first, one slot kept for a
reparse row) so the gather phase cannot re-claim the same row
repeatedly while Phase 2c is deferred.

### Recovery sweep for chunkless zero-vector threads

A recovery sweep (`_recover_zero_vector_threads`) runs in every
`initial_index` call, after the Maildir walk and before the drain, AND
periodically from the main loop
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
    being parsed or extracted, or a lone survivor in the bulk embed or
    its commit, when the process died (see below).
  - **`dead` row** (deterministic Phase 2 failure exhausted
    `max_attempts`): skipped — left alone as an operator-visible
    terminal state. Auto-resurrecting would burn embedder load
    against the same poison-pill payload on every container
    restart and contradict `initial_index`'s own `is_dead` skip.
    To rescue a dead-lettered file, an operator confirms the
    underlying cause is fixed and runs `make requeue-dead`
    (optionally `CLASS=...`), which resets dead rows to `queued`
    with a fresh attempt budget.

With deletion reconciliation on, `T`-flagged files are skipped: the
reaper owns them. Healthy chunkless threads (e.g., subject-fallback
threads from blank-body messages) are not touched — the DB query filters out
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
and `EmbedResponseError` in full, and anything else as its type name. A
message the standard library's email parser cannot parse within the
recursion limit (around a thousand nested `message/rfc822` levels) fails
the same way on every attempt, so it is dead-lettered on the first one,
like an oversized file, with `permanent_source_failure` and the fixed text
`unindexable: message nested too deeply for the email parser` (#1296); a
`RecursionError` raised anywhere else in the parse stage is retried as
before. When
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
slow scanned PDFs (each allowed up to its child's wall clock, 2,550 s
at the defaults; see the limits table) is not cut off. The restart policy brings the indexer back with the attempt
counted.

The container healthcheck (`indexer/healthcheck.sh`) fails once the
heartbeat file is 10 minutes old. Besides each message and embed
request, the PDF and image extractors refresh it after every page they
read or OCR, so a scanned PDF that runs for ~20 minutes stays healthy
(#485). Pages do not restart the stall guard's clock, which stays per
attachment; a page that hangs refreshes nothing.

### Reparse in place (#1078)

A parser change that adds per-message data (a new `messages` column, a
new per-message table) but changes no chunk ID, chunk text, embedding
input (body text, the subject line of the first chunk), Message-ID or
threading reaches mail already indexed by a **reparse**: every indexed
file is queued with reason `reparse`, and the worker runs it through
the ordinary pipeline. Phase 1 re-parses the file and rewrites its
per-message rows through `upsert_thread`; Phase 2a finds every chunk ID
already stored and queues nothing to embed; a chunkless thread keeps
its stored subject-fallback vector instead of embedding it again (one
still at the zero placeholder is repaired as usual). So a reparse
embeds only text that has no stored chunk. Attachment text comes from
the extraction cache unless the lookup refreshes the cached row (an
older extractor version, a missing completeness or limit record, a
limit raised since it cut the text, a container certification due, or
a `failed` result past its 7-day window): a chunk whose text is
unchanged keeps its ID and is not embedded again, and text the
re-extraction changes is embedded as new chunks (the v12 reparse embeds
the newly readable Office attachments this way). Chunks are cut with
the current `INDEXER_CHUNK_*` budgets, so a chunk that budgets changed
since a message was indexed cut differently gets a new ID and is
embedded. A thread vector still at the zero placeholder, and a thread
whose last chunk goes with a dropped attachment occurrence (#1375),
embed the thread's subject, once for each such message in a pass
(chunkless siblings in one batch each reserve the fallback). Retries, dead letters, the stall guard and heartbeats are the
queue's own, and a message that fails to parse dead-letters instead of
failing a migration. A reparse job whose file is gone while its path is
still indexed waits for the rename like any other job for an indexed
file (see the `FileNotFoundError` outcome under the queue's stage
outcomes below). A reparse can drop addresses from a
message's rows (the #1144 address budget), but the thread's
`participants` and `senders` keep them until a reap or a rebuild
(#1173).

A pass (a reparse or any other) also removes the attachment
occurrences the message's current parse no longer produces, for
example a row indexed under a filename a later parser fix decodes
differently (#1375). In phase 2c's transaction, after the parse's own
occurrence writes, each such occurrence's `attachments` row (with any
deferral mark) and `attachments_fts` row are deleted; the message's
chunk slice of its payload is deleted only when no remaining
occurrence of the message carries the same payload, with the deleted
vectors subtracted from the thread's chunk-vector sum, and a cached
extraction no remaining occurrence uses is purged in the same
transaction. A later message of the same batch that was prepared
against that cached row restores it unchanged (`extracted_at`
included) when it writes its occurrence; if it fails to commit, or the
process stops first, nothing is restored and the retry extracts again.
The thread's `has_attachments` is recomputed from its messages. A continuation pass
that drops an occurrence whose extractor module no surviving copy of
the payload ran rebuilds the payload's slice from the surviving
copies. Other messages' occurrences of the payload are untouched. This runs only while
attachment extraction is on (with it off, a pass writes no occurrence
rows and removes none). The attachments line counts the removed rows as
`dropped`. `make reparse` clears leftovers from parser fixes released
before this.

The v4 migration (`0004_participant_names.sql`, #1140) is one: it
creates `message_participant_names`, seeds it with each participant
row's stored first name, so name matching keeps what it saw before the
upgrade, adds `messages.participant_names_complete` as `NULL` on every
row, and queues the reparse, which adds the further names and sets the
flag. Until the reparse reaches a message (or, for a dead-lettered one,
until `make requeue-dead`), only its first names are stored and a name
or fragment address filter that does not match it reports it as
indeterminate rather than a miss.

The v5 migration (`0005_message_completeness.sql`, #1086) is another:
it adds the per-message completeness columns (see *Per-Message
Records*) as `NULL` on every row and queues the reparse. Phase 1 of
each reparse job writes the subject, address and attachment-list flags
and `caps_json`; phase 2c, which on a reparse keeps every stored chunk
and embeds nothing, writes `body_complete`. Until the reparse reaches
a message (or, for a dead-lettered one, until `make requeue-dead`),
every subject, text, attachment, address or `authority_class` filter
that does not match it reports it as indeterminate.

The v6 migration (`0006_attachment_text_complete.sql`, #1242, #1285)
adds attachment text completeness (see *Attachment text completeness*)
as `NULL` on every row and queues the reparse. Phase 1 re-reads each
message, so the parser's payload loss is known again; phase 2c
re-extracts each cached `success` or `empty` result once, since none has
a record yet, and writes every occurrence's flag. Occurrences stay
`NULL` for a dead-lettered message (until `make requeue-dead`), for a
file no longer on disk, for an `-ocr` row while OCR is off (the sweep
re-queues them once it is on), and for mail reparsed while
`INDEXER_ATTACHMENT_EXTRACTION_ENABLED` was off (phase 2 writes no
attachment rows then; run `make reparse` after enabling it).

The v15 migration (`0015_attachment_inventory_current.sql`, #1338) adds
`messages.attachment_rows_current`, NULL until reparsed. Phase 1 resets it to
NULL; phase 2c sets it in the same transaction as the occurrence writes and
stale removals: 1 when extraction is enabled, 0 when disabled. Deferred and
retained current occurrences count as current even without complete extracted
text. The migration queues the standard reparse, keeping unchanged chunks and
vectors. This marker certifies currentness, not manifest exhaustiveness:
`aggregate_messages` counts current positive payload identities even with an
incomplete manifest, and reports definite matches whose inventory is not known
exhaustive (`manifest = 1 AND (has_attachments = 0 OR current = 1)`). Unknown
inventories make identity counts lower bounds. A disabled-extraction pass leaves
old rows untouched and cannot certify them as current. The aggregation selects
its clock through the shared predicates, materializes message memberships once,
and counts attachment identities separately so occurrence fan-out does not
inflate message totals or repeat predicate evaluation. Sender/domain period
breakdowns are flat, capped pages over UTC calendar month/year cells.

The v8 migration (`0008_unknown_sent_dates.sql`, #1080) rebuilds
`messages` with `sent_at_status` `NULL` on every row and queues the
reparse (see *Message time*). Phase 1 of each job stores the send date
as parsed, or NULL with `missing` / `invalid`, and for an undated row
keeps the old fallback as `first_indexed_at`, so its effective time and
its thread's span do not move. Until the reparse reaches a message
without a delivery date (or, for a dead-lettered one, until
`make requeue-dead`), every date bound reports it as indeterminate.

The migration that adds such data triggers the reparse itself: after
its DDL it ends with `REPARSE_ENQUEUE_SQL` (`indexer/src/queue.py`),
copied verbatim (a test checks every migration that queues a reparse
uses it):

```sql
INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;
```

The statement only inserts, inside the migration's transaction, so the
parse runs later in the worker, not under the migration's write lock.
`filepath` is the queue's primary key and the conflict clause leaves
every existing job as it is: a pending or retrying job keeps its
reason, attempts, error and due time (it re-parses the file in full
when it runs), and a dead-lettered job stays dead; `make requeue-dead`
remains the way to retry it. `make reparse` (`src/reparse.py`, run in
the indexer container like `make requeue-dead`) runs the same
statement by hand, for recovery.

Reparse jobs are due when queued, all at once, but they do not hold
back other work (#1142). The queue hands out foreground jobs (every
reason but `reparse`: fresh mail, recovery, rescans and re-extraction)
first, so mail that arrives during a reparse is indexed by the next
batch. While both kinds are due, each batch (`claim_batch`,
`Database.queue_fetch_due_batch`) keeps one slot for the oldest due
reparse job and fills the rest with foreground jobs; capacity one kind
leaves unused goes to the other, so no slot is left empty, and each
kind keeps its due order. With a batch size of 1
(`INITIAL_INDEX_BATCH_SIZE` or `INDEXER_STEADY_STATE_BATCH_SIZE` set to
1) the two kinds take turns, tracked in memory only. So a reparse
advances by at least one job per batch, except while the embedder
breaker is open (no batch is claimed) and while a job left
`interrupted` by a crash runs alone first. While a reparse runs, the
heartbeat's `oldest_due_age` grows: it reports the oldest due job, a
reparse job behind the foreground ones, not a stalled drain. A reparse
queued by a migration at startup is still drained by the initial index
before startup deletion reconciliation runs. A claim is two indexed
SELECTs, each of which may walk past every due job of the other kind;
over 33,000 queued jobs it takes about 1.4 ms (#1142). The reparse
needs no schema change of its own: `reason` is free text with no
`CHECK` constraint.

Visibility: with the queue heartbeat (every 5 min) the indexer logs
`reparse: remaining=<n> parked_trashed=<n> reparsed_since_last_heartbeat=<n> dead=<n>`
while reparse jobs that can drain are queued, then one `reparse
complete: <n> message(s) reparsed since the indexer started, <n>
dead-lettered` line, at WARNING when any dead-lettered; a reparse
drained between two heartbeats still gets its completion line.
`remaining` leaves out reparse jobs parked as trashed: they drain only
if the file is restored, so they are counted in `parked_trashed`
instead, and the completion line notes any still parked (#1331). `get_mailbox_status` reports
the queued reparse jobs as `queue.reparse` (a subset of `pending`,
`retrying`, `deferred` and `extraction_deferred`), names them in the not-current reason, and `make status`
prints a line saying search finds those messages but the data the
upgrade adds is missing until the reparse finishes.

A change that alters chunk IDs, chunk text or embedding input needs a
rebuild instead (PLAN.md Phase 2, "Two kinds of reindex"; today, a
rebuild from Maildir, `docs/troubleshooting.md`).

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
while the observer was not running, an inotify queue overflow — is
therefore indexed eventually rather than omitted until the next
container restart.

An inotify queue overflow is the one miss the indexer can see (#1108).
When a burst fills the kernel's per-instance event queue
(`fs.inotify.max_queued_events`), Linux drops the events after it and
queues one `IN_Q_OVERFLOW` record, which watchdog 6.0.0 skips without
a word. At startup the indexer wraps watchdog's inotify buffer parser
(`Inotify._parse_event_buffer`, `_install_inotify_overflow_hook`) so
each overflow record logs a fixed-text WARNING (shared line budget)
and adds one to an overflow count in `_IngestionStateRecorder`. The
main loop then forces a re-schedule of the folder watch (a dropped
directory-create event leaves that directory unwatched, and a
recreated directory can reuse its inode, so the refresh's own check
cannot see it) and, once that succeeds, runs the periodic rescan
(rename sweep and walk), at once, and repeats both at most once per
`OVERFLOW_RESCAN_RETRY_SECS` (60 s) while the recovery is still owed;
an overflow after a completed recovery is handled at once again. The
recovery walk takes the count before it starts; only such a walk that
completes with no overflow since then clears the recovery, and logs
it. The startup walk and the ordinary periodic rescan re-schedule no
watch, so they leave an overflow owed (one during startup is
recovered on the main loop's first pass). Until then the watcher's
stamp acknowledgements are held back (see "Index currency" below).
Other platforms' watchdog backends have no inotify queue, and the
hook is not installed there.

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

When deletion reconciliation is enabled, the Maildir walks (startup
scan, periodic rescan), the zero-vector recovery sweep and the
watchdog's `on_created` / new-delivery `on_moved` branches skip
`T`-flagged files, and for every enqueue path (a reparse, a
re-extraction and `make requeue-dead` included) the drain never
indexes a claimed job whose file is `T`-flagged (the reaper owns that message):
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
`T`-flagged files are indexed like any other, so switching from mirror
to archive also re-indexes mail an earlier mirror-mode run reaped,
whose `T`-flagged files are still in the Maildir. Archive mode writes no
tombstones and reaps nothing, but it inherits any `pending_deletions`
row an earlier mirror-mode run left: the message keeps it through flag
renames, and loses it when a rename the watchdog handler or the rename
sweep records drops the `T` flag, or any other write gives the message a
live occurrence again, so a message restored upstream stops reading as
pending deletion (`docs/mcp-tools.md`, *Pending deletion*; #860). The handler logs one INFO line per restore
within the indexer's shared per-window line budget (the rest are
counted on the queue heartbeat's `suppressed_lines`); the sweep reports
`tombstones_cleared=N` on its own line.

Two stage outcomes short-circuit the retry path entirely:

- `FileNotFoundError` at parse — almost always because mbsync renamed
  the file (added an IMAP flag suffix) between enqueue and read, and
  the watcher has not recorded the rename yet. For a path that is not
  indexed, the renamed file enters the queue under its new name via a
  fresh `IN_MOVED_TO` event, so the worker calls
  `mark_skipped(reason="file_missing")` instead of `mark_failed`: row
  deleted, no retry, no dead-letter, and an INFO
  `skipped: <path> reason=file_missing` log line. A path that is still
  indexed gets no fresh event: `on_moved` only moves the file's records
  and its job (`update_filepath`). So a job for an indexed file
  (`reparse`, `reextract`, `recovery`, or a retry whose Phase 2 had not
  finished) is first deferred once, 60 s and without spending an
  attempt (counted under `parse=` in the heartbeat's deferrals), and
  the rename carries it to the new path (#1145). A file still missing
  after that wait is dropped as above, so a rename the watcher records
  only after the wait still loses the job.
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
suitable for typical mailboxes, and all three are documented in
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
mbsync writes `.mbsync-last-sync.json` (completion time, `SYNC_INTERVAL`,
the sync's attempt ID — the same one `last_success` below names — and
a durable success generation, in the `mbsync-state` volume rather than
SQLite, that mbsync allocates and persists before anything names it)
at the Maildir root. It writes a temporary file named after that sync
(`.mbsync-last-sync.<time>.<interval>.<attempt_id>.<generation>.tmp`)
and renames it into place. The stamp sits outside every `cur`/`new`
folder, so no Maildir walk or watchdog handler treats it as mail. A
stamp written before the 2026-10-13 delivery-identity fix (#1621
thread) carries neither field; the indexer reads it, but never
acknowledges or orders by it — see below.

A stamp on disk does not prove the indexer has queued that sync's
mail: the watcher may still be behind on its delivery events. So the
indexer **acknowledges** a sync only when every message it delivered
is queued:

- when the watcher handles the stamp's rename. Watchdog dispatches
  events in order, so the sync's delivery events were handled first.
  The sync is read from the temporary file's name, not the stamp's
  content, which a later sync may already have replaced. After an
  inotify queue overflow this no longer holds, since some delivery
  events were dropped (#1108): the last stamp handled is held
  instead (stamps arrive in sync order) and acknowledged when the
  recovery walk (after a forced watch re-schedule) that started after
  the latest overflow completes. Every delivery the overflow dropped
  was on disk before that walk began, and later ones reached the
  watcher in order.
- when a Maildir walk (startup or the periodic rescan) finishes: the
  stamp read before the walk is acknowledged.

Acknowledgements only move forward, ordered by the stamp's success
generation (2026-10-13, #1621 thread), never by its completion time: a
clock rollback could otherwise let two different attempts publish the
same `completed_at`, closing an identity gap rather than only
reordering records. A stamp with no generation (the legacy shape) is
never acknowledged, not even as the first one seen — it cannot be
ordered. With every health heartbeat (per message, per embed batch and
per attachment page read, at most every 30 s), the indexer
upserts the latest acknowledged sync and its own timestamp into the
one-row `ingestion_state` table. A missed stamp event
reads as a stale sync until the next rescan; a missed delivery event
stays invisible to `current` until the rescan queues it.

`next_success_generation` seeds its counter forward from the
generation already in the retained stamp when that is higher than the
counter file (`mbsync/entrypoint.sh` `stamp_generation`, #1612 round 8):
the `mbsync-state` volume can be recreated or restored from an older
backup while `maildir-volume` keeps a newer stamp, and without this the
counter would restart below a generation already published, stranding
the acknowledgement behind it (and an unrecovered collection failure
unable to clear, see *Collection status* below) until it caught back
up on its own. The two durable records can only diverge in this
direction — the counter is always allocated and persisted before a
stamp names it — so raising it to match an already-published generation
never regresses or reorders anything already acknowledged. A missing,
oversized or unparseable stamp (including the legacy shape) seeds
nothing, leaving the counter's own value as today.

### Collection status

The success stamp cannot tell a clean sync from one that skipped
folders, and a failed attempt writes none, so mbsync also publishes a
collection status (#1592; owner decision on the issue). It sits beside
the stamp, and reading it acknowledges nothing. The stamp's format
later changed (2026-10-13, #1621 thread) to carry the attempt ID and
success generation the delivery-identity fix needs — see *Index
currency* above and the Freshness bullet below.

- **File.** `.mbsync-collection-status.json`, format 1, one JSON line
  of counts and fixed tokens, never a folder name: `format`,
  `scope_uuid` (`ACCOUNT_SCOPE`), `run_deadline_secs`,
  `sync_interval_secs`; `attempt` (its ID, `started_at` and `outcome`);
  `last_terminal` (attempt ID, `completed_at`, `outcome`,
  `withheld_far_side_folders`, null when the error filter's counts
  never arrived, and `failure_class`); `last_success` (attempt ID,
  `completed_at`). Each publication is written to a unique temporary
  file in the Maildir root, made mode 0644 and renamed into place.
- **When.** Before every attempt (`in_progress`) and after it (its
  terminal outcome). A success is published after its stamp, with the
  stamp's completion time; when the stamp cannot be written, the
  success is still published (and is never delivered, since no stamp
  names it) before mbsync exits for a restart. A caught stop (TERM or
  INT) before the attempt has a result is published as `failed` with
  class `interrupted`; one after it publishes that result. A shell
  killed outright leaves the
  `in_progress` record until the next start publishes. A start carries
  the earlier records forward from the file itself, when it is in the
  written shape and names the configured account; otherwise it warns
  and starts with none.
- **Outcomes.** `succeeded`; `succeeded_with_withheld_folders`, the
  tolerated run whose only errors were far-side folders it could not
  open (see [troubleshooting](troubleshooting.md#a-proton-folder-was-renamed-or-deleted));
  `failed`, with the first cause as its class: `deadline` (stopped at
  `SYNC_DEADLINE_SECONDS`), `killed` (killed after ignoring the stop,
  or by the kernel), `error` (any other mbsync failure, a far-side
  failure together with another error included), `permission_repair`
  (mbsync succeeded, the repair failed) or `interrupted`. An attempt
  `in_progress` means only "started, no terminal result recorded"; an
  old one never proves the collector alive or dead.
- **Order.** Attempt IDs are a per-start random token and a counter;
  the records follow publication order (the file always holds the
  newest), never their timestamps, so a clock rollback cannot reorder
  them.
- **Reading.** The indexer reads the file at startup, before every
  `ingestion_state` write and on each rename onto it, under the lock
  its acknowledgements use. It reads one open descriptor (a rename
  mid-read leaves the old publication whole), only a regular file, at
  most 4096 bytes. A missing, legacy (no `format`), malformed,
  unsupported, unreadable or other-account file reads as `unknown`
  with that reason and one fixed-text WARNING per change of reason,
  never as a success.
- **Storage.** `account_collection_state`, one row per account (v18),
  replaced in the same transaction as `ingestion_state`, so the
  acknowledged sync and the outcome are one snapshot. A rebuilt index
  recovers the row from the file at its first write.
- **Freshness.** After a failed attempt, `current` stays false through
  retries until a later success is **delivered**: the file's last
  success is the sync named by the stamp the indexer has acknowledged,
  matched by attempt ID, which mbsync writes identically into both
  (never by completion time — two different attempts can publish the
  same one across a clock rollback, #1621 thread, 2026-10-13). A stamp
  with no attempt ID (the legacy shape) is never a match. The failed
  attempt is kept in the row
  (`unrecovered_failure_attempt_id`) across restarts until then. A
  success that withheld folders keeps the existing criteria and shows
  its count. An `unknown` status leaves `current` to the existing
  criteria, except that an unrecovered failure still holds it false.
  Delivery is also checked for a success terminal that was never
  preceded by an *observed* failure (#1612 round 12): a watcher delayed
  or behind enough to read a collection publication — a rename event,
  or `maybe_record`'s periodic heartbeat, which reads the file directly
  — before processing that same sync's own stamp rename can see a
  success whose failed predecessor it never observed (the predecessor's
  publication was already overwritten on disk by the time it was read).
  `success_delivered` (stored per row, from the same attempt-ID match)
  is checked on its own for this case; mcp-server reports it as
  `collection.success_undelivered`, independent of
  `failure_unrecovered`, so the gap closes whether or not the
  intervening failure was ever seen. In normal operation this clears
  within the watcher's own event-processing latency, since mbsync
  writes the stamp before the collection status that names it as the
  last success.

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

The occurrence reconciliation scan (*Occurrence reconciliation* under
*Deletion Reconciliation*, #1567) reads all three: a file whose size and
mtime match its record (or, for a missed rename, the record of the path
it was renamed from, since a rename keeps both) is a candidate to carry
the recorded `content_hash` forward as its bytes — a fast admission
shortcut, not proof (mbsync sets mtime from IMAP's INTERNALDATE, so two
different messages can legitimately share one), so the file is also
hashed and checked against that recorded hash before it verifies a
rename or names the message a found copy belongs to (`_recorded_hash`,
#1612 round 14; `messages.content_hash` is indexed for the found-copy
lookup); any other file is hashed only
where the module docstring says. The reaper unmarks every path recorded
with a removed message's hash (#1102); `indexed_files` has no
`content_hash` index, so that lookup reads the wanted hashes by claimant
ID and makes one pass over `indexed_files` per reap pass.

Rows for which `stat` / hash capture failed at parse time carry NULL
identity values. A NULL row never verifies or binds anything, so the
scan treats such a file as unknown (a vanished occurrence beside it is
counted, not retired, only when a same-uniq file is there); the columns
are populated lazily on the next reindex of the file.

### Freshness: `is_indexed` is existence, not proof

`Database.is_indexed` stays a pure path-existence check (`indexed_files`
has a row for the path), unchanged, so any caller that only needs "has
this path been through the pipeline at all" keeps an O(1) lookup.
`Database.is_indexed_and_fresh` adds the cheap half of the identity
check on top: the path is indexed **and** its current size and mtime
still match the recorded row. The Maildir walk
(`main._enqueue_unindexed_messages`) and the watcher's new-mail gate
(`MaildirHandler.on_moved`'s Case 2) call this instead of the bare
check, so a record left stale by an out-of-band content change — bytes
replaced at a path with no observed rename event — falls through to the
walk's existing dead-letter and already-queued gates exactly as an
unindexed file would, rather than being skipped forever (#1612). A
dead-lettered path still stays dead until `make requeue-dead`; staleness
only stops the *permanent* skip, not the existing retry/dead-letter
contract.

A watcher-observed rename (`on_moved`'s Case 1) carries an active
occurrence's claimant forward only after
`occurrence_scan.verify_rename_bytes` confirms the destination's bytes
against it: by the source's `indexed_files` identity when there is one
— size and mtime agreeing (a rename keeps both) is a fast admission
shortcut, not proof (mbsync sets mtime from IMAP's INTERNALDATE, so two
different messages can legitimately share one, #1612 round 12), so the
destination is also hashed and checked against the recorded
`content_hash` before it is trusted, the same single bounded hash
`Database._move_occurrence`'s sibling check (*Mailbox occurrences*
above) uses — or, for a
scan-bound occurrence (`bind_found_copy` never writes `indexed_files`,
so there is no recorded identity or hash to check metadata against),
by hashing the destination and checking it against the claimant ID's
embedded hash prefix (the first `CLAIMANT_HASH_CHARS` hex digits after
the `#`, `parser.claimant_id`). The verified identity is re-checked
inside the same transaction as the move (`Database._move_occurrence`'s
`verified_identity` parameter), the same TOCTOU pattern
`move_verified_occurrence` already uses for the reconciliation scan. A
mismatch is not carried forward — neither the occurrence nor a stale
`indexed_files` row under the destination's name (`_rename_rows` gates
the carry on whether `_move_occurrence` found an actual mismatch, not
merely on whether it found nothing to say) — and `on_moved` falls
through to Case 2 as if the destination were new mail.

Falling through leaves the stale occurrence active at the now-missing
source path for the general case: the reconciliation scan is the
eventual backstop (it reads the newly-indexed destination created once
Case 2 enqueues it, on a later pass). In mirror mode, when the same
rename also added the `T` flag, Case 2's trashed-destination skip means
nothing ever enqueues the destination, so the scan never gets that
second occurrence to notice the collision against either — and the
scan's own "missed renames" check only retires an occurrence whose
uniq resolves to no file at all under its folder, never one whose uniq
still resolves to *some* file, however mismatched (`scan_occurrences`,
module docstring "Missed renames"). `on_moved` closes this one case
itself: when the active occurrence's verification **proved** a mismatch
(it read the destination's bytes and they were not this claimant's)
**and** the destination is about to be skipped as already trashed, it
retires the occurrence directly (`Database.retire_mismatched_rename`,
#1612 round 13) and tombstones the claimant if that was its last live
occurrence (`Database.mark_lost_messages`, #1612 round 14 — the same
rule `Reconciler.handle_moved` applies to an ordinary trashed rename,
so the deletion grace period starts at once instead of waiting for the
next reconciliation sweep), rather than leaving it dangling with no
path to ever notice. Unlike `retire_mismatched_occurrence` (the content
backstop's case, where the mismatched file is still at the same
recorded path) there is nothing left to re-check about the
destination — `verify_rename_bytes` already hashed it — so the only
guard is the usual `state = 'active' AND relative_path = ?` check
against a concurrent move or retire of the same occurrence.
`verify_rename_bytes` returns a proven mismatch only when it actually
read the destination's bytes and they disagreed with the expected
hash; an unreadable destination or one whose metadata alone ruled out
trusting it without reading anything is inconclusive, not a mismatch
(#1612 round 14 — the earlier implementation conflated the two, firing
this retirement on a merely inconclusive check), and is left active for
a later reconciliation pass like any other unverified rename.

### The content backstop

Metadata agreeing is a fast admission shortcut, not proof: mbsync sets
a message's mtime from IMAP's INTERNALDATE, so two different messages
can legitimately share one, and a crafted or coincidental replacement
could share size and mtime too. `occurrence_scan.scan_occurrences`
closes that gap for a **held** path — one whose file did not move this
pass, which the metadata checks above never read — by hashing it and
comparing the digest to the occurrence's claimant, bounded per pass:

- **Cache.** Once a path's exact identity (size, mtime, inode) is
  hash-verified, `hash_cache` remembers it, so a later pass skips it in
  one dict lookup. A different file landing at the same path gets a new
  inode in the overwhelming case, so this is not merely a one-time
  check: a changed identity is always a cache miss.
- **Budget.** Only cache misses cost a read. Each pass hashes at most
  `_BACKSTOP_MAX_FILES` (500) previously-unverified held paths, or
  `_BACKSTOP_MAX_BYTES` (100 MB), whichever is hit first; the rest wait
  for a later pass, which sees the same cache miss. Sized from a plain
  wall-clock measurement (2026-10-13, Apple Silicon, warm page cache):
  SHA-256 over a 20 MB file took ~8.7 ms (~2.2 GB/s); a 200 KB file took
  ~0.1 ms. A cold read over real disk or a mounted volume is slower than
  a warm page-cache read, so the budget assumes a much more conservative
  ~20 MB/s: 100 MB at that rate is ~5 seconds, acceptable for a pass that
  already walks the whole Maildir tree. The file cap guards the
  per-file open/stat overhead a byte cap alone would not (many tiny
  files). At the default hourly sweep interval (`INDEXER_DELETION_SWEEP_
  INTERVAL_SECS`) this backstop-verifies a multi-gigabyte mailbox within
  a few days of first running — an eventual backstop for the narrow
  metadata-collision case, not the primary defense (the watcher's
  rename verification and the walk's freshness check are).
- **On a verified mismatch**, only that occurrence is retired
  (`Database.retire_mismatched_occurrence`) — the file is still there,
  unlike `retire_vanished_occurrence`'s case — and the path is queued
  for a reparse through `Database.queue_enqueue_if_absent`
  (`STALE_ENQUEUE_SQL`, `ON CONFLICT DO NOTHING`). Unlike
  `queue_enqueue`'s `INSERT OR REPLACE` (a watcher event is fresh
  intent and should reset prior state), a background detection can
  recur on a later pass before the worker catches up, so admission must
  not keep resetting a pending retry's attempts and error, or silently
  resurrect a dead letter — both stay exactly as they are until the
  worker or the operator (`make requeue-dead`) acts on them.
- **On an unreadable or unstable file** (a permission error, the file
  gone again between the walk and the check), the path is counted
  `unverified`, never retired: the backstop only acts on a positive
  mismatch it could prove.
- **On a budget already spent**, the path is left unhashed and counted
  `backstop_deferred` (#1612 round 10 review), surfaced in the
  `reconciler sweep` / `startup rename sweep` log lines, so an
  operator can tell this secondary check is incomplete for those paths
  this pass rather than it silently never running for them. Ordinary
  budget exhaustion defers the path to a later pass's fresh budget;
  a single held file whose own size always exceeds the whole byte
  budget hits this same return on every pass, never getting hashed at
  all, which stays an accepted gap in this secondary backstop (the
  watcher's rename verification and the walk's freshness check are the
  primary defense).

`reconciler.sweep_paths` always threads a `hash_cache` through to
`scan_occurrences` now, including from its three archive-mode call
sites in `main.py`; before #1612 those passed none, so the backstop (and
the hashing of unindexed `T`-flagged or dead-lettered files it shares
the cache with) silently never ran outside mirror mode.

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
asking Maven Central, so a warm cache makes no request (#1069). The
resolve checks every jar and POM, restored or downloaded, against the
same committed SHA-256 summary file as the image build, so a cache
entry that differs fails the job before the scan reads it (#1117).
`make trivy`
runs the same scans locally with the same flags (#1017), skipping the
per-checkout `.uv-cache` as the workflow does;
`scripts/tests/trivy_flags_test.sh` fails when the two differ.

### Maven trusted checksums

`indexer/java/checksums/checksums.sha256` lists the SHA-256 of every
artifact Maven resolves for the `.ppt` reader: the jars `pom.xml` pins,
their POMs and parent POMs, and `maven-dependency-plugin` with its own
dependency tree. The `ppt-builder` stage of `indexer/Dockerfile` and
the Maven step of `.github/workflows/security.yml` run Maven Resolver's
trusted-checksums check against it (`checksumAlgorithms=SHA-256`,
`failIfMissing=true`, recording off, the summary file read from the
checkout rather than the local repository), so an artifact whose
checksum differs, or that has no line, fails the run whether it came
from the cache or from Maven Central (#1117). Without it, Maven
verifies a checksum only when it downloads, and an artifact already in
the BuildKit cache mount or the restored CI cache would be used as it
is.

A change to `indexer/java/pom.xml`, a Dependabot bump included, needs
the file rewritten: run `make ppt-checksums` and commit the result
with the change. The target builds the Dockerfile's `ppt-checksums`
stage, which runs the image's own Maven with no cache mount, without
the layer cache (for `ppt-tools` too, so the JDK and Maven are the
ones a clean build installs), and with no summary file, so every
artifact is
downloaded from Maven Central, checked against Central's checksum
file (`--strict-checksums`) and recorded; review the diff as you would
the pom change. The file is written in a stable order, so two runs
for one `pom.xml` give the same file. `scripts/tests/maven_checksums_test.sh`
(`make test-maven-checksums`, also in CI) fails when the two Maven
commands' flags differ from the required set, or when the file lacks
a jar or POM line for a dependency or plugin `pom.xml` pins.

### Image scan

`.github/workflows/docker.yml` also scans the three built images
(indexer, mcp-server, mbsync) with Trivy after `docker compose build`,
on each change to a build input and weekly (#977). Before that build,
a pull-request run restores the indexer's `ppt-builder` stage from the
GitHub Actions cache (BuildKit's `gha` backend, #1070), so Maven
Central is contacted only when `indexer/java/pom.xml`, its checksums
file or a layer before them changed. Every run on `main` (a push, the weekly schedule, a
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
since the gates read the image, not the checkout. Before scanning,
they print a warning naming each image whose
`org.opencontainers.image.revision` label is missing or is not the
commit `make build` would stamp now (`SOURCE_COMMIT`), with both
values (#1103); a `-dirty` checkout always warns, because its files may
have changed since the build. The warning does not fail the target, and
the scans still run. The full reports
have no local equivalent: run `trivy image <name>` by hand for every
severity. `scripts/tests/trivy_flags_test.sh` derives the gates from
`docker.yml` and fails when the Makefile drifts from them.

## Trust Boundaries

A reference for PLAN.md decision 43 (defence in depth): the controls at
each trust boundary, the assumptions they share and the limits accepted
so far. It is not exhaustive. The rule covers every control that
enforces a boundary, listed here or not, and no test checks this table
against the code. The last column names tests and Semgrep rules that
fail when one of the row's controls is removed; a control none of them
covers is docs only.

| Boundary and threat | Controls in place | Shared assumptions | Accepted limitations and pending decisions | Locked by |
| --- | --- | --- | --- | --- |
| **Untrusted mail and attachments.** A crafted message or attachment stalls or exhausts the indexer, runs code in it, or puts mail content into logs. | A message over `INDEXER_PARSE_MAX_BYTES` is dead-lettered before it is parsed, after reading at most the cap plus one byte (`indexer/src/parser.py`, `indexer/src/main.py`). The `doc` and `ppt` tools, the `xls`, `docx`, `pptx`, `xlsx`, `eml`, `image`, `pdf` and `html` extractors, container identification and the HTML conversion of message bodies run in a child started by `indexer/src/extractors/_runner.py` (`run_tool`, `run_child`): `_launcher.py` sets `RLIMIT_AS` and `RLIMIT_CPU` before the tool loads, with a wall-clock timeout, an output cap, a kill of the whole process group and a parent-owned scratch directory. One pass of one message may start at most a budget of extraction processes and seconds on attachments the cache cannot serve; the rest are deferred to later passes, between other mail (`indexer/src/attachment_indexing.py` `ExtractionBudget`, #1236). Per-message rows are keyed by the claimant ID (`parser.py` `claimant_id`). In `docker-compose.yml` the indexer mounts Maildir `:ro` and has `mem_limit: 6g`; every service has a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`. mcp-server has no Maildir mount and opens the index `?mode=ro` with `PRAGMA query_only` (`mcp-server/src/lib/sqlite.py`). Log text goes through `log_tool_call` and its `_LOGGABLE_TOOL_PARAMS` allowlist (`mcp-server/src/lib/security.py`), `scrub_embed_error` (`indexer/src/embedder.py`) and `_stage_error` (`indexer/src/main.py`). | A child's limits are sized from a plain measurement of the tool in the image, so a tool upgrade can outgrow them. Process separation is not filesystem or network confinement (PLAN.md decision 42, #698). | Every extractor but `text` runs in a child; `text` stays in-process by decision 42, and the CI test that is to lock its implementation is not written yet (#1295). The dispatcher's OOXML ZIP-size guard reads the central directory in the indexer process until #1438. The stdlib message parse stays in-process (decision 42). `INDEXER_PARSE_MAX_BYTES=0` turns the message size cap off. Keeping mail out of logs is coding discipline plus marker tests: nothing stops a new log call from quoting mail. mbsync and mcp-server have no memory limit until #1300. | `indexer/tests/test_legacy_office.py` (`TestRunTool`, `TestEveryToolRunsUnderLimits`); `indexer/tests/test_image_child.py`; `indexer/tests/test_html_child.py`; `indexer/tests/test_pdf_child.py`; `indexer/tests/test_extraction_budget.py`; `indexer/tests/test_main.py` `test_oversized_file_dead_letters_not_terminal_success`; `scripts/tests/compose_test.sh` (hardening and the read-only `/maildir` on every overlay combination); Semgrep `compose-service-missing-read-only`, `compose-service-missing-no-new-privileges`, `compose-service-missing-cap-drop-all`, `compose-root-user`; `mcp-server/tests/test_sqlite.py` `test_write_attempt_raises` (the `?mode=ro` open; `PRAGMA query_only` is docs only); synthetic-marker tests in both suites, such as `mcp-server/tests/test_provider_error_privacy.py`. |
| **Secrets.** The Bridge password, provider API keys or the MCP token reach the repository, `docker inspect`, a log, or another local account. | Each secret is a Docker secret read from `.secrets/` (the `secrets` section of `docker-compose.yml`). `scripts/validate-env.sh`, which `make up` runs first, requires mode 600 on every secret file (`require_mode_600`) and rejects the API keys and `MCP_AUTH_TOKEN` in `.env` (`reject_secret_in_env`). `.gitignore` excludes `.env`, `.secrets/` and `*.pem`; the `detect-secrets` pre-commit hook also runs in CI (`lint.yml`). Clients get the token from a file, never an argument (`scripts/mcp-auth-headers.sh`; `mcp-server/src/stdio_adapter.py` refuses a group- or other-readable file). | Every secret rests on the operator's account and mode 600 on the host's disk: processes running as the operator are trusted (see [Endpoint authentication](#endpoint-authentication)). | Secrets are stored unencrypted on the host's disk. A plain `docker compose up` skips `validate-env.sh`. | `scripts/tests/validate_env_test.sh` (`loose_secret_mode_fails`, `loose_mcp_token_mode_fails`, `api_key_in_env_fails`, `mcp_token_in_env_fails`); `mcp-server/tests/test_stdio_adapter.py` `test_group_or_other_access_fails_closed`; `mcp-server/tests/test_http_transport.py` `test_tokens_stay_out_of_the_log`; Semgrep `shell-xtrace-enabled`. The `.gitignore` entries and the `detect-secrets` hook are docs only. <!-- pragma: allowlist secret --> |
| **Network exposure.** Another machine, another local account or a web page reaches the MCP endpoint, or a container other than mbsync logs in to Bridge. | Only `mcp-server` publishes a port, `127.0.0.1:${MCP_PORT:-3000}` (`docker-compose.yml`). mbsync alone joins `bridge-net` and alone mounts the `bridge_pass` secret. No service shares the host's network namespace. `mcp-server/src/main.py`: `_HostOriginGuard` rejects a bad Host (421) or Origin (403), and `_StaticBearerTokenVerifier` checks the bearer token with `hmac.compare_digest` before any session exists. `docker-compose.hardened.yml` makes `app-net` internal. mbsync reaches the Bridge app only through a name for this Mac's `127.0.0.1`: `BRIDGE_HOST` must be exactly one of `host.docker.internal`, `host.orb.internal`, `host.internal` (each with a recorded forwarding test, `mbsync/tests/host_alias_check.sh`), and `BRIDGE_CERT_HOST` a loopback name, checked by `mbsync/proton-settings.sh` in `scripts/validate-env.sh` and in the entrypoint's `require_prerequisites` before any render or connection; mbsync has no `extra_hosts`. | The loopback bind keeps other machines out; the token is what stops other local accounts. The container engine and its resolver send the `BRIDGE_HOST` names to this Mac's loopback: the name is not resolved to check it. | The bearer token is the only caller-authentication control against another local account, and it does not separate code running as the operator. `app-net` is not internal by default, so the indexer and mcp-server can reach remote providers. No network control limits Bridge's port to mbsync: on macOS `host.docker.internal` reaches the host's loopback from any container, so only the Bridge password, which mbsync alone mounts, keeps the others from logging in. Decision 46 (owner, 2026-10-11, #1497) replaced the fixed `BRIDGE_HOST` and `BRIDGE_CERT_HOST` in `docker-compose.yml` with this exact-value validation; decision 43 stands, with the caveat that the two sites share one implementation, so a fault in `proton-settings.sh` reaches both. The forwarding test runs on the Mac, not in CI. A Bridge on another machine waits on #1499. | `scripts/tests/compose_test.sh` (ports and `bridge-net` membership on every overlay combination); Semgrep `compose-port-on-other-service`, `compose-port-not-loopback`, `compose-bridge-net-member`, `compose-host-namespace`; `mbsync/tests/entrypoint_test.sh` `every_setting_row_agrees_at_both_sites` (each accepted and refused value at both sites, refused before any render or connection); `mbsync/tests/tls_check.sh` check 7; `scripts/tests/compose_test.sh` (no `extra_hosts` on mbsync); `mbsync/tests/host_alias_check.sh` (on the Mac only); `mcp-server/tests/test_http_transport.py` (`test_missing_token_is_unauthorized`, `test_wrong_token_is_unauthorized`, `test_empty_token_fails_closed`, `test_compare_is_constant_time`, `test_streamable_http_rejects_other_host`, `test_streamable_http_rejects_other_origin`). |
| **Bridge TLS.** Another listener on the Bridge app's loopback port (another local account while the app is down) receives the Bridge password, or the connection falls back to plaintext. | `mbsync/entrypoint.sh` `extract_bridge_cert` connects over implicit TLS only, with no STARTTLS or plaintext fallback, and checks the certificate against the required `BRIDGE_CERT_FINGERPRINT` (`verify_expected_fingerprint`) and then the pin in `mbsync-state` (`verify_cert_pin`) on every start, before the first sync sends the password. Only a `make rotate-bridge-pin` run (a one-shot `docker compose run --rm mbsync rotate-bridge-pin`, #1609) may replace the pin with a changed certificate that matches the fingerprint; that run exits without rendering a config or syncing, the entrypoint refuses any other argument before anything runs, and Compose sets no `command` or `entrypoint` for mbsync, so every start of the service enforces the pin. `scripts/validate-env.sh` refuses the removed `BRIDGE_CERT_PIN_ROTATE` setting. The certificate lives on tmpfs at `/tmp/mbsync/bridge-cert.pem`, which isync uses as `CertificateFile` with `TLSType IMAPS` (`mbsync/mbsyncrc.template`). `validate-env.sh` refuses a missing or malformed fingerprint. isync checks the certificate's name against `BRIDGE_CERT_HOST`, which both sites limit to loopback names (`127.0.0.1` by default, the only name in the app's own certificate); extraction itself does no name check. The template is pull-only: `Sync Pull`, `Expunge None`. | The fingerprint check, the pin and isync's `CertificateFile` all check the certificate extracted at start. On first boot or during a rotation the pin is written from that certificate, so a fingerprint taken from the wrong certificate passes all three; after that, the pin also refuses a certificate other than the pinned one. | Pull-only sync is one configuration control, the template's settings. isync's own validity and host name checks run on each sync, not at startup. During a `make rotate-bridge-pin` run the pin check does not stop a certificate change; the fingerprint check still does. | `mbsync/tests/entrypoint_test.sh` (`mismatch_is_refused_without_rotation`, `first_boot_with_another_expected_fingerprint_is_refused_unpinned`, `missing_expected_fingerprint_is_refused_at_startup`, `config_keeps_sync_safety`, `rendered_config_matches_the_golden_file`, the whole-entrypoint startup order cases such as `expected_fingerprint_mismatch_stops_before_the_pin`, `rotation_stops_at_an_expected_fingerprint_mismatch`, `rotation_run_replaces_the_pin_once`, `removed_rotation_setting_does_not_rotate` and `unknown_arguments_are_refused_before_anything_runs`); `scripts/tests/compose_test.sh` (`merged_hardening_rejects_a_command_or_entrypoint`); `scripts/tests/validate_env_test.sh` `removed_maintenance_overrides_fail`; `mbsync/tests/tls_check.sh` (check 6: a certificate for another name is refused before `LOGIN` under each accepted `BRIDGE_CERT_HOST`); `mbsync/tests/layout_check.sh`; `scripts/tests/validate_env_test.sh` `missing_bridge_cert_fingerprint_fails`; Semgrep `shell-tls-verification-disabled`. |

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
| Send/Move/Flag | Not implemented: no mail-changing tool, and mbsync is pull-only | Never |

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
| Operator configuration naming real people: source-authority rules (addresses, domains), the operator's own addresses, the Bridge username | `config/authority.toml`, `config/identity.toml`, `.env` in the checkout | Unencrypted |

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
  (`config/authority.toml`, `config/identity.toml`, `.env`), eval queries grounded in the real
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
| Embedding — `EMBED_BASE_URL` points at a remote provider | The index | Message subjects, body text and extracted attachment text at index time; search-query text at retrieval time → provider |
| Reranking — `RERANK_BASE_URL` points at a host-side server | ✅ | Never |
| Reranking — `RERANK_BASE_URL` points at a remote provider | Retrieval | The search query, candidate thread subjects, up to five changed reply subjects each, and retrieved chunks → provider |
| Q&A — `INFERENCE_MODE=openai`, host-side `INFERENCE_BASE_URL` | Retrieval local | Never |
| Q&A — `INFERENCE_MODE=openai`, remote `INFERENCE_BASE_URL` | Retrieval local | Retrieved chunks with their subject, participants, dates and attachment names → OpenAI-compatible provider |
| Q&A — `INFERENCE_MODE=anthropic` | Retrieval local | Retrieved chunks with their subject, participants, dates and attachment names → Anthropic-compatible provider |
| Q&A — `INFERENCE_MODE=none` (default) | ✅ | Never (intelligence tools not registered) |

> **Note on remote providers.** Pointing any of these at a remote
> provider ships data over the network: message subjects, body text
> and extracted attachment text through the embedder at index time, every search-query string
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
| Your own program calling the tools on this machine | Stays on machine |

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
when the token is empty, shorter than 32 characters or outside the
RFC 6750 bearer-token characters, and neither the token nor the
`Authorization` header is logged. Rejections are logged with a fixed reason
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

The toggle applies per-deployment. A per-session toggle is not planned
(`PLAN.md`, "Not doing").

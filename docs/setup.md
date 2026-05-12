# Setup Guide

## Prerequisites

- macOS with Docker Desktop installed and running
- Claude Desktop installed
- Proton Mail paid account (Bridge requires a paid plan)
- Git configured with SSH key for GitHub

## Step-by-Step Setup

### 1. Clone the repository

```bash
git clone git@github.com:marshalltech81/protonmail-local-ai.git
cd protonmail-local-ai
```

### 2. Create your environment file and secret placeholders

```bash
cp .env.example .env
```

Edit `.env` — you can leave `BRIDGE_USER` blank for now.
You will fill it in after the Bridge login step.

Create the secrets directory and placeholder files:

```bash
make init-secrets
```

This creates `.secrets/bridge_pass.txt`, `.secrets/inference_api_key.txt`,
`.secrets/embed_api_key.txt`, and `.secrets/rerank_api_key.txt` as
empty placeholders with `600` permissions. Docker Compose requires
all four files to exist before starting. You will overwrite them
with real values only as needed:

- `bridge_pass.txt` — after the Bridge login step below
- `inference_api_key.txt` — required (non-empty) whenever
  `INFERENCE_MODE` is enabled (`anthropic` or `openai`). For a remote
  provider, use the provider's real key. When pointing
  `INFERENCE_MODE=openai` at an unauthenticated host-side server
  (LM Studio, vLLM, `mlx_lm.server`), write any non-empty placeholder
  string — `unauthenticated` reads cleanly in logs — so the
  no-fallback startup contract holds uniformly across all three
  layers. The compat server ignores the bearer header; the placeholder
  never leaves the host. Leave the file empty only when
  `INFERENCE_MODE=none`.
- `embed_api_key.txt` — required (non-empty); `EMBED_MODE=openai` is
  always enabled (the indexer cannot run without an embedder). For a
  remote provider (DeepInfra, OpenRouter, etc.), use the provider's
  real key. For an unauthenticated host-side server, write any
  non-empty placeholder string (e.g. `unauthenticated`); the compat
  server ignores the bearer header.
- `rerank_api_key.txt` — required (non-empty) when `RERANK_MODE=cohere`.
  Leave empty only when `RERANK_MODE=none`.

### 3. Build all Docker images

```bash
make build
```

The Bridge image compiles from the official Proton source (`make build-nogui`).
This takes approximately 3–5 minutes on first build.
Subsequent builds use Docker layer cache and are much faster.

### 4. First-time Bridge login

This step only ever runs once. Your credentials persist in the `bridge-data`
Docker volume.

```bash
make first-run
```

Inside the interactive Bridge CLI:

```
>>> login
# Enter your Proton email address
# Enter your Proton account password
# Enter your 2FA code if enabled

>>> info
# Note the Username and Password shown — these are your Bridge credentials
# They are different from your Proton account password

>>> exit
```

Copy the displayed `Username` into your `.env` file:

```bash
BRIDGE_USER=your@proton.me          # from info → Username
```

Write the `Password` into the Docker secret file:

```bash
printf '%s' 'bridge-generated-pass' > .secrets/bridge_pass.txt
chmod 600 .secrets/bridge_pass.txt
```

Do not put the Bridge password in `.env` — it is passed to the mbsync container
exclusively via Docker Compose secrets, mounted at `/run/secrets/bridge_pass`.

**Why this step is manual (design note)**

The Bridge password lives inside `vault.enc`, which is encrypted with a key held
in the GPG-backed `pass` store. It would be technically possible to automate
extraction by reimplementing the vault format (msgpack framing, AES-256-GCM,
sha256 key derivation). This is intentionally avoided for two reasons:

1. **Fragility.** The vault format is a Bridge internal. Proton can change the
   framing, cipher parameters, or key derivation in any release without notice.
   External decryption code would break silently or produce garbage.

2. **Unnecessary.** The Bridge CLI (`bridge-v3 info`) already reads the vault
   through the supported code path and prints the Bridge credentials. The
   manual copy from that output is a one-time, human-in-the-loop step that
   is appropriate for a first-run flow requiring interactive login anyway.

If you want a shortcut, run this after the `login` / `info` steps above to
print the password directly:

```bash
docker compose run --rm protonmail-bridge \
  su -s /bin/bash bridge -c 'bridge-v3 info'
```

### 5. Configure your embedder and inference providers

This project does not ship its own model-serving components. You point
the indexer + mcp-server at any OpenAI-compatible embedder and choose
between an Anthropic-compatible Messages API or any OpenAI-compatible
chat-completions endpoint for inference.

**Required-vars contract (all three layers):**

For every enabled layer (`*_MODE` != `none`), `{LAYER}_API_KEY` and
`{LAYER}_MODEL` must be non-empty; `{LAYER}_BASE_URL` is optional.
Leaving `{LAYER}_BASE_URL` empty uses the SDK's documented default
(OpenAI proper for `openai`/`embed` modes, Anthropic API for
`anthropic` mode, Cohere API for `cohere` mode). The required
`{LAYER}_API_KEY` is the explicit-intent signal — an operator with a
real `sk-...` has unambiguously chosen their provider, so a typo or
forgotten env var can't accidentally ship inbox content to a remote
provider. Operators pointing at an unauthenticated host-side server
(LM Studio, vLLM, `mlx_lm.server`, TEI) set `{LAYER}_BASE_URL` to
the host endpoint and supply any non-empty placeholder string (e.g.
`unauthenticated`) for `{LAYER}_API_KEY`.

**Embedder** — required, indexer cannot run without it.

```bash
# Edit .env:
EMBED_BASE_URL=...      # optional; leave empty for OpenAI proper
EMBED_MODEL=...         # required (e.g. text-embedding-3-large, Qwen3-Embedding-8B)
```

The schema reserves a fixed 4096-dim vector — pick a model that
produces 4096-dim vectors or run a schema migration. See "Pointing at
a different embedder provider" below for examples (OpenAI proper,
DeepInfra, OpenRouter, host-side servers like LM Studio / vLLM /
`mlx_lm.server`). Write the embedder key to
`.secrets/embed_api_key.txt` (`chmod 600`). The key is required
(non-empty); for an unauthenticated host-side server, use any
placeholder string (e.g. `unauthenticated`).

**Inference** — choose one mode:

```bash
# Anthropic-compatible via the official anthropic SDK (default).
# Leave INFERENCE_BASE_URL empty to hit api.anthropic.com.
# Note: the Anthropic SDK appends '/v1/messages' itself, so when you
# DO set INFERENCE_BASE_URL (compatible gateway, region override),
# the value must NOT end with '/v1'. If you migrated from the old
# INFERENCE_ANTHROPIC_BASE_URL, drop the trailing '/v1'.
INFERENCE_MODE=anthropic
INFERENCE_BASE_URL=                       # optional; leave empty for Anthropic default
INFERENCE_MODEL=claude-sonnet-4-6
# write the key to .secrets/inference_api_key.txt (required, non-empty)

# OpenAI-compatible via the official openai SDK.
# Leave INFERENCE_BASE_URL empty to hit api.openai.com/v1, or set it
# to any /v1 base URL (host-side server, alternative provider).
INFERENCE_MODE=openai
INFERENCE_BASE_URL=                       # optional; leave empty for OpenAI proper
INFERENCE_MODEL=gpt-4
# write the key to .secrets/inference_api_key.txt (required, non-empty)
# For unauthenticated host servers, use any placeholder string
# (e.g. `unauthenticated`) — the compat server ignores the bearer
# header but the key must be non-empty so the startup contract holds.

# Disabled — intelligence tools are not registered.
INFERENCE_MODE=none
```

When pointing at a host-side server, bind it to `127.0.0.1` and use
`http://host.docker.internal:<port>/v1` from the container's
perspective. The project does not provision these servers — install
them with the tool of your choice (LM Studio, vLLM, TEI,
`mlx_lm.server`, etc.).

**Reranker** — optional; off by default.

```bash
# Default (off):
RERANK_MODE=none

# Cohere via the official cohere SDK.
# Leave RERANK_BASE_URL empty for the SDK default; set it for
# proxies, gateways, or region overrides.
RERANK_MODE=cohere
RERANK_BASE_URL=
RERANK_MODEL=rerank-v4.0-pro
# write the key to .secrets/rerank_api_key.txt
```

When disabled, hybrid search returns RRF-only ranking.

### 6. Start the full stack

```bash
make up
```

`make up` now validates `.env` and the secret files first. It fails fast if:

- `BRIDGE_USER` is still unset or left at the placeholder value
- `.secrets/bridge_pass.txt` is missing, empty, or not `600`
- any enabled layer's secret file is missing, empty, or not `600`:
  `inference_api_key.txt` when `INFERENCE_MODE` is `anthropic` or
  `openai`; `embed_api_key.txt` always (`EMBED_MODE` has no `none`
  mode); `rerank_api_key.txt` when `RERANK_MODE=cohere`. For
  unauthenticated host-side servers, write any non-empty placeholder
  string (e.g. `unauthenticated`). **`{LAYER}_BASE_URL` may be empty
  for any enabled layer — empty means "use the SDK default" (OpenAI
  proper, Anthropic API, Cohere API) and validation does NOT fail
  on an empty URL.**
- any enabled layer's `{LAYER}_MODEL` is empty (model is always
  required — no SDK has a default model)
- inference / embed / rerank secret placeholder files are missing or not `600`
  (validation requires the files to exist with `600` permissions even when the
  matching layer is `none`, so the docker-compose `secrets:` references
  resolve cleanly)
- numeric or enum settings such as `SYNC_INTERVAL`, `MCP_PORT`, `MCP_READ_ONLY`, or `INFERENCE_MODE` are invalid

Verify everything is running:

```bash
make logs
```

You should see:
- `protonmail-bridge` — "Starting Bridge in noninteractive mode"
- `mbsync` — "Bridge IMAP is ready" then "Syncing..."
- `indexer` — "Running initial index scan..."
- `mcp-server` — "MCP server starting on port 3000"

Operator-supplied inference / embed / rerank endpoints are not in
this list — they run wherever you choose (a remote provider, or a
host-side OpenAI-compatible server such as LM Studio, vLLM,
`mlx_lm.server`, or TEI). Verify reachability from your host with
a small `curl` against `$EMBED_BASE_URL`, `$INFERENCE_BASE_URL`, or
`$RERANK_BASE_URL` if a layer's container is failing to start.

The initial index scan may take several minutes depending on mailbox size.

The default MCP deployment is read-only:
- search, retrieval, and intelligence tools use the local SQLite index
- mail-changing action tools are not registered until a safe write path is explicitly enabled
- the latent live Bridge transport now fails closed unless a future write path
  is configured with explicit cert-pinned TLS

On first run, Bridge must download and decrypt your full mailbox from Proton's
servers before mbsync can pull anything. This can take a long time for large
mailboxes. Watch sync progress with:

```bash
docker run --rm \
    -v protonmail-local-ai_bridge-data:/data:ro \
    debian:bookworm-slim \
    bash -c 'find /data/local/protonmail/bridge-v3/logs -name "*.log" | sort | tail -1 | xargs tail -f'
```

Bridge writes sync progress into its structured log file in the `bridge-data`
volume.


### Reranker toggle

`RERANK_MODE` is a clean runtime toggle. The reranker is a post-RRF
stage with no schema dependency, so flipping it on or off takes
effect on the next request without touching indexing or embeddings.
The default is `none`; to use it, set `RERANK_MODE=cohere`, set
`RERANK_MODEL` (e.g. `rerank-v4.0-pro`), and write the API key to
`.secrets/rerank_api_key.txt`. `RERANK_BASE_URL` is optional —
leave empty for the Cohere SDK default.

### Pointing at a different embedder provider

The embedder client (indexer + mcp-server query path) speaks the
OpenAI-compatible `/v1/embeddings` shape, so any compliant provider is
a single env change away. Examples:

```bash
# OpenAI proper — leave EMBED_BASE_URL empty for the SDK default
# (https://api.openai.com/v1). The required EMBED_API_KEY is the
# explicit-intent signal that makes empty-URL unambiguous.
EMBED_BASE_URL=
EMBED_MODEL=text-embedding-3-large
# put the OpenAI key in .secrets/embed_api_key.txt — never .env

# Host-side server (LM Studio, vLLM, mlx_lm.server, TEI, etc.)
EMBED_BASE_URL=http://host.docker.internal:8001/v1
EMBED_MODEL=mlx-community/Qwen3-Embedding-8B-mxfp8
# put any placeholder string in .secrets/embed_api_key.txt

# DeepInfra
EMBED_BASE_URL=https://api.deepinfra.com/v1/openai
EMBED_MODEL=Qwen/Qwen3-Embedding-8B
# put the API key in .secrets/embed_api_key.txt — never .env

# OpenRouter
EMBED_BASE_URL=https://openrouter.ai/api/v1
EMBED_MODEL=qwen/qwen3-embedding-8b
```

After changing provider:

1. Set the new vars in `.env`. `EMBED_BASE_URL` may be left empty when
   targeting OpenAI proper — the SDK's documented default kicks in.
2. Write the API key to `.secrets/embed_api_key.txt` (`chmod 600`).
   `make init-secrets` creates an empty placeholder; the key is
   required (non-empty). For an unauthenticated host-side server, use
   any placeholder string (e.g. `unauthenticated`); compat servers
   ignore the bearer header.
3. **Indexer and mcp-server must point at the same provider + model**
   so query vectors are comparable to indexed vectors. A
   dimension mismatch (e.g. pointing mcp-server at a 3072-dim
   model against a 4096-dim index) surfaces at query time as a
   `Search error: Embedding dimension mismatch` naming
   `EMBED_BASE_URL` and `EMBED_MODEL`. A same-dim model with a
   different vector distribution still degrades hybrid search
   silently — there's no way to detect that without a full
   reindex.
4. The schema reserves a fixed 4096-dim vector. `EMBED_MODEL`
   must keep producing 4096-dim vectors (Qwen3-Embedding-8B variants)
   or a schema migration is required.
5. Switching to a model with a different vector distribution requires
   a full reindex (the existing 4096-dim index isn't comparable to the
   new model's 4096-dim space).

Privacy note: remote embedders ship every email body chunk to the
provider at index time and every search query at retrieval time.
Pointing at a host-side server keeps that traffic on your machine.
Choose accordingly.

The embedder has no enable/disable toggle equivalent to
`RERANK_MODE`. The SQLite schema is sized for 4096-dim vectors and
the indexer needs an embedder — falling back to "no embedder" would
mean a schema rollback plus a full reindex from Maildir.

## Updating Bridge

When you bump `BRIDGE_VERSION` in `.env`, validate the upstream patch points and
the rebuilt image before restarting the service:

```bash
make bridge-upgrade-check
make update
```

### 7. Configure Claude Desktop

Open or create `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "protonmail-local-ai": {
      "url": "http://localhost:3000/sse"
    }
  }
}
```

Restart Claude Desktop.

In a new conversation, you should see the ProtonMail tools available.
Test with: *"What is the status of my email index?"*

## Troubleshooting

### Bridge won't start — "Failed to launch exit status 1"

This can happen if the image is outdated. Check:

```bash
docker compose logs protonmail-bridge
```

If the image is outdated, rebuild:

```bash
make bridge-upgrade-check
make update
```

### Bridge won't start — keychain / GPG errors

```bash
docker compose logs protonmail-bridge
```

If the GPG/pass store is corrupt, wipe the bridge data volume and re-run first-run:

```bash
docker compose down
docker volume rm protonmail-local-ai_bridge-data
make first-run
```

### Bridge starts but shows "No Proton account found" every time

The account detection looks for `vault.enc` in the bridge-data volume.
If it keeps dropping to the interactive CLI, the volume may not be persisting correctly:

```bash
docker volume inspect protonmail-local-ai_bridge-data
```

Ensure `make first-run` uses `docker compose run` (not `docker run`) so the named
volume is mounted.

### Startup warnings: "Failed to add test credentials to keychain" / "no vault key found"

These are harmless. Bridge cannot use the desktop keychain (no dbus session in a
container) and falls back to its own encrypted vault. The "no vault key found" warning
only appears once — on the very first run before the vault is created.

### Reading Bridge logs directly from the volume

Bridge writes structured logs to a timestamped file inside the `bridge-data` volume.
Since Bridge no longer streams logs to Docker stdout, read them directly:

```bash
docker run --rm \
    -v protonmail-local-ai_bridge-data:/data:ro \
    debian:bookworm-slim \
    bash -c 'find /data/local/protonmail/bridge-v3/logs -name "*.log" | sort | tail -1 | xargs tail -n 100'
```

To follow the log in real time, replace `tail -n 100` with `tail -f`.

### Bridge is up but IMAP is unresponsive / mbsync can't connect

Bridge may still be in the middle of its initial Gluon sync — pulling every
message body from Proton's API into its local database before it can serve IMAP.
This is not the same as mbsync syncing to Maildir. It happens inside the Bridge
container and can take hours on a large mailbox.

Run these three diagnostics to understand what state Bridge is in:

**1. Check recent Bridge logs**

```bash
docker run --rm \
    -v protonmail-local-ai_bridge-data:/data:ro \
    debian:bookworm-slim \
    bash -c 'find /data/local/protonmail/bridge-v3/logs -name "*.log" | sort | tail -1 | xargs tail -n 30'
```

If you see rapid-fire lines like:

```
200 OK: GET https://mail-api.proton.me/mail/v4/messages/<id>
200 OK: GET https://mail-api.proton.me/mail/v4/messages/<id>
```

Bridge is still downloading messages. Do not attempt cert extraction yet —
IMAP will be unresponsive during heavy Gluon sync, and `mbsync` now fails
closed instead of syncing without a pinned Bridge cert. If Bridge stays in
this state, the `mbsync` container now exits after a bounded wait and Docker
restarts it so the failure is visible instead of hanging forever.

**2. Check that Bridge is authenticated**

```bash
docker exec protonmail-bridge \
    find /data/config/protonmail/bridge-v3 -type f | sort
```

If `vault.enc` is missing, Bridge is not authenticated and will not serve IMAP
at all. Re-run `make first-run` to log in again.

**3. Check the bridge binary is actually running**

```bash
docker exec protonmail-bridge ps aux
```

A container can be "Up" while the process inside has crashed. If `bridge` does
not appear in `ps aux`, the process exited — check the logs for the error and
restart the container.

**How to know Gluon sync is finished**

Watch for the log pattern to shift from message fetching to event polling:

```
# Still syncing — rapid fire, sub-second interval:
200 OK: GET .../mail/v4/messages/<id>
200 OK: GET .../mail/v4/messages/<id>

# Sync complete — sparse, several seconds apart:
200 OK: GET .../mail/v4/events/<id>
200 OK: POST .../data/v1/metrics
```

Once you see event polling instead of message fetching, IMAP is fully
responsive. Extract the TLS cert (see below) and then check mbsync.

**Confirm IMAP port is actually accepting connections**

Run this from outside the container to verify port 1143 is ready:

```bash
docker run --rm \
    --network protonmail-local-ai_bridge-net \
    debian:bookworm-slim \
    bash -c "apt-get install -y netcat-openbsd -qq 2>/dev/null && \
             echo | nc -w 5 protonmail-bridge 1143"
```

If IMAP is ready you will see the Bridge greeting banner, e.g.:

```
* OK [CAPABILITY IMAP4rev1 ...] ProtonMail Bridge ready.
```

If the command hangs or exits silently, Bridge is still syncing or the
process has crashed — check the logs and process steps above.

### Verifying mbsync is working

Run these checks in order of depth.

**1. Is mbsync running and looping?**

```bash
docker compose logs mbsync --tail 20
```

Look for `>>> Syncing...` lines repeating at your `SYNC_INTERVAL`. If startup
fails, `mbsync` now logs a specific cause such as:

- missing `BRIDGE_USER`
- missing or empty `/run/secrets/bridge_pass`
- cert extraction timeout
- `openssl s_client` handshake errors
- Bridge TLS cert fingerprint does not match the pinned value (see the
  "Bridge cert pin mismatch" section below)

Repeated sync failures now count toward an exit threshold so the container
restarts instead of looping forever in a broken state.

**2. Did any mail land in the Maildir volume?**

```bash
docker run --rm \
    -v protonmail-local-ai_maildir-volume:/maildir:ro \
    debian:bookworm-slim \
    find /maildir -name "*.eml" -o -name "*:2,*" | wc -l
```

A non-zero count means mbsync is writing files. Zero means it connected but
downloaded nothing — either the mailbox is empty or `Patterns` is filtering
everything out.

**3. Check the folder structure was created**

```bash
docker run --rm \
    -v protonmail-local-ai_maildir-volume:/maildir:ro \
    debian:bookworm-slim \
    find /maildir -maxdepth 2 -type d
```

You should see `INBOX`, `Sent`, `Drafts`, etc. If only `/maildir` appears with
nothing under it, the sync ran but Bridge returned no folders.

**4. Force a sync now and watch verbose output**

```bash
docker exec mbsync mbsync -c /tmp/mbsync/mbsyncrc -a -V 2>&1 | head -50
```

`-V` prints each folder being synced and message counts. This is the most
informative test — it will clearly show auth failures, cert errors, or folder
mismatches.

### mbsync fails to connect

Bridge takes 10–15 seconds to fully start. mbsync waits automatically, but it
now gives up after a bounded wait and lets Docker restart it rather than
appearing healthy forever. If it keeps failing:

```bash
docker compose logs mbsync
docker compose logs protonmail-bridge
```

If you want Docker's view of the current state:

```bash
docker inspect mbsync --format='{{json .State.Health}}'
```

### Embedder or inference endpoint unreachable from containers

The indexer or mcp-server reports a connection error against
`EMBED_BASE_URL` / `INFERENCE_BASE_URL` /
`RERANK_BASE_URL`. The project does not run those
servers, so the diagnostic depends on where you pointed it:

- Host-side server: confirm it is listening on the configured port
  (`lsof -iTCP:<port> -sTCP:LISTEN`) and bound to `127.0.0.1`.
  Containers reach `127.0.0.1` on the host as
  `host.docker.internal:<port>` via OrbStack.
- Remote provider: verify outbound networking from a container:

  ```bash
  docker run --rm curlimages/curl:latest -fsS https://example.com
  ```

  If the hardened compose overlay is active (`internal: true` on
  `app-net`), all remote provider calls are blocked by design.

### Inference / embedder cold start

The first call after a fresh install often triggers a model load on
host-side servers (or a per-provider warmup on remote endpoints).
`EMBED_WARMUP_TIMEOUT_SECS` (default 600) bounds how long the indexer
waits before failing the first warmup POST. Watch the relevant
provider's log for download / load progress.

### sqlite-vec fails with "wrong ELF class: ELFCLASS32" (ARM64 / Apple Silicon)

You are running an older pinned version. `sqlite-vec` versions prior to 0.1.9 ship
an armv7 (32-bit) wheel which is incompatible with aarch64 containers. Ensure
both `indexer/pyproject.toml` and `mcp-server/pyproject.toml` pin
`sqlite-vec==0.1.9` or later, regenerate the lockfiles, then rebuild:

```bash
docker compose build indexer mcp-server
```

### Index is empty after startup

The initial sync may still be running. Check:

```bash
docker compose logs indexer
docker compose logs mbsync
```

mbsync must connect to Bridge and complete at least one sync before the indexer
has emails to process.

### Enabling deletion reconciliation

By default the local index is append-only: messages you delete on ProtonMail
are still kept locally. To propagate deletions, set
`INDEXER_DELETION_ENABLED=true` in `.env` and restart the indexer. See the
`Indexer — deletion reconciliation` block in `.env.example` for all knobs
(grace window, sweep interval, mass-delete brake, unlink-on-reap).

Defaults — 7-day grace window, 5% mass-delete brake, no file unlink — are
the safe starting point. Quick checks after enabling:

```bash
docker compose logs indexer | grep reconciler
```

You should see one line per sweep/reap. If the reaper ever logs
`reaper aborted: ... exceed mass-delete threshold`, investigate why mbsync
marked a large batch as deleted (Bridge vault rebuild, folder rename,
account re-auth) before setting `INDEXER_DELETION_FORCE=true`.

Tombstones and reaper actions can be inspected directly:

```bash
docker run --rm -v protonmail-local-ai_sqlite-volume:/data:ro \
    debian:bookworm-slim bash -c \
    'apt-get -qq install -y sqlite3 >/dev/null && \
     sqlite3 /data/mail.db "SELECT COUNT(*) FROM pending_deletions;"'
```

The reaper sweeps `pending_deletions` on startup and once per
`INDEXER_DELETION_SWEEP_INTERVAL_SECS`. If you want a deletion to land
immediately for testing, drop the grace window to `0` and restart.

### Tuning indexing retries

Every discovered Maildir file is written to an `indexing_jobs` table
and drained by a worker loop. Transient failures (embed service
down, SQLite lock contention) get exponential backoff; a persistent
parser or schema error transitions the row to `dead` after
`INDEXER_MAX_ATTEMPTS` attempts and stops being retried.

| Variable | Default | Purpose |
|---|---|---|
| `INDEXER_MAX_ATTEMPTS` | `5` | Max retries before a row becomes `dead`. |
| `INDEXER_RETRY_BASE_SECONDS` | `30` | Base backoff. Each attempt multiplies by `2^(attempts-1)`, capped at 6 h. |

Inspect queued / dead work directly:

```bash
docker run --rm -v protonmail-local-ai_sqlite-volume:/data:ro \
    debian:bookworm-slim bash -c \
    'apt-get -qq install -y sqlite3 >/dev/null && \
     sqlite3 /data/mail.db \
       "SELECT status, COUNT(*) FROM indexing_jobs GROUP BY status;"'
```

A `dead` row carries the last `last_stage` / `last_error` so you can
tell an embed-service outage from a parser bug without digging through logs.
Re-enqueueing (for example by touching the file so mbsync re-delivers)
resets the row to `queued` with `attempts = 0`.

### Claude Desktop doesn't see the tools

1. Verify the MCP server is running: `docker compose ps`
2. Check the server is responding: `curl -N http://localhost:3000/sse`
3. Verify the Claude Desktop config JSON is valid (no trailing commas)
4. Restart Claude Desktop

### Bridge credentials expired / need to re-authenticate

```bash
make down
docker volume rm protonmail-local-ai_bridge-data
make first-run   # log in again
```

After login, copy the new `Username` into `.env` and write the new `Password`
into `.secrets/bridge_pass.txt`:

```bash
printf '%s' 'new-bridge-generated-pass' > .secrets/bridge_pass.txt
chmod 600 .secrets/bridge_pass.txt
make up
```

Your email index is in a separate volume (`sqlite-volume`) and is not affected.

### mbsync refuses to sync — Bridge cert pin mismatch

On first boot `mbsync` extracts Bridge's TLS cert, computes its SHA-256
fingerprint, and saves it to a persistent state volume (`mbsync-state`).
On every subsequent boot the freshly extracted cert is compared to the
pinned fingerprint. A mismatch is treated as a security event and
`mbsync` refuses to sync. Log output looks like:

```
>>> ERROR: Bridge cert fingerprint does not match pinned value — refusing to sync.
>>>   pinned:  sha256:<old>
>>>   current: sha256:<new>
```

Legitimate cert rotations happen when Bridge is upgraded or `vault.enc`
is regenerated. To accept the new cert, start `mbsync` once with
`BRIDGE_CERT_PIN_ROTATE=true`:

```bash
BRIDGE_CERT_PIN_ROTATE=true docker compose up -d mbsync
```

The container writes the new fingerprint to the pin file on startup and
syncing resumes. Set `BRIDGE_CERT_PIN_ROTATE` back to `false` (or remove
it from `.env`) before the next restart so the new pin is enforced going
forward. Leaving it permanently true disables pin enforcement.

`make clean` removes the `mbsync-state` volume along with everything
else, so the next boot after `make clean` is treated as a first boot
and trust-on-first-use re-pins whatever cert Bridge presents.

`make clean` also truncates `.secrets/bridge_pass.txt` because it
authenticates against Bridge state that the volume wipe just deleted
(`vault.enc`). After `make clean` you must re-run `make first-run`
and paste the new Bridge password into `.secrets/bridge_pass.txt`.
Inference / embed / rerank provider keys
(`.secrets/inference_api_key.txt`, `.secrets/embed_api_key.txt`,
`.secrets/rerank_api_key.txt`) are intentionally preserved because
they authenticate against external services that survive container
rebuilds.

### mbsync fails — TLS hostname mismatch after rebuilding Bridge image

The TLS cert Bridge generates is cached inside `vault.enc` in the `bridge-data`
volume. Rebuilding the image does not regenerate the cert — the old one
(issued for `127.0.0.1` only) is reused. To force a fresh cert with the correct
SANs, delete `vault.enc` from the volume without wiping the GPG/pass store:

```bash
make down
docker run --rm -v protonmail-local-ai_bridge-data:/data debian:bookworm-slim \
    rm -f /data/config/protonmail/bridge-v3/vault.enc
make first-run   # re-login; Bridge generates a new cert with protonmail-bridge SAN
make up
```

Deleting `vault.enc` is a full re-authentication path, not a lightweight cert
refresh. Plan on logging into Bridge again.

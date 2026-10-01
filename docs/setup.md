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

Your credentials persist in the `bridge-data` Docker volume, so this step
normally runs once. `make first-run` always opens the interactive CLI, so if
a login is interrupted, run it again and finish the `login` step.

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

2. **Unnecessary.** The Bridge CLI's `info` command already reads the vault
   through the supported code path and prints the Bridge credentials. The
   manual copy from that output is a one-time, human-in-the-loop step that
   is appropriate for a first-run flow requiring interactive login anyway.

To see the credentials again later, reopen the CLI the same way. Stop the
stack first so only one Bridge uses the `bridge-data` volume:

```bash
make down
make first-run   # opens the CLI even though you are logged in
# >>> info       # copy the Username and Password, then
# >>> exit
make up
```

`make first-run` disables Docker logging for the session, so the credentials
`info` prints stay out of the Docker log files. The Bridge entrypoint takes no
command: `docker compose run --rm protonmail-bridge <command>` exits with an
error instead of running it.

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

The SQLite schema reserves a fixed 4096-dim vector, so `EMBED_MODEL`
must produce 4096-dim vectors. The recommended path is a host-side
server running a 4096-dim model (Qwen3-Embedding-8B family), reached
from the containers via `host.docker.internal`:

```bash
# Edit .env — host-side server (mlx_lm.server, LM Studio, vLLM, TEI):
EMBED_BASE_URL=http://host.docker.internal:8001/v1
EMBED_MODEL=mlx-community/Qwen3-Embedding-8B-mxfp8
# write any placeholder string to .secrets/embed_api_key.txt (chmod 600);
# unauthenticated compat servers ignore the bearer header but the
# secret must be non-empty so the startup contract holds.
```

Alternative providers that also serve a 4096-dim Qwen3-Embedding-8B
(DeepInfra, OpenRouter) are listed under "Pointing at a different
embedder provider" below. Leaving `EMBED_BASE_URL` empty is
contract-supported (it falls back to OpenAI proper via the SDK default),
but OpenAI's public embedding catalog has no 4096-dim model today
(`text-embedding-3-large` is 3072-dim, `-3-small` is 1536-dim), so the
empty-URL path is not a usable recipe without a schema migration.

**Inference** — choose one mode:

```bash
# Anthropic-compatible via the official anthropic SDK (default).
# Leave INFERENCE_BASE_URL empty to hit api.anthropic.com.
# Note: the Anthropic SDK appends '/v1/messages' itself, so when you
# DO set INFERENCE_BASE_URL (compatible gateway, region override),
# the value must NOT end with '/v1'.
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
- numeric or enum settings such as `SYNC_INTERVAL`, `MCP_PORT`, `MCP_TRANSPORT`, or `INFERENCE_MODE` are invalid

Verify everything is running:

```bash
make logs
```

You should see:
- `protonmail-bridge` — "Account found. Starting Bridge as user 'bridge'..."
- `mbsync` — "Bridge IMAP port is reachable.", then "Bridge cert
  fingerprint matches the pinned value." (or "First boot — pinned Bridge
  cert fingerprint ..." on the first start), then "Running initial sync..."
- `indexer` — "Running initial index scan..."
- `mcp-server` — "MCP server starting on port 3000"

Operator-supplied inference / embed / rerank endpoints are not in
this list — they run wherever you choose (a remote provider, or a
host-side OpenAI-compatible server such as LM Studio, vLLM,
`mlx_lm.server`, or TEI). Verify reachability from your host with
a small `curl` against `$EMBED_BASE_URL`, `$INFERENCE_BASE_URL`, or
`$RERANK_BASE_URL` if a layer's container is failing to start.

The initial index scan may take several minutes depending on mailbox size.

The MCP server is read-only:
- search, retrieval, and intelligence tools use the local SQLite index
- there are no mail-changing tools, and mcp-server has no connection to Bridge

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

`RERANK_MODE` can be switched without a reindex. The reranker is a
post-RRF stage with no schema dependency, so turning it on or off
leaves indexing and embeddings untouched.
The default is `none`; to use it, set `RERANK_MODE=cohere`, set
`RERANK_MODEL` (e.g. `rerank-v4.0-pro`), and write the API key to
`.secrets/rerank_api_key.txt`. `RERANK_BASE_URL` is optional —
leave empty for the Cohere SDK default.

The MCP server reads the rerank settings once at startup, so a change
takes effect only when the `mcp-server` container is recreated. After
editing `.env`, run `make up`: Compose recreates every container whose
configuration changed. `docker compose restart` is not enough, because
a restarted container keeps the environment it was created with. If
the stack was started with an overlay (such as
`docker-compose.hardened.yml`), run `docker compose up -d` with the
same `-f` files instead, or the recreated container drops the overlay.
A change to `.secrets/rerank_api_key.txt` alone does not change the
container's configuration, so recreate it explicitly with
`docker compose up -d --force-recreate mcp-server` (again with the
same `-f` files).

### Source-authority rules (optional)

You can tag senders with a source-authority class (`counsel`,
`management`, `vendor`, `government`, `personal`, `other`) from a
rules file you write. The classes become filterable metadata
(`authority_class` on `search_emails` and `query_messages`, and the
class plus the rule that set it on `find_contact`); they never change
ranking. Nothing is classified by a model.

```bash
cp config/authority.toml.example config/authority.toml
# edit: one table per class, with `addresses` (exact) and/or
# `domains` (the domain and its subdomains)
chmod 644 config/authority.toml   # the indexer runs as UID 1002
docker compose restart indexer
```

`config/authority.toml` is gitignored: it holds real addresses and
domains, so never commit it. The `config/` directory is mounted
read-only into the indexer at `/config`. The indexer reads the file at
startup and reclassifies every known sender, so restart it after an
edit (the mount is unchanged, so `restart` is enough). Without the
file every sender is `unclassified`. A malformed file (invalid TOML,
an unknown class or key, a pattern that is not a bare address or
domain, a pattern listed twice, more than 10,000 patterns or more than
1 MiB) stops the indexer at startup with an error naming the entry's
position; check `docker compose logs indexer`.

An address rule beats a domain rule, and the closest listed parent
domain wins (`domains = ["example.com"]` covers `mail.example.com`
unless `mail.example.com` has its own rule).

### Pointing at a different embedder provider

The embedder client (indexer + mcp-server query path) speaks the
OpenAI-compatible `/v1/embeddings` shape, so any compliant provider is
a single env change away. Examples:

```bash
# Host-side server (mlx_lm.server, LM Studio, vLLM, TEI, etc.) —
# the privacy-preserving option; mail content never leaves the host.
# (An empty EMBED_BASE_URL is the default and selects OpenAI proper.)
EMBED_BASE_URL=http://host.docker.internal:8001/v1
EMBED_MODEL=mlx-community/Qwen3-Embedding-8B-mxfp8
# put any placeholder string (e.g. `unauthenticated`) in
# .secrets/embed_api_key.txt — compat servers ignore the bearer header

# DeepInfra
EMBED_BASE_URL=https://api.deepinfra.com/v1/openai
EMBED_MODEL=Qwen/Qwen3-Embedding-8B
# put the API key in .secrets/embed_api_key.txt — never .env

# OpenRouter
EMBED_BASE_URL=https://openrouter.ai/api/v1
EMBED_MODEL=qwen/qwen3-embedding-8b
# put the API key in .secrets/embed_api_key.txt — never .env
```

OpenAI proper is not currently a usable embed provider here: their
public embedding catalog has no 4096-dim model
(`text-embedding-3-large` is 3072-dim, `text-embedding-3-small` is
1536-dim), and the schema reserves a fixed 4096-dim vector. The
empty-`EMBED_BASE_URL` / SDK-default path remains documented for
symmetry with `INFERENCE_MODE=openai`, but using OpenAI as the
embedder would require a schema migration to a different vector
width — it is not a copy-paste config swap today.

After changing provider:

1. Set the new vars in `.env`. `EMBED_BASE_URL` is required for every
   provider listed above (no compatible OpenAI-proper embed model
   exists today, so the empty-URL / SDK-default path has no usable
   recipe to copy).
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

When you bump `BRIDGE_VERSION` in `.env`, also set `BRIDGE_COMMIT` to the commit
that release tag points at. Proton's release tags are lightweight (unsigned), so
the build pins the exact commit and refuses a tag that resolves to anything
else — a re-pointed upstream tag cannot change what gets compiled:

```bash
git ls-remote https://github.com/ProtonMail/proton-bridge.git 'refs/tags/v3.28.0*'
# use the ^{} line when present (annotated tag); otherwise the only line
```

Then validate the upstream patch points and the rebuilt image before restarting
the service:

```bash
make bridge-upgrade-check
make update
```

`make update` runs `make bridge-upgrade-check` first and stops if it fails.
If the check fails, do not work around it. A commit mismatch is often a
pin mistake to correct (see below). For patch drift, a smoke failure, or a
moved upstream tag, stay on the working release — and because `.env`
already holds the candidate `BRIDGE_VERSION` and `BRIDGE_COMMIT` by then,
that means putting both back:

1. Restore the previous `BRIDGE_VERSION` and `BRIDGE_COMMIT` in `.env`.
2. If the failure came from the smoke step (it runs after the patch check
   passes), it has already rebuilt the local `protonmail-local-ai/bridge`
   image from the candidate. The running container is unaffected, but the
   next `make up` or container recreation would use the rejected image, so
   rebuild the known-good one: `docker compose build protonmail-bridge`.
   Do not restart Bridge until that build finishes.

By failure:

- **Patch drift** (`make bridge-patch-check`): the upstream source no longer
  matches what `bridge/patch-source.sh` expects — usually Proton moved or
  reworded the code around a patch point. Stay on the previous release
  and wait for a repo update that re-targets the patches;
  do not hand-edit the upstream source or skip the check.
- **Smoke failure** (`make bridge-smoke`): the patched image built but does
  not start as expected (for example, the `autoUpdate="false"` vault marker
  is missing from its log, or Bridge exits non-zero or logs a fatal error
  around it). Treat it the same way — stay on the previous release.
- **Commit mismatch**: `BRIDGE_VERSION` does not resolve to
  `BRIDGE_COMMIT`. What to do depends on whether the pin was already
  verified for this version:
  - **Pin just edited** — usually a local mistake: `BRIDGE_VERSION` was
    bumped without `BRIDGE_COMMIT` (an unset `BRIDGE_COMMIT` falls back to
    the previous release's commit), or the annotated tag object's SHA was
    copied instead of the `^{}` line. Correct it from the `git ls-remote`
    lookup above. Proton's tags are unsigned, so before building, check that
    the commit is the one the release shipped with — for example, that its
    date matches the release date on Proton's GitHub releases page.
  - **Pin previously verified for this version** — the upstream tag has
    moved. Do not copy the new commit from `git ls-remote`; that is the
    unverified source the pin exists to block. Stay on the previous
    release (steps above) until you know why the tag moved.

Things to expect after an upgrade:

- **Gluon cache.** Bridge keeps its IMAP cache (Gluon) in the `bridge-data`
  volume under `/data/local`. If a new version cannot use the existing
  cache, Bridge re-downloads the mailbox from Proton, which can take hours
  on a large mailbox; IMAP is unresponsive until it finishes. See "Bridge is
  up but IMAP is unresponsive" in [`troubleshooting.md`](troubleshooting.md)
  for how to tell a re-sync is in progress.
- **Vault path.** `bridge/entrypoint.sh` detects an existing account by
  looking for `/data/config/protonmail/bridge-v3/vault.enc`. The `bridge-v3`
  segment is tied to Bridge's major version. If a future major version
  stores its vault elsewhere, account detection fails and the container
  drops to the interactive CLI on every restart. Check the vault path
  before adopting a new Bridge major version.
- **Cert pin.** A new Bridge version can present a new TLS cert, which
  mbsync treats as a pin mismatch. See "mbsync refuses to sync — Bridge cert
  pin mismatch" in [`troubleshooting.md`](troubleshooting.md).

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

Other MCP clients connect the same way. The server speaks SSE at `/sse`
by default; set `MCP_TRANSPORT=streamable-http` to serve Streamable HTTP
at `/mcp` instead, or `dual` for both on the same port. Whatever the
transport, the server answers only requests addressed to `localhost`,
`127.0.0.1` or `[::1]` (any port): a request with another `Host` header
gets `421 Misdirected Request`, and a browser `Origin` other than those
names over `http` gets `403`.

A Streamable HTTP session that sees no request for
`MCP_SESSION_IDLE_TIMEOUT_SECS` seconds (default 1800) is ended, so a
session a client abandons without closing it does not hold server
resources until a restart. A client that comes back after that gets
`404` for the old session ID and starts a new session; raise the value
in `.env` if a client of yours does not reconnect on its own.

## Troubleshooting

See [`troubleshooting.md`](troubleshooting.md) for Bridge, mbsync,
indexer, and MCP client diagnostics and recovery steps.

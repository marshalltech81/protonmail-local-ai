# Setup Guide

## Prerequisites

- macOS with Docker Desktop installed and running
- Claude Desktop installed
- Proton Mail paid account (Bridge requires a paid plan)
- Git configured with SSH key for GitHub

Already running the Proton Mail Bridge app on your Mac? The optional
[macOS Bridge mode](#macos-bridge-mode-optional) syncs from it instead
of the Bridge container; it replaces the Bridge build and login in
steps 3–4 and the `make up` in step 6.

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
empty placeholders, and `.secrets/mcp_auth_token.txt` holding a random
token, all with `600` permissions. Docker Compose requires all five
files to exist before starting. You will overwrite the placeholders
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
- `mcp_auth_token.txt` — required (non-empty). The bearer token every
  MCP client must send (see [Connect an MCP client](#7-connect-an-mcp-client)).
  `make init-secrets` generates it; to create or replace it yourself,
  run `(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)`.
  Never put it in `.env`.

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

`make up` now validates `.env` and the secret files first. It checks the
values Compose will use: a variable exported in your shell overrides `.env`,
and a key left empty or out takes its `docker-compose.yml` default, so
`BRIDGE_VERSION`, `SYNC_INTERVAL`, `MCP_PORT` and (in `anthropic` mode)
`INFERENCE_MODEL` may be omitted. A secret file holding only whitespace counts as empty, since the
services strip it. It fails fast if:

- `BRIDGE_USER` is still unset or left at the placeholder value
- `.secrets/bridge_pass.txt` is missing, empty, or not `600`
- any enabled layer's secret file is missing, empty, or not `600`:
  `inference_api_key.txt` when `INFERENCE_MODE` is `anthropic` or
  `openai`; `embed_api_key.txt` always (`EMBED_MODE` has no `none`
  mode); `rerank_api_key.txt` when `RERANK_MODE=cohere`;
  `mcp_auth_token.txt` always (`MCP_AUTH_TOKEN` in `.env` also fails).
  For unauthenticated host-side servers, write any non-empty placeholder
  string (e.g. `unauthenticated`). **`{LAYER}_BASE_URL` may be empty
  for any enabled layer — empty means "use the SDK default" (OpenAI
  proper, Anthropic API, Cohere API) and validation does NOT fail
  on an empty URL.**
- any enabled layer's `{LAYER}_MODEL` is empty (model is always
  required — no SDK has a default model; in `anthropic` mode an empty
  `INFERENCE_MODEL` takes the Compose default `claude-sonnet-4-6`, which
  `openai` mode cannot use)
- inference / embed / rerank secret placeholder files are missing or not `600`
  (validation requires the files to exist with `600` permissions even when the
  matching layer is `none`, so the docker-compose `secrets:` references
  resolve cleanly)
- `config/authority.toml`, when present, is not a regular file (a symlink
  counts as not one) or is not `600`
- numeric or enum settings such as `SYNC_INTERVAL`, `MCP_PORT`, `MCP_TRANSPORT`, or `INFERENCE_MODE` are invalid
  (`MCP_TRANSPORT` accepts only `streamable-http` or unset; the removed `sse`
  and `dual` fail with migration steps)

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

mbsync's first sync can take hours for a large mailbox. mbsync reports
healthy as soon as that sync is running, so the indexer and MCP server
start during it. Until the first sync completes, `get_mailbox_status`
(and `make status`) reports the index as not current, with "no
successful mail sync has been recorded". mbsync makes the synced
folders readable to the indexer only when the sync finishes; when it
records that sync, the indexer starts watching those folders and
queues their mail, with no restart needed (#516). See "Mail from the
first sync is indexed only after it completes" in
`docs/troubleshooting.md`.
If `make up` fails with "dependency failed to start: container mbsync is
unhealthy", see "`make up` fails — mbsync is unhealthy" in
`docs/troubleshooting.md` for the cause and how to start the rest.

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
editing `.env`, run `make up` (`make up-macos-bridge` in
[macOS Bridge mode](#macos-bridge-mode-optional)): Compose recreates every container whose
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
install -m 600 config/authority.toml.example config/authority.toml
# edit: one table per class, with `addresses` (exact) and/or
# `domains` (the domain and its subdomains)
make restart-indexer
```

`config/authority.toml` is gitignored: it holds real addresses and
domains, so never commit it. Keep it a regular file at `600`, like the
files in `.secrets/`, so other accounts on the host cannot read it;
`make up` and `make restart-indexer` fail if it has any other mode, is
a symlink or is not a regular file. Restart with `make restart-indexer`
rather than `docker compose restart indexer`, which skips that check, so
an editor that saves the file with a looser mode is caught. The indexer
runs as UID 1002, but on macOS the Docker Desktop and OrbStack file
sharing serves a bind-mounted file to the container's user, so it still
reads a `600` file you own (the same way the services read the `600`
secret files). On Linux with Docker Engine the bind mount keeps your
ownership, so the indexer cannot read a `600` file you own; a safe
access path there is tracked in #526. Do not widen the mode or hand the
file to a host group to work around it: GID 1002 may belong to another
account on the host.
The `config/` directory is mounted
read-only into the indexer at `/config`, and the path
`/config/authority.toml` is fixed (there is no environment setting for
it). The indexer reads the file at
startup and reclassifies every known sender, so restart it after an
edit (the mount is unchanged, so `restart` is enough). Without the
file every sender is `unclassified`. A malformed file (invalid TOML,
an unknown class or key, a pattern that is not a bare address or
domain, a pattern listed twice, more than 10,000 patterns or more than
1 MiB) stops the indexer at startup with an error naming the entry's
position; so does a path that exists but cannot be read, such as a
dangling symlink. Check `docker compose logs indexer`.

Domains are written bare: letters, digits and hyphens in dot-separated
labels (at most 16 labels, 253 characters). There are no wildcards:
`domains = ["example.com"]` already covers every subdomain. An address
rule beats a domain rule, and the closest listed parent domain wins
(`example.com` covers `mail.example.com` unless `mail.example.com` has
its own rule).

Authority reflects the claimed From address: the index does not
authenticate senders, so spoofed mail from a classified address or
domain is classified too (see #463). Treat the class as who a message
says it is from, not proof. Mail in Proton's `Spam` folder, where most
spoofed and DMARC-failing mail lands, never counts toward an
`authority_class` filter; spoofed mail that reaches the inbox still
does. Checking DKIM/DMARC verdicts is deferred until Bridge's headers
have been checked on real mail.

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

### 7. Connect an MCP client

The server speaks one MCP transport, Streamable HTTP, at
`http://127.0.0.1:3000/mcp` (replace `3000` with `MCP_PORT` if you
changed it). The port is published on the IPv4 loopback interface
only, so the client has to run on this machine. Configure clients with
`127.0.0.1`, not `localhost`: `localhost` can resolve to the IPv6
loopback `::1` first, where another local account could listen on the
same port and collect the bearer token. Two separate questions decide
whether a client can connect: whether it speaks Streamable HTTP, and
whether its connection starts on this machine (and so can reach
`localhost`).

**Every request to `/mcp` must carry the bearer token** from
`.secrets/mcp_auth_token.txt` as `Authorization: Bearer <token>`. A
request without it, or with another token, gets `401` before any MCP
session is created; `/health` needs no token. The token keeps other
local accounts, and web pages in your browser, from using the endpoint.
It does not stop code running as your own user, which can read the
file, so the trust condition is "processes running as the operator are
trusted". Keep the token out of shell history, chat transcripts and
committed files, and out of any command's arguments: on a shared
machine other accounts can read every process's arguments from the
process list, so a command such as `--header "Authorization: Bearer
$(cat ...)"` hands them the token. The setups below read it from a file
instead.

**Claude Code** connects from this machine and speaks Streamable HTTP.
It asks `scripts/mcp-auth-headers.sh`, as its `headersHelper`, for the
header each time it connects; the script reads
`.secrets/mcp_auth_token.txt` and prints the header without passing the
token to another program. Run this from the repository root (`$PWD`
records the script's absolute path):

```bash
claude mcp add-json protonmail-local-ai \
  "{\"type\":\"http\",\"url\":\"http://127.0.0.1:3000/mcp\",\"headersHelper\":\"$PWD/scripts/mcp-auth-headers.sh\"}"
```

Claude Code runs a `headersHelper` only in a workspace whose trust
dialog you have accepted, so start Claude Code in this repository once
interactively if `claude mcp list` reports that the helper was not run.
Keep the default `local` scope (or `user`); `--scope project` writes the
entry to `.mcp.json` in the current directory. The configuration holds
only the script path, never the token. If you use an HTTP proxy, read
the proxy note under Codex below: it applies to Claude Code too.

**Claude Desktop** has two ways to add an MCP server, and neither
takes this URL directly:

- `~/Library/Application Support/Claude/claude_desktop_config.json`
  configures local servers that Claude Desktop starts as a command and
  talks to over stdio. Anthropic documents no URL-only entry for it.
- Settings → Connectors adds a remote (custom) connector. That
  connection is brokered by your Claude account and starts from
  Anthropic's servers, not from your Mac, so it cannot reach
  `localhost`. Do not publish or tunnel this port to make it reachable;
  the server is designed to be local-only.

Connect Claude Desktop through the stdio adapter in this repository,
`mcp-server/src/stdio_adapter.py`. It runs on your Mac as a command
Claude Desktop starts, speaks stdio to Claude Desktop and relays every
request to `http://127.0.0.1:${MCP_PORT:-3000}/mcp` with the bearer
token. It is built on the `fastmcp` version `mcp-server/uv.lock`
already pins, so it adds no dependency. It reads the token from
`.secrets/mcp_auth_token.txt` itself, so the token is neither in
`claude_desktop_config.json` nor in any command line, and it exits with
a fixed message, before connecting, if that file is missing, empty or
not mode 600. It connects directly, ignoring `HTTP_PROXY`, `ALL_PROXY`
and the system proxy, so the token goes only to the loopback port. It
writes nothing else: no log lines, no token, no tool arguments or
results.

It needs [`uv`](https://docs.astral.sh/uv/) on the Mac. Create the
adapter's environment once from the repository root, so Claude
Desktop's first start does not wait for an install:

```bash
(cd mcp-server && uv sync --frozen)
```

Claude Desktop does not use your shell's `PATH`, so the entry needs
`uv`'s absolute path; `command -v uv` prints it. Add the
`protonmail-local-ai` entry below to the `mcpServers` object in
`claude_desktop_config.json`, keeping any servers already there; use the
whole example (also in
[`claude_desktop_config.example.json`](claude_desktop_config.example.json))
only when the file does not exist yet. Replace `/ABSOLUTE/PATH/TO/uv`
with that path and `/ABSOLUTE/PATH/TO/protonmail-local-ai` with the
repository's absolute path:

```json
{
  "mcpServers": {
    "protonmail-local-ai": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": [
        "run",
        "--directory",
        "/ABSOLUTE/PATH/TO/protonmail-local-ai/mcp-server",
        "--frozen",
        "python",
        "-m",
        "src.stdio_adapter"
      ]
    }
  }
}
```

`--frozen` runs the locked versions without re-resolving them. If you
changed `MCP_PORT`, add `"env": {"MCP_PORT": "<port>"}` to the entry.
To keep the token file elsewhere, append `"--token-file"` and its
absolute path to `args`; the file must still be mode 600. Pass a path,
never the token.

Third-party stdio bridges (such as the npm package `mcp-remote`) are
not recommended: they are code from outside this repository that runs
as your user and relays every tool call and result, and their packages
can change hands.

Restart Claude Desktop. In a new conversation, you should see the
ProtonMail tools available. Test with: *"What is the status of my email
index?"* Remember that Claude Desktop sends tool results to Anthropic
as conversation context (see
[architecture.md](architecture.md#mcp-client-layer-governed-by-which-client-you-connect) for what each
client sees). If Claude Desktop shows the server as running but lists
no ProtonMail tools, the server rejected the token: the adapter then
offers no tools and refuses every call. See "MCP client gets 401
Unauthorized" in [`troubleshooting.md`](troubleshooting.md).

**Codex** (the CLI and IDE extension) connects from this machine and
speaks Streamable HTTP, so it needs no adapter. Its
`mcp_servers.<name>.http_headers_helper` setting runs a local command
that prints a JSON object of headers, which is what
`scripts/mcp-auth-headers.sh` prints for Claude Code. Add this to
`~/.codex/config.toml`, with the repository's absolute path. Codex runs
the value with `sh -c`, so keep the single quotes around the path; they
keep a path with spaces in one piece (a path containing a single quote
needs escaping):

```toml
[mcp_servers.protonmail-local-ai]
url = "http://127.0.0.1:3000/mcp"
http_headers_helper = "'/ABSOLUTE/PATH/TO/protonmail-local-ai/scripts/mcp-auth-headers.sh'"
```

Codex, like Claude Code, applies `HTTP_PROXY`, `ALL_PROXY` and the
system (or PAC) proxy settings to its own HTTP connections. If you use a
proxy, make sure it is bypassed for `127.0.0.1` (for example
`NO_PROXY=127.0.0.1` and the system proxy's bypass list); otherwise the
proxy receives the bearer token and every tool call and result. If you
cannot guarantee that, connect Codex through the stdio adapter instead,
which ignores proxy settings; Codex starts it like Claude Desktop does:

```toml
[mcp_servers.protonmail-local-ai]
command = "/ABSOLUTE/PATH/TO/uv"
args = ["run", "--directory", "/ABSOLUTE/PATH/TO/protonmail-local-ai/mcp-server", "--frozen", "python", "-m", "src.stdio_adapter"]
```

(Codex gives stdio servers a reduced environment, so with a changed
`MCP_PORT` add `env = { MCP_PORT = "<port>" }` to the entry.)

Codex runs the helper when it connects and again once after a `401`,
so a rotated token is picked up without editing the file. Codex also
has `bearer_token_env_var`, which reads the token from an environment
variable (`codex mcp add protonmail-local-ai --url
http://127.0.0.1:3000/mcp --bearer-token-env-var PROTONMAIL_MCP_TOKEN`
writes it). The variable then has to be exported in the shell that
starts Codex, for example from your shell profile:

```bash
export PROTONMAIL_MCP_TOKEN="$(< /ABSOLUTE/PATH/TO/protonmail-local-ai/.secrets/mcp_auth_token.txt)"
```

That keeps the token out of command arguments, but every program
started from that shell inherits it, and anything that records its
environment (a crash report, a debug dump, a tool that prints `env`)
can capture it. Prefer `http_headers_helper`. If both are set, Codex
uses the explicit bearer token.

**Cloud-originated connectors** (claude.ai custom connectors, ChatGPT
developer mode) connect from the provider's servers. They support
Streamable HTTP but cannot reach a localhost-only server, and this
project does not support exposing it. ChatGPT's connectors need a
public HTTPS endpoint, so ChatGPT is not supported yet; it is planned
after go-live.

**Other MCP clients** that run on this machine and speak Streamable HTTP
connect to `http://127.0.0.1:3000/mcp` directly, sending the
`Authorization: Bearer <token>` header. A client that cannot send a
custom header cannot connect.

**Rotating the token.** Write a new token to
`.secrets/mcp_auth_token.txt` (the `openssl` command above), restart the
server with `docker compose restart mcp-server` (the token is read once
at startup, and `make up` does not recreate a container whose
configuration is unchanged), then update
each client. Claude Code's helper reads the file on each connection, so
reconnect it (`/mcp` in Claude Code). Codex reruns its helper after a
`401`; with `bearer_token_env_var`, re-export the variable and restart
Codex. The Claude Desktop adapter reads the file when it starts, so
restart Claude Desktop.

**Upgrading from a release without MCP authentication.** Run
`make init-secrets` (it creates only the missing token file), then
`make up` (`make up-macos-bridge` in macOS Bridge mode), then add the header to each client as above. Until a client
sends the token, its requests get `401`.

**Upgrading from a release that served `/sse` (breaking change).** The
legacy HTTP+SSE transport, its `/sse` and `/messages/` endpoints, and
the `MCP_TRANSPORT` values `sse` and `dual` were removed. Remove
`MCP_TRANSPORT` from `.env`, and run `unset MCP_TRANSPORT` in any shell
that exports it, since an exported value wins over `.env` (or set it to
`streamable-http`): `sse` or
`dual` now fails `make validate-env` and mcp-server startup with these
steps. Change each client from `http://localhost:3000/sse` to
`http://127.0.0.1:3000/mcp`, and for a client that picks its transport,
choose Streamable HTTP (`http`), not SSE.

The server answers only requests addressed to `localhost`,
`127.0.0.1` or `[::1]` (any port): a request with another `Host` header
gets `421 Misdirected Request`, and a browser `Origin` other than those
names over `http` gets `403`.

A Streamable HTTP session that sees no request for
`MCP_SESSION_IDLE_TIMEOUT_SECS` seconds (default 1800) is ended, so a
session a client abandons without closing it does not hold server
resources until a restart. A client that comes back after that gets
`404` for the old session ID and starts a new session; raise the value
in `.env` if a client of yours does not reconnect on its own.

## macOS Bridge mode (optional)

By default the stack runs its own Bridge, built from Proton's source in
the `protonmail-bridge` container. If you already run the official
Proton Mail Bridge app on your Mac, mbsync can sync from that instead.
mbsync, the indexer and the MCP server still run in containers; the
Bridge container is neither built nor started.

```text
Proton Mail → Bridge app on macOS (IMAP on 127.0.0.1)
                  ↑ IMAP + STARTTLS via host.docker.internal
             mbsync container → Maildir → indexer → MCP server
```

The mode is a Compose overlay, `docker-compose.macos-bridge.yml`, with
Makefile targets that always pass it:

| Default (Bridge container) | macOS Bridge mode |
|---|---|
| `make build` | `make build-macos-bridge` |
| `make first-run` | log in in the Bridge app |
| `make up` | `make up-macos-bridge` |
| `make update` | the app updates itself |

`make down`, `make logs`, `make status`, `make restart-indexer` and
`make requeue-dead` work in either mode. Any other `docker compose`
command must pass both files
(`docker compose -f docker-compose.yml -f docker-compose.macos-bridge.yml …`):
without the overlay, Compose recreates mbsync pointed at the Bridge
container.

### Requirements and limits

- macOS with OrbStack. Containers reach the Mac through
  `host.docker.internal`, which OrbStack (and Docker Desktop) provide;
  the mode was checked on OrbStack. Bridge stays bound to the Mac's
  loopback interface: no port is published, no LAN address is used and
  no container uses host networking.
- The Bridge app must stay running and logged in. While it is closed,
  logged out, or the Mac sleeps, mbsync waits with its usual bounded
  retries, then exits and Docker restarts it; it syncs again once
  Bridge is back. Freshness is reported by `make status` as usual.
- The app's IMAP connection mode must be STARTTLS, its default (the
  connection-mode setting in the app's advanced settings).
- The app's certificate must be issued for `127.0.0.1`, which is what
  Bridge generates. A certificate you imported into Bridge yourself
  must be too.

### How TLS is checked

mbsync keeps STARTTLS, certificate verification and the persistent
fingerprint pin. The app's certificate names only `127.0.0.1`, and the
isync shipped in the image (1.4.4) checks a self-signed CA certificate
like Bridge's against the configured host name, so connecting to
`host.docker.internal` directly fails with `certificate owner does not
match hostname`. The overlay therefore sets `BRIDGE_CERT_HOST=127.0.0.1`:
the generated `mbsyncrc` keeps `Host 127.0.0.1` for the certificate
check and reaches `host.docker.internal:<port>` through an isync
`Tunnel` (`socat`), which only relays bytes. TLS still runs end to end
between mbsync and the app. See
[architecture.md](architecture.md#bridge-modes) for the details.

Unlike the Bridge container, which sits alone on its own Docker network,
the app listens on an unprivileged port on the Mac's loopback, which
another local account can hold while the app is not running. mbsync
therefore does not trust the first certificate it sees: you give it the
app's fingerprint in `BRIDGE_CERT_FINGERPRINT`, and it refuses any other
certificate, before the pin and before the password is sent, on every
start.

### Set it up

1. In the Bridge app, open the account's mailbox details and note the
   IMAP username, password and port. These are generated by Bridge and
   differ from your Proton account password.
2. `make init-secrets`, then put the username in `.env` and the
   password in the Docker secret, exactly as in step 4 above:

   ```bash
   BRIDGE_USER=your@proton.me          # the app's IMAP username
   ```

   ```bash
   printf '%s' 'bridge-generated-pass' > .secrets/bridge_pass.txt
   chmod 600 .secrets/bridge_pass.txt
   ```

   If the app's IMAP port is not 1143, set `BRIDGE_IMAP_PORT` in `.env`.
   It is read only in this mode.
3. Take the app's certificate fingerprint on the Mac and put it in
   `.env`. With the app running and logged in (so it, and nothing else,
   holds its port), run this in a Mac terminal, replacing 1143 with the
   app's IMAP port if it differs:

   ```bash
   openssl s_client -connect 127.0.0.1:1143 -starttls imap </dev/null 2>/dev/null \
       | openssl x509 -noout -fingerprint -sha256
   ```

   If your Bridge version can export its TLS certificate (in its
   settings), `openssl x509 -in cert.pem -noout -fingerprint -sha256` on
   the exported `cert.pem` gives the same value without a network
   connection. Then:

   ```bash
   BRIDGE_CERT_FINGERPRINT=AB:CD:...   # the value after "Fingerprint="
   ```

   Colon-separated or bare hex, any case, and the whole
   `sha256 Fingerprint=…` line are all accepted. The value is not
   secret.
4. Configure the embedder and inference providers (step 5 of the main setup).
5. Build and start:

   ```bash
   make build-macos-bridge
   make up-macos-bridge
   ```

6. Check the certificate was pinned and the first sync ran:

   ```bash
   docker logs mbsync
   ```

   Without `BRIDGE_CERT_FINGERPRINT`, or with a value that does not
   match, mbsync refuses before logging in and pins nothing; the log
   shows the fingerprint it was presented.

### Switching an existing installation

An installation that already synced from the Bridge container (or the
reverse) has three pieces of state tied to the old Bridge. None of the
steps below deletes mail; the last one is only for state that turns out
to be incompatible, and it starts with a backup.

1. **Stop the stack**: `make down`.
2. **Credentials.** The app's IMAP username and password differ from the
   container's. Replace `BRIDGE_USER` and `.secrets/bridge_pass.txt`
   with the app's (the container Bridge's stay in its `bridge-data`
   volume, which this mode leaves untouched).
3. **Certificate pin.** Set `BRIDGE_CERT_FINGERPRINT` to the app's
   fingerprint (step 3 of "Set it up"). The app presents a different
   certificate from the Bridge container, so the first start still
   refuses to sync with `Bridge cert fingerprint does not match pinned
   value`. Accept the app's certificate once and turn enforcement back
   on (a rotation in this mode accepts only the certificate matching
   `BRIDGE_CERT_FINGERPRINT`):

   ```bash
   make up-macos-bridge                              # refused: pin mismatch
   BRIDGE_CERT_PIN_ROTATE=true make up-macos-bridge  # re-pins, then syncs
   docker logs mbsync                                # check the "rotating pin" warning
   make up-macos-bridge                              # recreates mbsync with rotation off
   ```

   The last command recreates mbsync because its environment changed;
   `docker logs mbsync` no longer shows the `BRIDGE_CERT_PIN_ROTATE=true`
   warning. See
   [troubleshooting](troubleshooting.md#mbsync-refuses-to-sync--bridge-cert-pin-mismatch)
   for the rotation flag's semantics.
4. **mbsync's sync state (UIDVALIDITY).** mbsync records each Proton
   folder's IMAP UIDVALIDITY and message UIDs in a `.mbsyncstate` file in
   that folder's Maildir directory. Another Bridge instance usually
   numbers the same folders differently. Watch the first sync after the switch:
   - No `UIDVALIDITY` errors: the state is compatible and syncing
     continues into the existing Maildir.
   - `UIDVALIDITY genuinely changed` or `Unable to recover from
     UIDVALIDITY change`: mbsync refuses to sync the affected folders
     and changes nothing in them, and the run counts as a failed sync
     (repeated failures restart mbsync). The log shows `<folder>` in
     place of each folder's name
     ([how to see which](troubleshooting.md#folder-names-in-mbsyncs-log)).
     The existing state cannot be
     reused. Either switch back (`make down`, then `make up` with the
     old credentials and a pin rotation back), or back up the Maildir
     and start it over, with the index, from the app, as in
     [mbsync reports a UIDVALIDITY change](troubleshooting.md#mbsync-reports-a-uidvalidity-change).
   - `Recovered from change of UIDVALIDITY`: the change was spurious;
     isync checked the messages and kept the state. Nothing to do.

The same steps apply when switching back to the Bridge container, with
`make up` and `make first-run`'s credentials.

## Troubleshooting

See [`troubleshooting.md`](troubleshooting.md) for Bridge, mbsync,
indexer, and MCP client diagnostics and recovery steps.

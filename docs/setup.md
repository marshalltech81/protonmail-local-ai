# Setup Guide

## Prerequisites

- macOS with OrbStack or Docker Desktop installed and running
- The official [Proton Mail Bridge](https://proton.me/mail/bridge) app,
  installed on the same Mac and signed in (Bridge requires a paid
  Proton plan)
- An MCP client on the same Mac, such as Claude Desktop, Claude Code or
  Codex (see [step 7](#7-connect-an-mcp-client))
- Git configured with SSH key for GitHub
- Full-disk encryption on the Mac (FileVault, in System Settings >
  Privacy & Security). The stack stores your decrypted mail, its search
  index and sync state as unencrypted files; FileVault is what protects
  them on a lost or stolen Mac that is powered off (or restarted and not
  yet unlocked at login), not during a logged-in or screen-locked
  session. OrbStack and Docker Desktop keep the
  Docker volumes inside a virtual-machine disk image, by default on the
  Mac's startup disk, which FileVault covers. Check where yours actually
  is: if the Docker disk image was moved (Docker Desktop allows it) or
  the checkout, which holds `.secrets/`, lives on another drive, that
  drive must be encrypted too. See `docs/architecture.md`, "At rest (on
  the host's disk)".

### Platform support

The stack syncs from the Proton Mail Bridge app running on the host.
mbsync, the indexer and the MCP server run in containers; mbsync reaches
the app through `host.docker.internal` (`BRIDGE_HOST`; on OrbStack,
`host.orb.internal` and `host.internal` also work). The engine and its
resolver are trusted to send that name to this Mac's loopback: the name
is not resolved to check it.

- **macOS** (OrbStack or Docker Desktop): tested. There
  `host.docker.internal` forwards to the Mac's loopback interface, which
  is where the app listens.
- **Windows** (Docker Desktop): expected to work the same way, since
  Docker Desktop forwards `host.docker.internal` to the host's loopback
  there too, but untested.
- **Linux**: not supported out of the box. On Docker Engine,
  `host.docker.internal` (mapped to `host-gateway`) reaches the docker
  bridge address, not the host loopback the app binds, so mbsync cannot
  reach the app.

The app stays bound to the host's loopback interface: no port is
published, no LAN address is used and no container uses host networking.

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

Edit `.env` — you can leave `BRIDGE_USER` and `BRIDGE_CERT_FINGERPRINT`
blank for now. You will fill them in from the Bridge app in step 4.

Create the secrets directory and placeholder files:

```bash
make init-secrets
```

It also writes `ACCOUNT_SCOPE` to `.env` when that line is missing or
empty: a random UUID naming the account the index is registered to
(#1596). It is not a secret, but keep it with your `.env`: the indexer
and mcp-server refuse to start on an index built under another value,
and it is never replaced once set (docs/troubleshooting.md, "Account
scope mismatch").

This creates `.secrets/bridge_pass.txt`, `.secrets/inference_api_key.txt`,
`.secrets/embed_api_key.txt`, and `.secrets/rerank_api_key.txt` as
empty placeholders, and `.secrets/mcp_auth_token.txt` holding a random
token, all with `600` permissions. Docker Compose requires all five
files to exist before starting. You will overwrite the placeholders
with real values only as needed:

- `bridge_pass.txt` — the Bridge app's IMAP password, in step 4 below
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
  A token you choose yourself must be at least 32 characters from
  `A-Z a-z 0-9 - . _ ~ + /` with optional trailing `=` (the RFC 6750
  bearer-token set, which `openssl rand -hex 32` and
  `openssl rand -base64 32` output both meet); `make validate-env` and
  mcp-server startup reject anything else. Never put it in `.env`.

### 3. Build all Docker images

```bash
make build
```

`make build` passes the checkout's commit as the `GIT_COMMIT` build
argument: the short hash, with `-dirty` when a tracked file is modified
or an untracked file is present that Git does not ignore. It is always
taken from the checkout, even when `GIT_COMMIT` is set in your shell or
CI; to label a build explicitly, run
`make build GIT_COMMIT_OVERRIDE=<value>`.
Each image records it as the `org.opencontainers.image.revision` label
and logs it at startup (see `docs/troubleshooting.md`, "Which build and
settings is a container running?"). A plain `docker compose build`, or
an image `make up` builds because none exists yet, records `unknown`.

Besides the Debian and Python packages, the indexer build fetches the
Java libraries of its legacy `.ppt` reader (Apache POI, pinned in
`indexer/java/pom.xml`) from Maven Central (`repo.maven.apache.org`),
so the build host needs to reach it. Each one must match its SHA-256
in the committed `indexer/java/checksums/checksums.sha256`, or the
build fails (see `docs/troubleshooting.md`, "The indexer build fails
with a trusted checksum mismatch"). The downloads stay in a BuildKit
cache mount, so a rebuild that runs the step again (after a
`pom.xml` change or a base-image refresh) fetches only what the mount
lacks; `make build-nocache` starts from an empty mount and refills it,
and `docker builder prune` empties it. Nothing is downloaded at
runtime.

### 4. Set up the Proton Mail Bridge app

The Bridge app must stay running and signed in. While it is closed,
signed out, or the Mac sleeps, mbsync waits with its usual bounded
retries, then exits and Docker restarts it; it syncs again once Bridge
is back. Freshness is reported by `make status` as usual.

1. Sign in to your Proton account in the Bridge app. Then, in the app's
   settings, set the IMAP connection mode to **SSL** (implicit TLS)
   under the IMAP/SMTP connection mode; the SMTP mode does not matter,
   mbsync does not use SMTP. STARTTLS, the app's default, does not
   work: mbsync speaks only implicit TLS and has no STARTTLS fallback,
   so with STARTTLS it stops at startup with
   `Bridge is not serving implicit TLS`.
2. Open the account's mailbox details and note the IMAP username,
   password and port; the port normally stays 1143. These are generated
   by Bridge and differ from your Proton account password. Put the
   username in `.env`:

   ```bash
   BRIDGE_USER=your@proton.me          # the app's IMAP username
   ```

   Write the password into the Docker secret file. The command reads it
   without echoing it, so it stays out of your shell history; paste it
   and press Enter:

   ```bash
   (umask 077; IFS= read -rs pass && printf '%s' "$pass" > .secrets/bridge_pass.txt)
   chmod 600 .secrets/bridge_pass.txt
   ```

   Do not put the Bridge password in `.env` — it is passed to the mbsync
   container exclusively via Docker Compose secrets, mounted at
   `/run/secrets/bridge_pass`. If the app's IMAP port is not 1143, set
   `BRIDGE_IMAP_PORT` in `.env` (1 to 65535, without leading zeros).
3. Take the app's certificate fingerprint and put it in `.env`
   (required). With the app running and signed in (so it, and nothing
   else, holds its port) and its IMAP mode set to SSL (sub-step 1), run
   this in a terminal on the host, replacing 1143 with the app's IMAP
   port if it differs:

   ```bash
   openssl s_client -connect 127.0.0.1:1143 </dev/null \
       | openssl x509 -noout -fingerprint -sha256
   ```

   There is no `-starttls imap`: the app speaks TLS from the first
   byte. If this prints `unable to load certificate` (and `s_client`
   reports a `wrong version number` error), the app is still in STARTTLS
   mode; switch it to SSL and run it again.

   If your Bridge version can export its TLS certificate (in its
   settings), `openssl x509 -in cert.pem -noout -fingerprint -sha256` on
   the exported `cert.pem` gives the same value without a network
   connection. Then:

   ```bash
   BRIDGE_CERT_FINGERPRINT=AB:CD:...   # the value after "Fingerprint="
   ```

   Colon-separated or bare hex, any case, and the whole
   `sha256 Fingerprint=…` line are all accepted. The value is not
   secret. The app's certificate must be issued for `127.0.0.1`, which
   is what Bridge generates; a certificate you imported into Bridge
   yourself must be too, unless you set `BRIDGE_CERT_HOST` to the name
   it is issued for: `localhost`, another `127.x.y.z` or `::1`, nothing
   else.

   Optional settings in the same `.env` block (PLAN.md decision 46;
   `.env.example` lists them): `SOURCE_MODE`, the mail source
   (`proton-bridge`, the default and the only value accepted);
   `BRIDGE_HOST`, the container's name for this Mac
   (`host.docker.internal` by default; OrbStack's `host.orb.internal`
   or `host.internal` also work); `PROTON_FOLDER_EXCLUSIONS`, folders
   mbsync does not sync (`All Mail|Labels/*|Starred`, which must stay in
   the list; you may add more after them, separated by `|`; mail already
   synced from an added folder stays in the Maildir and the index until
   you remove it, see [troubleshooting.md](troubleshooting.md#remove-mail-from-a-folder-you-have-just-excluded)); and
   isync's account, store and channel names
   (`PROTON_IMAP_ACCOUNT_NAME`, `PROTON_REMOTE_STORE_NAME`,
   `PROTON_LOCAL_STORE_NAME`, `PROTON_CHANNEL_NAME`). Leave them unset
   unless you need them. `make validate-env` and mbsync both refuse any
   other value, naming the setting, never the value; the Bridge must
   run on this Mac, and a Bridge elsewhere waits on
   [#1499](https://github.com/marshalltech81/protonmail-local-ai/issues/1499).

#### How TLS is checked

mbsync connects with implicit TLS (the app's SSL mode): the TLS
handshake is the first thing on the connection, so there is no
plaintext phase for anything on the path to strip or inject into. It
keeps certificate verification and the persistent fingerprint pin. The
app's certificate names only `127.0.0.1`, and the isync shipped in the
image (1.5.1) checks a self-signed CA certificate like Bridge's against
the configured host name, so connecting to `host.docker.internal`
directly fails with `certificate owner does not match hostname`.
`BRIDGE_CERT_HOST` therefore defaults to `127.0.0.1`: the
generated `mbsyncrc` keeps `Host 127.0.0.1` for the certificate check
and reaches `host.docker.internal:<port>` through an isync `Tunnel`
(`socat`), which only relays bytes. TLS still runs end to end between
mbsync and the app. See [architecture.md](architecture.md#bridge) for
the details.

The app listens on an unprivileged port on the host's loopback, which
another local account can hold while the app is not running. mbsync
therefore does not trust the first certificate it sees: you give it the
app's fingerprint in `BRIDGE_CERT_FINGERPRINT`, and it refuses any other
certificate, before the pin and before the password is sent, on every
start. Without `BRIDGE_CERT_FINGERPRINT`, `make validate-env` (and so
`make up`) refuses to start, and mbsync itself refuses at startup,
before it waits for or connects to the app. With a value that does not
match, mbsync refuses before logging in and pins nothing; its log shows
the fingerprint it was presented.

### 5. Configure your embedder and inference providers

This project does not ship its own model-serving components. You point
the indexer + mcp-server at any OpenAI-compatible embedder and choose
between an Anthropic-compatible Messages API or any OpenAI-compatible
chat-completions endpoint for inference.

**Required-vars contract (all three layers):**

For every enabled layer (`*_MODE` != `none`; embed is always
enabled), `{LAYER}_API_KEY`, `{LAYER}_MODEL` and `{LAYER}_BASE_URL`
must be non-empty. `{LAYER}_BASE_URL` is the provider's URL, or the
literal `default` (any case) to use the SDK's documented default
endpoint (OpenAI proper for `openai`/`embed` modes, Anthropic API for
`anthropic` mode, Cohere API for `cohere` mode). An empty
`{LAYER}_BASE_URL` stops `make up` and the services at startup, before
any request: an API key is not a choice of provider, because the
request (with mail text) reaches the provider before it checks the key
(#750). Operators pointing at an unauthenticated host-side server (LM
Studio, vLLM, TEI, `mlx_lm.server` for inference only) set `{LAYER}_BASE_URL` to the host
endpoint and supply any non-empty placeholder string (e.g.
`unauthenticated`) for `{LAYER}_API_KEY`.

`INFERENCE_MODE` defaults to `none`, so a fresh install sends nothing
to an inference provider until you choose one below.

> **Migration (#750).** An existing `.env` with an enabled layer and an
> empty or missing `*_BASE_URL` must add `*_BASE_URL=default` (or the
> provider's URL), or `make up`, the indexer and the MCP server refuse
> to start. An `.env` that relied on the old `INFERENCE_MODE` default
> (`anthropic`) without setting it must now set `INFERENCE_MODE`
> explicitly to keep the intelligence tools.

**Embedder** — required, indexer cannot run without it.

The SQLite schema reserves a fixed 4096-dim vector, so `EMBED_MODEL`
must produce 4096-dim vectors. The recommended path is a host-side
server running a 4096-dim model (Qwen3-Embedding-8B family), reached
from the containers via `host.docker.internal`:

```bash
# Edit .env — host-side server (LM Studio, vLLM, TEI):
EMBED_BASE_URL=http://host.docker.internal:8001/v1
# the model name your server serves the 4096-dim model under
EMBED_MODEL=Qwen/Qwen3-Embedding-8B
# write any placeholder string to .secrets/embed_api_key.txt (chmod 600);
# unauthenticated compat servers ignore the bearer header but the
# secret must be non-empty so the startup contract holds.
```

Alternative providers that also serve a 4096-dim Qwen3-Embedding-8B
(DeepInfra, OpenRouter) are listed under "Pointing at a different
embedder provider" below. `EMBED_BASE_URL=default` is
contract-supported (OpenAI proper via the SDK default), but OpenAI's
public embedding catalog has no 4096-dim model today
(`text-embedding-3-large` is 3072-dim, `-3-small` is 1536-dim), so the
`default` path is not a usable recipe without a schema migration.

**Inference** — choose one mode:

```bash
# Anthropic-compatible via the official anthropic SDK.
# INFERENCE_BASE_URL=default hits api.anthropic.com.
# Note: the Anthropic SDK appends '/v1/messages' itself, so when you
# set a URL (compatible gateway, region override), it must NOT end
# with '/v1'.
INFERENCE_MODE=anthropic
INFERENCE_BASE_URL=default                # required; `default` = api.anthropic.com
INFERENCE_MODEL=claude-sonnet-5-5
# write the key to .secrets/inference_api_key.txt (required, non-empty)

# OpenAI-compatible via the official openai SDK.
# INFERENCE_BASE_URL=default hits api.openai.com/v1; or set it to
# any /v1 base URL (host-side server, alternative provider).
INFERENCE_MODE=openai
INFERENCE_BASE_URL=default                # required; `default` = OpenAI proper
INFERENCE_MODEL=gpt-4
# write the key to .secrets/inference_api_key.txt (required, non-empty)
# For unauthenticated host servers, use any placeholder string
# (e.g. `unauthenticated`) — the compat server ignores the bearer
# header but the key must be non-empty so the startup contract holds.

# Disabled (the default) — intelligence tools are not registered,
# and INFERENCE_BASE_URL is not needed.
INFERENCE_MODE=none
```

When pointing at a host-side server, bind it to `127.0.0.1` and use
`http://host.docker.internal:<port>/v1` from the container's
perspective. The project does not provision these servers — install
them with the tool of your choice (LM Studio, vLLM, TEI,
`mlx_lm.server` for inference only, etc.).

**Reranker** — optional; off by default.

```bash
# Default (off):
RERANK_MODE=none

# Cohere via the official cohere SDK.
# RERANK_BASE_URL=default uses the SDK default (api.cohere.com); set
# a URL for proxies, gateways, or region overrides.
RERANK_MODE=cohere
RERANK_BASE_URL=default
RERANK_MODEL=rerank-v4.0-pro
# write the key to .secrets/rerank_api_key.txt
```

When disabled, hybrid search returns RRF-only ranking.

### 6. Start the full stack

```bash
make up
```

`make up` first creates any missing secret file (`make init-secrets`),
then validates `.env` and the secret files. It checks the
values Compose will use: a variable exported in your shell overrides `.env`,
and a key left empty or out takes its `docker-compose.yml` default, so
`SYNC_INTERVAL`, `MCP_PORT` and (in `anthropic` mode)
`INFERENCE_MODEL` may be omitted. A secret file holding only whitespace counts as empty, since the
services strip it. It fails fast if:

- `BRIDGE_USER` is still unset or left at the placeholder value
- `BRIDGE_CERT_FINGERPRINT` is unset or not a SHA-256 fingerprint
- `ACCOUNT_SCOPE` is unset or not a canonical lower-case UUID
  (`make init-secrets` writes one when the line is missing or empty)
- `.secrets/bridge_pass.txt` is missing, empty, or not `600`
- any enabled layer's secret file is missing, empty, or not `600`:
  `inference_api_key.txt` when `INFERENCE_MODE` is `anthropic` or
  `openai`; `embed_api_key.txt` always (`EMBED_MODE` has no `none`
  mode); `rerank_api_key.txt` when `RERANK_MODE=cohere`;
  `mcp_auth_token.txt` always (`MCP_AUTH_TOKEN` in `.env` also fails,
  and so does a token shorter than 32 characters or outside the RFC 6750
  set described above).
  For unauthenticated host-side servers, write any non-empty placeholder
  string (e.g. `unauthenticated`).
- `INFERENCE_API_KEY`, `EMBED_API_KEY` or `RERANK_API_KEY` is set in
  `.env`: Compose never passes it to the containers, so move the value
  to the matching `.secrets/` file
- any enabled layer's `{LAYER}_BASE_URL` is empty: set the provider's
  URL, or `default` for the SDK's default endpoint (OpenAI proper,
  Anthropic API, Cohere API); see the migration note in step 5
- any enabled layer's `{LAYER}_MODEL` is empty (model is always
  required — no SDK has a default model; in `anthropic` mode an empty
  `INFERENCE_MODEL` takes the Compose default `claude-sonnet-5-5`, which
  `openai` mode cannot use)
- inference / embed / rerank secret placeholder files are missing or not `600`
  (validation requires the files to exist with `600` permissions even when the
  matching layer is `none`: Compose cannot create a container whose
  `secrets:` file is missing, and every secret file is held to `600`)
- `config/authority.toml`, when present, is not a regular file (a symlink
  counts as not one) or is not `600`; on a Linux host, also when it does
  not grant the indexer (UID 1002) read access through an ACL naming
  that UID alone (see "Source-authority rules" below)
- `config/identity.toml`, when present, fails the same checks (see
  "Your own addresses" below)
- numeric or enum settings such as `SYNC_INTERVAL`, `MCP_PORT`, `MCP_TRANSPORT`, or `INFERENCE_MODE` are invalid
  (`MCP_TRANSPORT` accepts only `streamable-http` or unset; the removed `sse`
  and `dual` fail with migration steps)

Verify everything is running:

```bash
make logs
```

You should see:
- `mbsync` — "Bridge IMAP port is reachable.", then "Bridge cert
  fingerprint matches the pinned value." (or "First boot — pinned Bridge
  cert fingerprint ..." on the first start), then "Running initial sync..."
- `indexer` — "Running initial index scan..."
- `mcp-server` — "MCP server starting on port 3000"

Operator-supplied inference / embed / rerank endpoints are not in
this list — they run wherever you choose (a remote provider, or a
host-side OpenAI-compatible server such as LM Studio, vLLM,
TEI, or `mlx_lm.server` for inference only). If a layer's container fails to start with a
connection error, see "Embedder or inference endpoint unreachable from
containers" in `docs/troubleshooting.md`.

The initial index scan may take several minutes depending on mailbox size.
It indexes the oldest mail first across every folder, so each message
is indexed before the replies to it and conversations thread correctly
(#752); recent mail becomes searchable last, near the end of the scan.

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
mailboxes; the Bridge app shows its own sync progress.

### Reranker toggle

`RERANK_MODE` can be switched without a reindex. The reranker is a
post-RRF stage with no schema dependency, so turning it on or off
leaves indexing and embeddings untouched.
The default is `none`; to use it, set `RERANK_MODE=cohere`, set
`RERANK_MODEL` (e.g. `rerank-v4.0-pro`), and write the API key to
`.secrets/rerank_api_key.txt`, and set `RERANK_BASE_URL` (`default`
for the Cohere SDK default, or a proxy URL).

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
(`authority_class` on `search_emails`, `query_messages` and
`aggregate_messages`, which also groups by it, and the class plus the
rule that set it on `find_contact`); they never change
ranking. Nothing is classified by a model.

```bash
install -m 600 config/authority.toml.example config/authority.toml
# Linux host only (see below): let the indexer's UID 1002, and no one
# else, read it
setfacl -m u:1002:r config/authority.toml
# edit: one table per class, with `addresses` (exact) and/or
# `domains` (the domain and its subdomains)
make restart-indexer   # a running stack; on first run, make up
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
ownership, so the indexer cannot read a `600` file you own. Compose
cannot fix that from its side: its file-based `secrets:` and `configs:`
are plain bind mounts that ignore `uid`, `gid` and `mode`. Instead,
grant UID 1002 alone read access with a POSIX ACL (the `setfacl` line
above; install the `acl` package if it is missing). `ls -l` then shows
`-rw-r-----+`: the group bits are the ACL mask, not the file's group,
and on Linux `make up` accepts that `640` only when the ACL is exactly
yours read-write and UID 1002 read-only, with nothing for the group or
other accounts. Otherwise it fails and prints the command that resets
the file to that state:
`setfacl -b config/authority.toml && chmod 600 config/authority.toml && setfacl -m u:1002:r config/authority.toml`.
Do not widen the mode or hand the file to a host group instead: GID
1002 may belong to another account on the host. The ACL grants host
UID 1002 read access too, so if `getent passwd` names any account other
than yours with UID 1002 (one sharing your UID counts), `make up` fails:
give that account another UID (as root, `usermod -u <new-uid>
<account>`) or remove it. This check needs `getent` (`libc-bin` on
Debian and Ubuntu).
The indexer must also be able to enter `config/` itself; if the
directory lacks the search bit for the indexer (a checkout made under
a `077` umask, or a `config/` whose group is GID 1002 without the
group search bit), `make up` fails and prints `setfacl -m u:1002:x
config`. This check needs `getfacl` (the `acl` package) unless UID
1002 owns `config/`.
An editor that saves by writing a new file drops the ACL; `make
restart-indexer` then fails with the same command. With rootless
Docker or `userns-remap` the container's UID 1002 maps to a different
host UID, which this check does not cover.
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

### Your own addresses (optional)

List the addresses you send from in `config/identity.toml`, so the index
can tell mail you sent from mail you received (message direction,
#824). The indexer stores the list today; the MCP tools that use it
are not built yet. Without the file your addresses are unconfigured.

```bash
install -m 600 config/identity.toml.example config/identity.toml
# Linux host only: let the indexer's UID 1002, and no one else, read it
setfacl -m u:1002:r config/identity.toml
# edit: addresses = ["you@your-domain", ...]
make restart-indexer   # a running stack; on first run, make up
```

List every address exactly: each alias, each custom-domain address and
each address of every account in Bridge. Nothing is inferred: a domain, a plus-alias
(`you+lists@...`) or the Sent folder adds no address, so an address you
leave out reads as someone else's, and mail you sent from it reads as
received. Case does not matter.

The file is gitignored and holds real addresses, so treat it as
`config/authority.toml` above: never commit it, keep it a regular file
at `600` (on Linux, plus the ACL for UID 1002 alone), and restart with
`make restart-indexer` so the mode is checked; `make up` and `make
restart-indexer` fail as they do for that file, with the same fix
commands. The path `/config/identity.toml` is fixed.

The indexer reads the file at startup and stores the addresses in the
index, so an edit takes effect at the next `make restart-indexer`, with
no reindex and no MCP server restart. The log line `Operator identity:
<n> address(es)` confirms the count (it never prints an address). An
empty or malformed file (invalid TOML, a key other than `addresses`, an
entry that is not a bare `name@domain` address, an address listed
twice, more than 1,000 addresses or more than 64 KiB) stops the indexer
at startup with an error naming the entry's position. Removing the file
clears the stored addresses at the next start.

### Pointing at a different embedder provider

The embedder client (indexer + mcp-server query path) speaks the
OpenAI-compatible `/v1/embeddings` shape, so any compliant provider is
a single env change away. Examples:

```bash
# Host-side server (LM Studio, vLLM, TEI, etc.) —
# the privacy-preserving option; embedding requests stay on the host.
# (EMBED_BASE_URL=default selects OpenAI proper; empty fails startup.)
# EMBED_MODEL is the name your server serves the 4096-dim model under.
EMBED_BASE_URL=http://host.docker.internal:8001/v1
EMBED_MODEL=Qwen/Qwen3-Embedding-8B
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
`EMBED_BASE_URL=default` / SDK-default path remains documented for
symmetry with `INFERENCE_MODE=openai`, but using OpenAI as the
embedder would require a schema migration to a different vector
width — it is not a copy-paste config swap today.

After changing provider:

1. Set the new vars in `.env`. `EMBED_BASE_URL` is always required;
   every provider listed above needs its URL (no compatible
   OpenAI-proper embed model exists today, so the `default` /
   SDK-default path has no usable recipe to copy).
2. Write the API key to `.secrets/embed_api_key.txt` (`chmod 600`).
   `make init-secrets` creates an empty placeholder; the key is
   required (non-empty). For an unauthenticated host-side server, use
   any placeholder string (e.g. `unauthenticated`); compat servers
   ignore the bearer header.
3. **Indexer and mcp-server must point at the same provider + model**
   so query vectors are comparable to indexed vectors.
4. The schema reserves a fixed 4096-dim vector. `EMBED_MODEL`
   must keep producing 4096-dim vectors (Qwen3-Embedding-8B variants)
   or a schema migration is required.
5. On an existing index, an embedder that differs from the one the
   index recorded stops both services at startup with "The configured
   embedder is not the one that built this index" (what is compared,
   and the calibration tolerance, is in docs/architecture.md,
   "Embedder identity record"). Rebuild
   the index with the new embedder, or put the old settings back; see
   "Embedder identity mismatch" in `docs/troubleshooting.md`.

Privacy note: remote embedders receive message subjects, body text
and extracted attachment text at index time and every search
query at retrieval time.
Pointing at a host-side server keeps that traffic on your machine.
Choose accordingly.

The embedder has no enable/disable toggle equivalent to
`RERANK_MODE`. The SQLite schema is sized for 4096-dim vectors and
the indexer needs an embedder — falling back to "no embedder" would
mean a schema rollback plus a full reindex from Maildir.

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

The server answers only requests addressed to `localhost`,
`127.0.0.1`, `[::1]` or `mcp-server` (its name inside the Compose
network), on any port: a request with another `Host` header gets
`421 Misdirected Request`, and a browser `Origin` other than those
names over `http` gets `403`.

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
`make up`, then add the header to each client as above. Until a client
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

A Streamable HTTP session that sees no request for
`MCP_SESSION_IDLE_TIMEOUT_SECS` seconds (default 1800) is ended, so a
session a client abandons without closing it does not hold server
resources until a restart. A client that comes back after that gets
`404` for the old session ID and starts a new session; raise the value
in `.env` if a client of yours does not reconnect on its own.

## Operator configuration file (not yet active)

**Nothing below changes how the stack runs today.** `make up` reads
`.env`, the secret files and `config/*.toml` exactly as described above.
A versioned TOML file with several accounts is the approved direction
(#1353); this section describes its schema and its translator, which
exist now so the mapping can be checked. The launcher that runs the
stack from it, and the explicit choice between `.env` and TOML, come
with #1603.

`config/config.toml` (copy `config/config.toml.example`) holds
`schema_version = 1`, a `[global]` table (the model layers and their
API-key files, the indexer, MCP and the source-authority rules) and one
`[[accounts]]` table per account. It is gitignored and must be mode
`600`: it holds your Bridge username and addresses. Secrets stay in
their files; the TOML holds only their paths, relative to `config/`.

```bash
install -m 600 config/config.toml.example config/config.toml
# edit it, then check it and see what it produces (needs Python 3.11+):
python3 scripts/config/operator_config.py translate
```

The translator reads at most 4 MiB, validates the file, and writes
`.generated/` (gitignored, directories `700`, files `600`): nothing reads
it yet.

- `compose.env`: the settings `.env` holds today, for the global keys and
  the account at `/maildir`, each value in single quotes.
- `compose.accounts.yml`: one collector per enabled account, each with
  the hardening of `docker-compose.yml`'s `mbsync` and only its own
  password secret, Maildir and state volumes and network; the account at
  `/maildir` keeps the names `mbsync`, `bridge_pass`, `maildir-volume`,
  `mbsync-state` and `bridge-net`; any other collector also names
  mbsync's non-root user, `1001:1001`. It also names the model API-key
  files. CI scans this file, generated from synthetic configurations,
  with the Semgrep rules and the Compose hardening checks
  (`scripts/tests/semgrep_generated_test.sh`,
  `scripts/tests/compose_test.sh`).
- `config/identity.toml` and `config/authority.toml`: the files the
  indexer reads today, from the `/maildir` account's
  `identity.addresses` and from `global.classification.authority`; each
  is absent when its table is.

It refuses, naming the key and never the value: an unknown key, a wrong
type, a value out of range, a blank or missing required value, a schema
version other than 1, a file others can read, a file that is not TOML,
and what `make validate-env` would refuse in the result (an enabled
layer's `base_url` that is not `default` or an http(s) URL without
credentials, an anthropic `base_url` ending in `/v1`, chunk budgets or
an inference context that do not fit together, the example's placeholder
username), and any layer's `base_url`, enabled or not, that carries
credentials or is not a URL `mcp-server` can split, and an identity
address or authority pattern the indexer's loaders would refuse (not a
bare address or domain, or repeated). No secret path may name
`.secrets/mcp_auth_token.txt`; this and the file rules below hold for
this checkout's files and for those beside a configuration kept
elsewhere (its own `../.secrets/` and `config/`). With `--out` set, it refuses an
output directory where an output would replace the configuration file,
a secret file it names or a protected file. For the accounts:

- `scope_uuid` is the account's immutable UUID (today's
  `ACCOUNT_SCOPE`); two accounts with the same one are refused. `name`
  is an editable display name that identifies nothing.
- `maildir_root` is `/maildir` for one account at most, and
  `/accounts/<its scope_uuid>` for any other. An enabled account at
  `/maildir` is required while the indexer reads only that one (#1605).
- Each account has its own `password_file`, shared with no other
  account and no model key. A password file is never a model key's
  conventional file (`.secrets/embed_api_key.txt`,
  `.secrets/inference_api_key.txt`, `.secrets/rerank_api_key.txt`), and
  an `api_key_file` never `.secrets/bridge_pass.txt`, even when that
  file's own setting points elsewhere. No password or key file is the
  MCP token's file, `.env`, `config/config.toml`, `config/identity.toml`,
  `config/authority.toml`, one of the translator's outputs under
  `.generated/`, or a `*.pem`, `*.key`, `*.p12` or `*.pfx` file.
- A `proton-bridge` account has a `[accounts.proton_bridge]` table and
  takes exclusions only: a non-empty `sync.mailboxes.include` is
  refused, and `exclude` must keep `All Mail`, `Labels/*` and `Starred`.
  Its connection, certificate, exclusion and isync-name values go
  through the same check as `make validate-env` and mbsync's startup
  (`mbsync/proton-settings.sh`).
- An `imap` account (`[accounts.imap]` with its folder roles) is part of
  the schema, but enabling one is refused until #1606.

Certificate pin rotation and forced deletion are not configuration:
they are the one-run commands `make rotate-bridge-pin` and
`make force-deletion` (#1609).

Every `.env.example` setting, every TOML key and what stays fixed (a
test in `scripts/config/tests` keeps these tables equal to the
translator's). An integer or number is at most 9223372036854775807, the
largest value `scripts/validate-env.sh`'s shell arithmetic reads, and
`-0.0` is read as 0:

<!-- operator-config-mapping:begin -->
| `.env.example` setting | TOML key | Default | Validation | Consumer | After a change |
| --- | --- | --- | --- | --- | --- |
| `SOURCE_MODE` | `accounts[].provider` | required | one of proton-bridge, imap (imap cannot be enabled until #1606) | mbsync, indexer, mcp-server | recreate all three; recorded in the index with the UUID |
| `ACCOUNT_SCOPE` | `accounts[].scope_uuid` | required | UUID, canonical lower case; unique | indexer, mcp-server | never changed: a different UUID is a different account (#1596) |
| `BRIDGE_USER` | `accounts[].connection.username` | required | text | mbsync (mbsyncrc User) | recreate mbsync |
| `BRIDGE_IMAP_PORT` | `accounts[].connection.port` | 1143 (proton-bridge) | integer 1 to 65535; proton-settings.sh | mbsync (Tunnel, probes) | recreate mbsync |
| `BRIDGE_CERT_FINGERPRINT` | `accounts[].proton_bridge.certificate_fingerprint` | required | text; 64 hex digits | mbsync (certificate check) | recreate mbsync |
| `BRIDGE_HOST` | `accounts[].connection.host` | host.docker.internal (proton-bridge) | text; proton-settings.sh allowlist | mbsync (Tunnel, probes) | recreate mbsync |
| `BRIDGE_CERT_HOST` | `accounts[].proton_bridge.certificate_host` | 127.0.0.1 | text; proton-settings.sh loopback names | mbsync (mbsyncrc Host) | recreate mbsync |
| `PROTON_IMAP_ACCOUNT_NAME` | `accounts[].sync.imap_account_name` | protonmail | text; proton-settings.sh names | mbsync (mbsyncrc) | recreate mbsync |
| `PROTON_REMOTE_STORE_NAME` | `accounts[].sync.remote_store_name` | protonmail-remote | text; proton-settings.sh names | mbsync (mbsyncrc, output filter) | recreate mbsync |
| `PROTON_LOCAL_STORE_NAME` | `accounts[].sync.local_store_name` | protonmail-local | text; proton-settings.sh names | mbsync (mbsyncrc) | recreate mbsync |
| `PROTON_CHANNEL_NAME` | `accounts[].sync.channel_name` | protonmail | text; proton-settings.sh names | mbsync (mbsyncrc, output filter) | recreate mbsync |
| `PROTON_FOLDER_EXCLUSIONS` | `accounts[].sync.mailboxes.exclude` | All Mail, Labels/\*, Starred (proton-bridge) | list of text, at most 256; joined with \|; must keep All Mail, Labels/\*, Starred | mbsync (mbsyncrc Patterns) | recreate mbsync; mail already synced stays |
| `SYNC_INTERVAL` | `accounts[].sync.interval_seconds` | 60 | integer >= 1 | mbsync (entrypoint, healthcheck) | recreate mbsync |
| `SYNC_DEADLINE_SECONDS` | `accounts[].sync.deadline_seconds` | 86400 | integer 1 to 999999999 | mbsync (entrypoint, healthcheck) | recreate mbsync |
| `EMBED_BASE_URL` | `global.embedding.base_url` | required | text; URL or default | indexer, mcp-server | recreate indexer and mcp-server; on an existing index it must match the recorded embedder |
| `EMBED_MODEL` | `global.embedding.model` | required | text | indexer, mcp-server | recreate indexer and mcp-server; on an existing index it must match the recorded embedder |
| `EMBED_MODE` | `global.embedding.mode` | openai | one of openai | indexer, mcp-server | recreate indexer and mcp-server |
| `EMBED_TIMEOUT_SECS` | `global.embedding.timeout_seconds` | 60 | number >= 1 | mcp-server | recreate mcp-server |
| `EMBED_BATCH_SIZE` | `global.embedding.batch_size` | 64 | integer >= 1 | indexer | recreate indexer |
| `EMBED_CONCURRENCY` | `global.embedding.concurrency` | 1 | integer >= 1 | indexer | recreate indexer |
| `INITIAL_INDEX_BATCH_SIZE` | `global.indexer.initial_batch_size` | 50 | integer >= 1 | indexer | recreate indexer |
| `INDEXER_STEADY_STATE_BATCH_SIZE` | `global.indexer.steady_state_batch_size` | 8 | integer >= 1 | indexer | recreate indexer |
| `INDEXER_WAL_CHECKPOINT_INTERVAL_SECS` | `global.indexer.wal_checkpoint_interval_seconds` | 600 | integer >= 60 | indexer | recreate indexer |
| `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS` | `global.indexer.recovery_sweep_interval_seconds` | 1800 | integer >= 60 | indexer | recreate indexer |
| `RERANK_MODE` | `global.reranking.mode` | none | one of cohere, none | mcp-server | recreate mcp-server |
| `RERANK_BASE_URL` | `global.reranking.base_url` | required when enabled | text; URL or default; required when enabled | mcp-server | recreate mcp-server |
| `RERANK_MODEL` | `global.reranking.model` | required when enabled | text; required when enabled | mcp-server | recreate mcp-server |
| `RERANK_CANDIDATES` | `global.reranking.candidates` | 20 | integer >= 1 | mcp-server | recreate mcp-server |
| `RERANK_TIMEOUT_SECS` | `global.reranking.timeout_seconds` | 60 | number >= 1 | mcp-server | recreate mcp-server |
| `EMBED_WARMUP_TIMEOUT_SECS` | `global.embedding.warmup_timeout_seconds` | 600 | number >= 1 | indexer | recreate indexer |
| `INFERENCE_MODE` | `global.inference.mode` | none | one of anthropic, openai, none | mcp-server | recreate mcp-server |
| `INFERENCE_BASE_URL` | `global.inference.base_url` | required when enabled | text; URL or default; required when enabled | mcp-server | recreate mcp-server |
| `INFERENCE_MODEL` | `global.inference.model` | required when enabled | text; required when enabled | mcp-server | recreate mcp-server |
| `INFERENCE_TIMEOUT_SECS` | `global.inference.timeout_seconds` | 300 | number >= 1 | mcp-server | recreate mcp-server |
| `INFERENCE_MAX_TOKENS` | `global.inference.max_tokens` | unset: the mode's default | integer >= 1; unset: the mode's default | mcp-server | recreate mcp-server |
| `INFERENCE_CONTEXT_TOKENS` | `global.inference.context_tokens` | unset: the mode's default | integer >= 1; unset: the mode's default | mcp-server | recreate mcp-server |
| `INFERENCE_STRUCTURED_OUTPUT` | `global.inference.structured_output` | true | true or false | mcp-server | recreate mcp-server |
| `INDEXER_DELETION_ENABLED` | `global.indexer.deletion.enabled` | true | true or false | indexer (reconciler) | recreate indexer |
| `INDEXER_DELETION_GRACE_DAYS` | `global.indexer.deletion.grace_days` | 7 | integer >= 0 | indexer (reconciler) | recreate indexer |
| `INDEXER_DELETION_SWEEP_INTERVAL_SECS` | `global.indexer.deletion.sweep_interval_seconds` | 3600 | integer >= 60 | indexer (reconciler) | recreate indexer |
| `INDEXER_DELETION_MAX_BATCH_PCT` | `global.indexer.deletion.max_batch_fraction` | 0.05 | number 0 to 1 | indexer (reconciler) | recreate indexer |
| `INDEXER_MAX_ATTEMPTS` | `global.indexer.retry.max_attempts` | 5 | integer >= 1 | indexer (queue) | recreate indexer |
| `INDEXER_RETRY_BASE_SECONDS` | `global.indexer.retry.base_seconds` | 30 | integer >= 1 | indexer (queue) | recreate indexer |
| `INDEXER_MESSAGE_TIMEOUT_SECONDS` | `global.indexer.message_timeout_seconds` | 3600 | integer >= 0; 0 disables | indexer | recreate indexer |
| `INDEXER_CHUNK_TARGET_TOKENS` | `global.indexer.chunking.target_tokens` | 1000 | integer >= 1; <= max_tokens | indexer | recreate indexer |
| `INDEXER_CHUNK_MAX_TOKENS` | `global.indexer.chunking.max_tokens` | 1500 | integer >= 1 | indexer | recreate indexer |
| `INDEXER_CHUNK_OVERLAP_TOKENS` | `global.indexer.chunking.overlap_tokens` | 150 | integer >= 0; < target_tokens | indexer | recreate indexer |
| `INDEXER_PARSE_MAX_BYTES` | `global.indexer.parse_max_bytes` | 50000000 | integer >= 0; 0 disables | indexer | recreate indexer |
| `INDEXER_ATTACHMENT_EXTRACTION_ENABLED` | `global.indexer.attachments.enabled` | true | true or false | indexer | recreate indexer |
| `INDEXER_OCR_ENABLED` | `global.indexer.attachments.ocr.enabled` | true | true or false | indexer | recreate indexer |
| `INDEXER_ATTACHMENT_MAX_BYTES` | `global.indexer.attachments.max_bytes` | 33554432 | integer >= 1 | indexer | recreate indexer |
| `INDEXER_OCR_MAX_PAGES` | `global.indexer.attachments.ocr.max_pages` | 20 | integer >= 1 | indexer | recreate indexer |
| `INDEXER_OCR_TIMEOUT_SECONDS` | `global.indexer.attachments.ocr.timeout_seconds` | 60 | integer >= 0; 0 disables | indexer | recreate indexer |
| `INDEXER_PDF_MAX_DIGITAL_PAGES` | `global.indexer.attachments.pdf_max_digital_pages` | 500 | integer >= 0; 0 disables | indexer | recreate indexer |
| `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` | `global.indexer.attachments.max_extracted_chars` | 2000000 | integer >= 0; 0 disables | indexer | recreate indexer |
| `MCP_PORT` | `global.mcp.port` | 3000 | integer 1 to 65535 | mcp-server and its loopback port mapping | recreate mcp-server |
| `MCP_TRANSPORT` | fixed: streamable-http | - | - | mcp-server | - |
| `MCP_SESSION_IDLE_TIMEOUT_SECS` | `global.mcp.session_idle_timeout_seconds` | 1800 | number >= 1 | mcp-server | recreate mcp-server |
| `MCP_EXPERIMENTAL_TOOLS` | `global.mcp.experimental_tools` | false | true or false | mcp-server | recreate mcp-server |

| TOML key without a `.env.example` setting | Default | Validation | Consumer | After a change |
| --- | --- | --- | --- | --- |
| `accounts[].name` | required | text | none: a display name, never an identifier | - |
| `accounts[].enabled` | true | true or false | which accounts get a collector in compose.accounts.yml; disabling keeps the data | recreate the collector |
| `accounts[].maildir_root` | required | /maildir (one account at most) or /accounts/<scope_uuid> | /maildir: today's projection (compose.env); /accounts/<scope_uuid>: its collector only, the indexer mount is #1605 | - |
| `accounts[].password_file` | required | path, relative to config/; no other account's or model key's, nor a model key's conventional file | the collector's password secret (bridge_pass) | recreate the collector |
| `accounts[].sync.mailboxes.include` | unset | list of text; empty for proton-bridge | proton-bridge: must be empty; imap: #1606 | - |
| `accounts[].identity.addresses` | table absent: unconfigured | list of 1 to 1000 bare addresses (name@domain); unique | config/identity.toml for the indexer (the account at /maildir) | restart indexer |
| `accounts[].proton_bridge.declared_address_scope` | unverified | one of combined, split, unverified | none until #1593 | - |
| `accounts[].imap.folder_roles.trash` | unset | text | none until #1606 | - |
| `accounts[].imap.folder_roles.sent` | unset | text | none until #1606 | - |
| `accounts[].imap.folder_roles.drafts` | unset | text | none until #1606 | - |
| `accounts[].imap.folder_roles.spam` | unset | text | none until #1606 | - |
| `global.embedding.api_key_file` | ../.secrets/embed_api_key.txt | path, relative to config/; not .secrets/bridge_pass.txt | the embed_api_key secret (indexer, mcp-server) | recreate indexer and mcp-server |
| `global.inference.api_key_file` | ../.secrets/inference_api_key.txt | path, relative to config/; not .secrets/bridge_pass.txt | the inference_api_key secret (mcp-server) | recreate mcp-server |
| `global.reranking.api_key_file` | ../.secrets/rerank_api_key.txt | path, relative to config/; not .secrets/bridge_pass.txt | the rerank_api_key secret (mcp-server) | recreate mcp-server |
| `global.classification.authority.counsel.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.counsel.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.management.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.management.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.vendor.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.vendor.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.government.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.government.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.personal.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.personal.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.other.domains` | unset | list of bare domains, at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |
| `global.classification.authority.other.addresses` | unset | list of bare addresses (name@domain), at most 10000; unique across classes | config/authority.toml for the indexer | restart indexer |

| Runtime-owned, not configurable | Why |
| --- | --- |
| MCP_TRANSPORT | Streamable HTTP at /mcp is the only transport (#498) |
| MAILDIR_PATH | the Maildir mount path, /maildir |
| SQLITE_PATH | the index mount path, /data/mail.db |
| INDEXER_HEALTH_FILE | the indexer's healthcheck file on its tmpfs |
| HOME | the indexer user's home on its tmpfs |
| PATH | the search path the PDF extractor child sets |
| GIT_COMMIT | a build argument make build passes |
| EMBED_API_KEY | a Docker secret; the variable is a fallback outside containers |
| INFERENCE_API_KEY | a Docker secret; the variable is a fallback outside containers |
| RERANK_API_KEY | a Docker secret; the variable is a fallback outside containers |
| MCP_AUTH_TOKEN | .secrets/mcp_auth_token.txt, a fixed path the client helpers read |
| mbsync transport | pull-only sync, Expunge None, Create Near, CopyArrivalDate, SubFolders Legacy, SyncState \*, the isync layout exclusions |
| Bridge trust | implicit TLS through the Tunnel, the fingerprint check, the persistent pin, the host and certificate-name allowlists |
| hardening | networks, volumes, non-root users, read-only root filesystems, capabilities, the loopback-only MCP port |
<!-- operator-config-mapping:end -->

## Updating Bridge

The Bridge app updates itself; nothing in this repository pins or
builds it. If an update (or a reinstall) gives the app a new TLS
certificate, mbsync refuses to sync until you set
`BRIDGE_CERT_FINGERPRINT` to the new fingerprint and accept it once:
see "mbsync refuses to sync — Bridge cert pin mismatch" in
[`troubleshooting.md`](troubleshooting.md).

## Upgrading to the account registry (schema v14)

From schema v14 the index records the account it serves (#1596):
`ACCOUNT_SCOPE` in `.env` and the `SOURCE_MODE` provider, and each
Maildir copy of a message as its own occurrence (#1566). Upgrade with
`make up`, not a plain `docker compose up`:

1. Take a snapshot first: `make backup-index BACKUP_DIR=<directory
   outside the checkout>`.
2. `make build`, then `make up`. `make up` runs `make init-secrets`,
   which writes a new `ACCOUNT_SCOPE` to `.env` when the line is missing
   or empty, and `make validate-env`, which refuses a missing or
   malformed one before any container starts. The indexer then migrates
   the index to v14: it records the account, seeds one occurrence per
   message and queues a reparse of every indexed message, all in the
   same transaction as the version stamp. The log shows `Migration v14
   seeded N mailbox occurrence(s)`. The reparse then re-reads every
   message without embedding calls, so it takes time on a large
   mailbox but costs nothing at the embedder; search keeps working
   throughout ([Reparse or rebuild after an
   upgrade](troubleshooting.md#reparse-or-rebuild-after-an-upgrade)).
3. Keep the new `.env` line with your backups. From now on the index
   only opens under that value.

A plain `docker compose up` without the line starts nothing useful:
Compose warns that `ACCOUNT_SCOPE` is not set, and the indexer and
mcp-server exit with "ACCOUNT_SCOPE is not set" before opening the
index, so nothing is migrated. Run `make up` instead. If a migration is
interrupted, the index stays at v13 with no account recorded, and the
next start retries it (see docs/troubleshooting.md, "Indexer stops
during a schema migration").

## Upgrading to the isync 1.5.1 mbsync image (Debian trixie)

isync 1.5 writes a folder whose name has a non-ASCII character or `&`
under its decoded UTF-8 name, where isync 1.4.4 kept the modified UTF-7
spelling (`Folders/.Caf&AOk-` becomes `Folders/.Café`; see
`docs/architecture.md`, Maildir layout). An existing directory under
the old spelling is not renamed: the first 1.5.1 sync downloads that
folder again into the new directory, so its messages are indexed
twice (conflicting Message-IDs), and the old directory is reported as
a far-side box that "cannot be opened anymore". Before upgrading, with
the old image still running, count the affected directories. isync
1.4.4 always encodes `&`, so a directory name containing `&` is one of
them. The command prints the count only, no folder names:

```bash
docker exec mbsync sh -c 'find /maildir -mindepth 1 -type d -name "*&*" | wc -l'
```

If it prints `0`, upgrade as usual. Otherwise migrate before
upgrading. The migration renames those directories to the names 1.5.1
expects and never deletes anything. That matters: with `Expunge None`,
a folder or subfolder you renamed or deleted in Proton survives only in
the Maildir. Stop the stack (`make down`) and work through the steps
below from the checkout, in one shell. They read your stack's Maildir
volume name from the resolved Compose config, with the same project name
(`-p` or `COMPOSE_PROJECT_NAME`) the stack runs under, and name the
backup volume after it:

```bash
maildir=$(docker compose config --format json \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["maildir-volume"]["name"])')
backup="${maildir}-utf7-backup"
```

1. Back up the affected directories into a separate Docker volume (it
   stays in Docker's storage, like the original). The archive holds
   complete messages, so the volume root is mode 700 and the archive
   mode 600:

   ```bash
   docker run --rm -v "$maildir":/maildir:ro \
     -v "$backup":/backup debian:trixie-slim sh -c \
     'umask 077 && chmod 700 /backup && cd /maildir &&
      find . -mindepth 1 -type d -name "*&*" -prune -print0 |
      tar --null -cf /backup/encoded-folders.tar -T - &&
      chmod 600 /backup/encoded-folders.tar'
   ```

2. Save this script as `utf7-migrate.py`. It renames every directory
   whose name holds modified UTF-7 to its decoded name, deepest first, so
   subfolders, sync state and messages all move with their folder. Within
   a folder it moves every source to a temporary name before giving any
   its final name, since one folder's target can be another's current
   name (`A&-` becomes `A&` while `A&--` becomes `A&-`).
   Without `--apply` it only prints the plan, with your folder names, to
   your terminal. It renames nothing if any target or temporary name
   (`.utf7-migrating-<n>`) already exists, or if a name is not modified
   UTF-7 (for example, when run after upgrading).
   Apply it once only: a second run could decode a name twice (`A&--B`
   becomes `A&-B`, then `A&B`), so `--apply` records a marker in the
   backup volume and the script refuses to run again.

   ```python
   import base64, binascii, os, re, sys

   MARKER = "/backup/utf7-migration-applied"


   def dec(s):
       return re.sub(
           r"&([A-Za-z0-9+,]*)-",
           lambda m: (
               base64.b64decode(m[1].replace(",", "/") + "=" * (-len(m[1]) % 4), validate=True).decode(
                   "utf-16-be"
               )
               if m[1]
               else "&"
           ),
           s,
       )


   if os.path.exists(MARKER):
       sys.exit(
           "stopped: the migration was already applied; running it again "
           "could decode a name twice. Nothing was renamed"
       )
   # Each parent's renames, deepest parent first, planned on the tree as it is.
   batches = {}
   for parent, dirs, _ in os.walk("/maildir"):
       for d in dirs:
           if "&" in d:
               try:
                   new = dec(d)
               except binascii.Error, UnicodeDecodeError:
                   sys.exit(
                       "stopped: a directory name is not modified UTF-7; "
                       "run this only before upgrading; nothing was renamed"
                   )
               if new != d:
                   batches.setdefault(parent, []).append((d, new))
   order = sorted(batches, key=lambda p: p.count(os.sep), reverse=True)
   clash = 0
   for parent in order:
       sources = {src for src, _ in batches[parent]}
       temps = {f".utf7-migrating-{i}" for i in range(len(batches[parent]))}
       for src, dst in batches[parent]:
           print(os.path.join(parent, src), "->", os.path.join(parent, dst))
           # A target that is another source here is moved out of the way first.
           if os.path.exists(os.path.join(parent, dst)) and dst not in sources:
               clash += 1
           # A target spelled like a temporary name would land on a staged folder.
           if dst in temps:
               clash += 1
       # Temporary names must be free: a rename onto one would replace an
       # empty directory, or stop half way through on anything else.
       clash += sum(os.path.lexists(os.path.join(parent, t)) for t in temps)
   if clash:
       sys.exit(f"stopped: {clash} target or temporary name(s) taken; nothing was renamed")
   if sys.argv[1:] == ["--apply"]:
       open(MARKER, "x").close()
       for parent in order:
           staged = []
           for i, (src, dst) in enumerate(batches[parent]):
               tmp = os.path.join(parent, f".utf7-migrating-{i}")
               os.rename(os.path.join(parent, src), tmp)
               staged.append((tmp, os.path.join(parent, dst)))
           for tmp, dst in staged:
               os.rename(tmp, dst)
       print(f"renamed {sum(len(b) for b in batches.values())} directories")
   ```

3. Review the plan, then apply it:

   ```bash
   docker run --rm -v "$PWD/utf7-migrate.py:/m.py:ro" -v "$backup":/backup \
     -v "$maildir":/maildir:ro python:3.14-slim-trixie python /m.py
   docker run --rm -v "$PWD/utf7-migrate.py:/m.py:ro" -v "$backup":/backup \
     -v "$maildir":/maildir python:3.14-slim-trixie python /m.py --apply
   ```

4. Build the new images with the updated checkout: `make build`. This is
   required: `make up` does not rebuild, and starting the old 1.4.4
   image on the renamed folders would recreate the encoded directories
   and download their mail again.

5. Rebuild the index from the Maildir, since every renamed directory
   changes its messages' file paths. Follow `docs/troubleshooting.md`,
   "Indexer refuses to start — wipe the sqlite-volume", which removes
   only the index and runs `make up`.

The first 1.5.1 sync then finds each folder under its decoded name with
its sync state. A folder gone from Proton stays, reported as a far-side
box that "cannot be opened anymore", as before. Keep the backup volume
until the first sync and the rebuilt index look complete, then remove
it (`docker volume rm "$backup"`). That also removes the
script's "already applied" marker, so do not run the script again
afterwards.

## Switching to another Bridge installation

An installation that already synced from one Bridge (for example before
reinstalling the app or moving to another Mac) has three pieces of
state tied to the old Bridge. None of the steps below deletes mail; the last one is
only for state that turns out to be incompatible, and it starts with a
backup.

1. **Stop the stack**: `make down`.
2. **Credentials.** The new Bridge's IMAP username and password differ
   from the old one's. Replace `BRIDGE_USER` and
   `.secrets/bridge_pass.txt` with the new ones (step 4.2 of the setup).
3. **Certificate pin.** Set `BRIDGE_CERT_FINGERPRINT` to the new
   Bridge's fingerprint (step 4.3). It presents a different
   certificate from the old one, so the first start still refuses to
   sync with `Bridge cert fingerprint does not match pinned value`.
   Accept the new certificate once (a rotation accepts only the
   certificate matching `BRIDGE_CERT_FINGERPRINT`):

   ```bash
   make up                 # refused: pin mismatch
   make rotate-bridge-pin  # re-pins in a one-shot container, then recreates mbsync
   docker logs mbsync      # the recreated mbsync: "fingerprint matches the pinned value"
   ```

   The rotation runs in its own container and syncs nothing; the
   recreated mbsync enforces the new pin. See
   [troubleshooting](troubleshooting.md#mbsync-refuses-to-sync--bridge-cert-pin-mismatch).
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
     old credentials, fingerprint and a pin rotation back), or back up
     the Maildir and start it over, with the index, from the new Bridge,
     as in
     [mbsync reports a UIDVALIDITY change](troubleshooting.md#mbsync-reports-a-uidvalidity-change).
   - `Recovered from change of UIDVALIDITY`: the change was spurious;
     isync checked the messages and kept the state. Nothing to do.

## Troubleshooting

See [`troubleshooting.md`](troubleshooting.md) for Bridge, mbsync,
indexer, and MCP client diagnostics and recovery steps.

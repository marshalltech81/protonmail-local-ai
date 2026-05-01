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

This creates `.secrets/bridge_pass.txt` and `.secrets/anthropic_api_key.txt`
as empty placeholders with `600` permissions. Docker Compose requires both files
to exist before starting. You will overwrite them with real values:

- `bridge_pass.txt` — after the Bridge login step below
- `anthropic_api_key.txt` — only if you use `LLM_MODE=cloud`; leave empty for local-only mode

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

### 5. Pull Ollama models

```bash
make pull-models
```

This pulls:
- `nomic-embed-text` — embedding model for search (~274MB)
- `llama3.2` — local LLM for Q&A (~2GB)

`make pull-models` brings the `ollama` container up first (if not already
running) and waits up to 120 seconds for it to report ready before
pulling, so this step works from a clean stack — you do not need to run
`make up` first.

If you plan to use the host-Ollama overlay on macOS (recommended on Apple
Silicon for Metal acceleration), use `make pull-models-host` instead. It
pulls into the native Ollama install via `ollama pull` rather than
`docker exec ollama ollama pull`. See the "Optional: native (host) Ollama
on macOS" section below for one-time host setup.

### 6. Start the full stack

```bash
make up
```

`make up` now validates `.env` and the secret files first. It fails fast if:

- `BRIDGE_USER` is still unset or left at the placeholder value
- `.secrets/bridge_pass.txt` is missing, empty, or not `600`
- `LLM_MODE=cloud` but `.secrets/anthropic_api_key.txt` is missing, empty, or not `600`
- numeric or enum settings such as `SYNC_INTERVAL`, `MCP_PORT`, `MCP_READ_ONLY`, or `LLM_MODE` are invalid

Verify everything is running:

```bash
make logs
```

You should see:
- `protonmail-bridge` — "Starting Bridge in noninteractive mode"
- `mbsync` — "Bridge IMAP is ready" then "Syncing..."
- `indexer` — "Running initial index scan..."
- `ollama` — serving on port 11434
- `mcp-server` — "MCP server starting on port 3000"

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

## Optional: native (host) Ollama on macOS

Containerized Ollama on Apple Silicon cannot use Metal GPU acceleration —
OrbStack runs the linux/arm64 build inside a Linux VM that has no Metal pass-
through. Native Homebrew Ollama uses Metal directly and is typically several
times faster for both inference and embedding.

The repository ships an overlay (`docker-compose.host-ollama.yml`) that drops
the in-stack `ollama` container and points the indexer + mcp-server at a
host-resident Ollama via OrbStack's `host.docker.internal`. The default stack
(`make up`) is unchanged; the overlay opts in.

### One-time host setup

1. **Install Ollama:**

   ```bash
   brew install ollama
   ```

2. **Bind the listener on `0.0.0.0:11434`** so OrbStack containers can reach
   it via `host.docker.internal`. Containers cannot reach the host's
   `127.0.0.1`.

   `brew services` regenerates `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`
   from its template on every `start` / `restart` and discards user edits, so
   the durable pattern is to run Ollama under a separate user-owned LaunchAgent
   that brew does not manage.

   First, stop the brew-managed service:

   ```bash
   brew services stop ollama
   ```

   Then write `~/Library/LaunchAgents/com.local.ollama-host.plist` with the
   following contents (preserves brew's flash-attention + KV cache defaults
   and adds `OLLAMA_HOST`):

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>
     <string>com.local.ollama-host</string>
     <key>EnvironmentVariables</key>
     <dict>
       <key>OLLAMA_HOST</key>
       <string>0.0.0.0:11434</string>
       <key>OLLAMA_FLASH_ATTENTION</key>
       <string>1</string>
       <key>OLLAMA_KV_CACHE_TYPE</key>
       <string>q8_0</string>
     </dict>
     <key>ProgramArguments</key>
     <array>
       <string>/opt/homebrew/opt/ollama/bin/ollama</string>
       <string>serve</string>
     </array>
     <key>KeepAlive</key>
     <true/>
     <key>RunAtLoad</key>
     <true/>
     <key>StandardErrorPath</key>
     <string>/opt/homebrew/var/log/ollama.log</string>
     <key>StandardOutPath</key>
     <string>/opt/homebrew/var/log/ollama.log</string>
     <key>WorkingDirectory</key>
     <string>/opt/homebrew/var</string>
   </dict>
   </plist>
   ```

   Load it with launchd directly:

   ```bash
   launchctl bootstrap "gui/$(id -u)" \
     ~/Library/LaunchAgents/com.local.ollama-host.plist
   ```

   To stop or reload the service later:

   ```bash
   launchctl bootout "gui/$(id -u)/com.local.ollama-host"
   launchctl bootstrap "gui/$(id -u)" \
     ~/Library/LaunchAgents/com.local.ollama-host.plist
   ```

   Verify the bind:

   ```bash
   curl -s http://127.0.0.1:11434/api/tags
   lsof -iTCP:11434 -sTCP:LISTEN  # should show *:11434, not 127.0.0.1:11434
   ```

   Do not run `brew services start ollama` after this point — it would
   spawn a second listener on the same port and the bootstrap will fight
   with brew. If you need to revert, `launchctl bootout` the custom label
   first, then `brew services start ollama` to fall back to the brew
   default (loopback bind).

3. **Enable the macOS Application Firewall** so the listener is not exposed
   to LAN neighbors:

   - System Settings → Network → Firewall → On
   - Click **Options** and enable **Stealth mode**
   - Add an explicit block on the Ollama binary (loopback and the OrbStack
     vmnet bridge are not affected by the Application Firewall, so containers
     still reach Ollama; the LAN does not):

     ```bash
     sudo /usr/libexec/ApplicationFirewall/socketfilterfw \
       --add /opt/homebrew/bin/ollama
     sudo /usr/libexec/ApplicationFirewall/socketfilterfw \
       --blockapp /opt/homebrew/bin/ollama
     ```

4. **Verify reachability from a container** before changing how you start
   the stack:

   ```bash
   docker run --rm curlimages/curl:latest \
     -fsS http://host.docker.internal:11434/api/tags
   ```

   This should return a JSON tag list.

5. **Pull the models on the host** (replaces `make pull-models`, which pulls
   into the container):

   ```bash
   make pull-models-host
   ```

### Day-to-day commands

| Mode | Start | Stop | Logs | Pull models |
|---|---|---|---|---|
| Containerized Ollama (default) | `make up` | `make down` | `make logs` | `make pull-models` |
| Host-Ollama overlay (macOS) | `make up-host-ollama` | `make down-host-ollama` | `make logs-host-ollama` | `make pull-models-host` |

The Open WebUI overlay has a host-Ollama variant too:

```bash
make open-webui-up-host-ollama
```

### Falling back

The overlay is purely additive. To return to the containerized path:

```bash
make down-host-ollama
make up
```

The host LaunchAgent keeps running independently — `make down-host-ollama`
only changes how containers are wired. To free port `11434` and undo the
wildcard bind, stop the LaunchAgent too:

```bash
launchctl bootout "gui/$(id -u)/com.local.ollama-host"
lsof -iTCP:11434 -sTCP:LISTEN  # should now print nothing
```

`brew services stop ollama` does **not** stop the custom LaunchAgent —
brew does not manage the `com.local.ollama-host` label. Use `launchctl
bootout` as shown above. After the LaunchAgent is stopped you can leave
the brew formula installed; the default stack will start its own
container as before.

### Threat model

The host-bound `0.0.0.0:11434` listener is the only externally-reachable
interface this overlay introduces. The mitigations above (stealth mode +
binary block) close the LAN exposure. Same-machine processes can still
reach Ollama without authentication; on a single-user dev laptop this is
the same trust boundary the containerized stack already operates in.
Ollama itself does not have access to the SQLite index or Maildir.

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

### Optional: Run Open WebUI

Open WebUI can provide a local browser UI backed by the existing Ollama
container. It does **not** need a second Ollama instance.

Open WebUI's native MCP integration uses Streamable HTTP, not SSE. To expose
both transports from this server, set this in `.env`:

```bash
MCP_TRANSPORT=dual
```

The Open WebUI session key is a Docker Compose secret (not an env var, so it
stays out of `docker inspect` metadata). `make open-webui-up` auto-generates
`.secrets/open_webui_secret_key.txt` via `openssl rand -base64 32` on first
run; to rotate it later, delete the file and re-run the target.

Start the UI. Signup is **disabled by default** (Open WebUI grants admin to
whoever signs up first; on a multi-user workstation a default-allow posture
risks another local user racing you to admin). For the very first run, flip
the switch on for the admin-creation pass:

```bash
OPEN_WEBUI_ENABLE_SIGNUP=true make open-webui-up
```

Open `http://localhost:8080`, create the first admin account, then add the
MCP server in Open WebUI:

- Type: `MCP (Streamable HTTP)`
- Server URL: `http://mcp-server:3000/mcp`
- Auth: `None`

Because Open WebUI is running in Docker on the Compose network, it should use
container DNS names: `http://ollama:11434` for the model backend and
`http://mcp-server:3000/mcp` for the MCP server. Both are set by
`docker-compose.open-webui.yml`.

After creating the admin account, restart the UI **without** the signup
override so the default-deny posture is back in effect:

```bash
make open-webui-up
```

Keep Open WebUI bound to localhost and backed by Ollama if your goal is fully
local mail conversations.

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

### Ollama container is unhealthy

The official `ollama/ollama` image does not include `curl` or `wget`.
The healthcheck uses `ollama list` — if you see repeated health failures, check:

```bash
docker inspect ollama --format='{{json .State.Health.Log}}'
```

### Ollama model not found

```bash
make pull-models
```

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
and drained by a worker loop. Transient failures (Ollama down, SQLite
lock contention) get exponential backoff; a persistent parser or
schema error transitions the row to `dead` after
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
tell an Ollama outage from a parser bug without digging through logs.
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

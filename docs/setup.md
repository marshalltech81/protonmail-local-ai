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

### 2. Create your environment file

```bash
cp .env.example .env
```

Edit `.env` — you can leave `BRIDGE_USER` and `BRIDGE_PASS` blank for now.
You will fill those in after the Bridge login step.

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

Copy the displayed `Username` and `Password` into your `.env` file:

```bash
BRIDGE_USER=your@proton.me          # from info → Username
BRIDGE_PASS=bridge-generated-pass   # from info → Password
```

### 5. Pull Ollama models

```bash
make pull-models
```

This pulls:
- `nomic-embed-text` — embedding model for search (~274MB)
- `llama3.2` — local LLM for Q&A (~2GB)

### 6. Start the full stack

```bash
make up
```

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

On first run, Bridge must download and decrypt your full mailbox from Proton's
servers before mbsync can pull anything. This can take a long time for large
mailboxes. Watch sync progress with:

```bash
docker compose logs -f protonmail-bridge
```

Bridge logs sync percentage, elapsed time, and ETA directly to Docker logs.

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

Bridge v3+ builds two binaries: a launcher (`bridge`) and the daemon (`proton-bridge`).
If you see this, both binaries are present in the image. Check:

```bash
docker compose logs protonmail-bridge
```

If the image is outdated, rebuild:

```bash
docker compose build protonmail-bridge
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

### mbsync fails to connect

Bridge takes 10–15 seconds to fully start. mbsync waits automatically,
but if it keeps failing:

```bash
docker compose logs mbsync
docker compose logs protonmail-bridge
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
both `indexer/requirements.txt` and `mcp-server/requirements.txt` pin
`sqlite-vec==0.1.9` or later, then rebuild:

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

### Claude Desktop doesn't see the tools

1. Verify the MCP server is running: `docker compose ps`
2. Check the server is responding: `curl -N http://localhost:3000/sse`
3. Verify the Claude Desktop config JSON is valid (no trailing commas)
4. Restart Claude Desktop

### Bridge credentials expired / need to re-authenticate

```bash
make down
docker volume rm protonmail-local-ai_bridge-data
make first-run   # log in again, copy new credentials into .env
make up
```

Your email index is in a separate volume (`sqlite-volume`) and is not affected.

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

### Bridge won't start

```bash
docker logs protonmail-bridge
```

If you see keychain errors, the GPG/pass setup may have failed.
Run `make clean` and repeat from step 4.

### mbsync fails to connect

Bridge takes 10–15 seconds to fully start. mbsync waits automatically,
but if it keeps failing:

```bash
docker logs mbsync
docker logs protonmail-bridge
```

### Ollama model not found

```bash
make pull-models
```

### Index is empty after startup

The initial sync may still be running. Check:

```bash
docker logs indexer
```

### Claude Desktop doesn't see the tools

1. Verify the MCP server is running: `docker compose ps`
2. Verify the port: `curl http://localhost:3000/sse`
3. Check the Claude Desktop config JSON is valid
4. Restart Claude Desktop

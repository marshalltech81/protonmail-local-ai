# protonmail-local-ai

## What This Is
A fully local, privacy-first AI search and intelligence layer for ProtonMail.
Five Docker containers: ProtonBridge, mbsync, Ollama, indexer, and MCP server.
Exposes email search and Q&A tools to Claude Desktop via MCP HTTP/SSE transport.

## Repository
https://github.com/marshalltech81/protonmail-local-ai

## Architecture
- See docs/architecture.md for the full data flow diagram
- All email stays in Docker volumes — nothing leaves the machine by default
- Bridge container builds from Proton's official source using `make build-nogui`
- Index is SQLite with FTS5 (keyword) + sqlite-vec (vector) — hybrid search via RRF
- Thread-level indexing: one SQLite row per conversation, not per message

## Container Map
- bridge/       — ProtonBridge (IMAP:1143, SMTP:1025, internal only)
- mbsync/       — isync daemon, syncs Bridge → Maildir every SYNC_INTERVAL seconds
- indexer/      — Python: parses Maildir, threads emails, embeds via Ollama, writes SQLite
- mcp-server/   — Python: MCP tools over HTTP/SSE on localhost:3000
- ollama        — uses official ollama/ollama image, no custom Dockerfile

## Key Design Decisions
- debian:bookworm-slim for all runtime images — not Alpine (CGO + pass/gpg incompatibility)
- No distroless — Bridge requires pass (a bash script) and gpg for its keychain
- Thread-level indexing because message-level loses conversational context
- Hybrid search (BM25 + vector via RRF) beats either approach alone for email
- MCP uses HTTP/SSE transport — stdio only works when the process runs on the host
- Bridge builds two binaries: `bridge` (launcher) + `proton-bridge` (daemon) — both must be
  copied into the runtime image or the launcher exits with "failed to launch"
- Bridge container runs as non-root user `bridge` (UID 1000) — /data subdirectories are
  pre-created in the image so Docker's copy-on-empty initializes the named volume with
  correct ownership on first mount
- All five XDG vars must be set or Bridge falls back to ~/.local/share inside the container
- Bridge account detection checks for vault.enc at
  $XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc
- sqlite-vec must be ≥0.1.9 on ARM64 — earlier versions ship an armv7 (32-bit) wheel
  that fails with ELFCLASS32 on aarch64 containers (Apple Silicon / ARM servers)
- MCP server uses FastMCP (mcp.server.fastmcp) — the low-level Server class does not
  expose a .tool() decorator in mcp==1.3.0

## MCP Tool Groups
1. Search      — search_emails (hybrid/semantic/keyword)
2. Retrieval   — get_thread, get_message, list_threads, list_folders
3. Intelligence — ask_mailbox, summarize_thread, extract_from_emails
4. Actions     — send_email, reply_to_thread, move_message, mark_read, flag_message
5. System      — get_index_status, get_sync_status

## Environment
- Copy .env.example → .env before running anything
- BRIDGE_USER and BRIDGE_PASS come from Bridge CLI → info (not your Proton password)
- LLM_MODE=local uses Ollama (fully private), LLM_MODE=cloud uses Claude API
- All secrets live in .env which is gitignored — never commit it

## Common Commands
```
make build        # build all images (~5 min first time)
make first-run    # one-time Bridge login
make pull-models  # pull Ollama models
make up           # start stack
make logs         # tail all logs
make status       # container + index health
make clean        # remove all containers and volumes (destructive)
```

## Python Conventions
- Python 3.12 across all services
- No type: ignore comments — fix the types properly
- Async throughout the MCP server (asyncio + httpx async)
- Indexer is sync except for the watchdog event loop
- All new dependencies must be pinned to exact versions in requirements.txt

## What Not to Change
- Do not switch to Alpine — musl/glibc incompatibility with Bridge's CGO is real
- Do not add Qt6 dependencies — Bridge is built with make build-nogui intentionally
- Do not expose any container port other than mcp-server:3000 to the host
- Do not add network: host to any container
- The SQLite schema version is tracked — migrations must increment SCHEMA_VERSION
- Do not change the thread-level indexing strategy without reading docs/architecture.md

## File Structure
```
bridge/                   ProtonBridge container
  Dockerfile              Two-stage: golang:bookworm builder → debian:bookworm-slim runtime
  entrypoint.sh           GPG/pass bootstrap + Bridge launch

mbsync/                   Email sync container
  Dockerfile
  entrypoint.sh           Waits for Bridge, then syncs on loop
  mbsyncrc.template       Config template — envsubst fills credentials at runtime

indexer/                  Parser, threader, embedder, SQLite writer
  src/main.py             Entry point — watchdog + initial scan
  src/parser.py           .eml → Message dataclass
  src/threader.py         Message → Thread (In-Reply-To / References / subject)
  src/database.py         SQLite schema + FTS5 + sqlite-vec writes
  src/embedder.py         Ollama embedding client with retry

mcp-server/               MCP server — Claude Desktop interface
  src/main.py             FastMCP server + SSE transport setup
  src/tools/search.py     search_emails tool
  src/tools/retrieval.py  get_thread, get_message, list_threads, list_folders
  src/tools/intelligence.py  ask_mailbox, summarize_thread, extract_from_emails
  src/tools/actions.py    send_email, reply_to_thread, move_message, mark_read, flag_message
  src/lib/sqlite.py       Read-only SQLite query layer (hybrid search, RRF)
  src/lib/imap.py         IMAP/SMTP client for Bridge operations
  src/lib/ollama.py       Ollama embed + complete client

docs/
  architecture.md         Full data flow, container map, privacy model
  setup.md                Step-by-step first-time setup guide
  mcp-tools.md            MCP tool reference with all parameters
  claude_desktop_config.example.json   Copy into Claude Desktop config

scripts/
  first-run.sh            Wrapper for make first-run
  update.sh               Bridge version update helper
```

## Testing
No test suite yet — this is the next priority after the initial stack runs cleanly.
When adding tests:
- Use pytest for all Python services
- Start with indexer/src/parser.py and indexer/src/threader.py — most logic lives there
- Integration tests should mock IMAP rather than hitting a real Bridge instance
- Use real .eml fixture files for parser tests

## Known Gaps / Next Steps
- reply_to_thread in mcp-server/src/tools/actions.py — returns a clear "not implemented"
  error; needs to fetch last message in thread and call send_email with threading headers
- create_draft — returns a clear "not implemented" error; needs IMAP APPEND to Drafts folder
- Per-session LLM mode toggle (currently set globally via .env)
- Test suite (pytest — start with indexer/src/parser.py and threader.py)
- Attachment download tool
- Schema migration framework — SCHEMA_VERSION is tracked but no migration runner exists yet
- Ollama embedding dimension (768) is hardcoded in indexer/src/database.py line 93 —
  switching embedding models requires a manual schema reset

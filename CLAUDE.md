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
- Bridge builds a single `bridge` binary via `make build-nogui` — prior versions (pre-v3.24)
  produced a separate `proton-bridge` daemon that also needed copying
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
- Bridge binds to 0.0.0.0 (patched from 127.0.0.1 in internal/constants/constants.go
  before compilation) — without this patch mbsync cannot reach Bridge from another container
- Bridge v3 stores ALL credentials and TLS cert inside vault.enc (encrypted) — there are
  no plain .pem files on disk; TLS cert must be extracted from live connection via
  openssl s_client, not from the filesystem
- Bridge uses docker-credential-helpers library (unrelated to Docker the runtime) as its
  cross-platform secret storage abstraction — on Linux this delegates to pass
- Bridge v3 uses bridge-v3 namespace internally (pass store path decodes to
  protonmail/bridge-v3/users/bridge-vault-key)
- mbsync is the only container that should have direct IMAP access to Bridge —
  mcp-server reads SQLite only and must never connect to port 1143 directly
- MCP_READ_ONLY=true is the intended default — write operations should be opt-in, not opt-out
  (not yet implemented — see Pending Implementation Queue → Read-only protection)

## Bridge-Specific Operational Notes
- Bridge's "syncing" log messages refer to its internal Gluon database sync with Proton's
  API servers — this is NOT the same as mbsync downloading to Maildir
- Gluon is Bridge v3's internal IMAP library — it has a known bug with All Mail and
  Labels/* folders causing IMAP CLOSE errors during expunge; exclude both from Patterns
- Bridge must complete its internal Gluon sync before mbsync can download messages —
  on first run with a large mailbox this can take hours
- bridge.lock at $XDG_CACHE_HOME/protonmail/bridge-v3/bridge.lock can cause "another
  instance running" errors after a hard crash — delete it to recover
- Bridge restart recovers automatically via restart: unless-stopped in compose file
- TLS cert extraction command (run after sync is stable):
  docker run --rm --network protonmail-local-ai_protonmail-net debian:bookworm-slim \
  bash -c "apt-get install -y openssl -qq 2>/dev/null && echo | openssl s_client \
  -connect protonmail-bridge:1143 -starttls imap 2>/dev/null | openssl x509" \
  > ./mbsync/bridge-cert.pem

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

## Secret Safety — Before Every Commit
Before staging or committing, verify no secrets are included:
```bash
git diff --staged | grep -iE '(password|pass|secret|token|key|credential)' | grep '^\+'
```
Files that must never be committed:
- `.env` — Bridge credentials, API keys (gitignored, but verify with `git status`)
- `mbsync/bridge-cert.pem` — extracted TLS cert (gitignored)
- Any file ending in `.pem`, `.key`, `.p12`, `.pfx`

If a secret is accidentally committed, treat it as compromised immediately:
rotate the credential, then remove it from git history with `git filter-repo`
(do not use `git rebase` or `git commit --amend` — they leave the secret in the reflog).

## Common Commands
```
make build        # build all images (~5 min first time)
make first-run    # one-time Bridge login
make pull-models  # pull Ollama models
make up           # start stack
make logs         # tail all logs
make status       # container + index health
make clean        # remove all containers and volumes (destructive)
# make recert — not yet a Makefile target; use the manual docker run command in
#               Bridge-Specific Operational Notes → TLS cert extraction command
```

## Python Conventions
- Python 3.14 across all services
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
- Do not give mcp-server or indexer direct network access to Bridge IMAP port 1143
- Do not set Sync All in mbsyncrc.template — Sync Pull only, never write back to Proton
- Do not remove Expunge None from mbsyncrc.template
- Do not add All Mail or Labels/* to mbsync Patterns — causes Gluon IMAP CLOSE errors

## File Structure
```
bridge/                   ProtonBridge container
  Dockerfile              Two-stage: golang:bookworm builder → debian:bookworm-slim runtime
  entrypoint.sh           GPG/pass bootstrap + Bridge launch

mbsync/                   Email sync container
  Dockerfile
  entrypoint.sh           Waits for Bridge, then syncs on loop
  mbsyncrc.template       Config template — envsubst fills credentials at runtime
  bridge-cert.pem         Bridge TLS cert — extracted automatically by entrypoint.sh at startup

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
                          MCP_READ_ONLY guard not yet implemented — see Pending Implementation Queue
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

## Pending Implementation Queue
Work through these in order. Do not skip ahead.

### Next immediate action
1. Wait for initial ProtonMail sync to complete (watch: docker logs -f mbsync)
2. Extract TLS cert: run the docker run command in Bridge-Specific Operational Notes above
   (make recert is not yet a Makefile target)
3. Verify cert extracted: cat mbsync/bridge-cert.pem

### Security hardening
- [x] TLS cert pinning in mbsync — entrypoint extracts cert via openssl s_client on
      every container start; CertificateFile points to /home/mbsync/bridge-cert.pem
- [ ] PassCmd instead of plain BRIDGE_PASS env var in mbsyncrc.template
- [ ] Split Docker networks: bridge-sync-net (Bridge+mbsync) and protonmail-net (rest)

### mbsync improvements
- [ ] Change Sync All → Sync Pull in mbsyncrc.template
- [ ] Exclude All Mail and Labels/* from Patterns in mbsyncrc.template
- [ ] Explicit SyncState /maildir/.mbsyncstate in mbsyncrc.template
- [ ] IMAP IDLE investigation — replace sleep loop with push notifications

### Read-only protection
- [ ] MCP_READ_ONLY=true default in .env.example
- [ ] Read-only guard function in mcp-server/src/tools/actions.py
- [ ] sqlite-volume mounted :ro in mcp-server in docker-compose.yml

### Known gaps (existing)
- [ ] reply_to_thread — needs fetch last message + send_email with threading headers
- [ ] create_draft — needs IMAP APPEND to Drafts folder
- [ ] Per-session LLM mode toggle (currently global via .env)
- [ ] Test suite (pytest — start with parser.py and threader.py)
- [ ] Attachment download tool
- [ ] Schema migration framework — SCHEMA_VERSION tracked but no runner exists
- [ ] Ollama embedding dimension (768) hardcoded in database.py line 93 —
      switching models requires manual schema reset

### Future projects
- [ ] Extract Bridge container into standalone repo `protonmail-bridge-headless`
      once current Bridge work is complete and stable


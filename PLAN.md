# PLAN.md

## Purpose

This file tracks the current working plan for the repository.

Use this file for:
- active priorities
- ordered implementation work
- blockers and open decisions
- short-term backlog

Do not use this file for permanent architectural rules.
Permanent constraints belong in `AGENTS.md`.
Detailed design and operational docs belong in `docs/`.

## Current Objective

Bring the local ProtonMail stack to a secure, stable, and testable baseline for daily use, with safe defaults and clear operational behavior.

## Current State

Implemented and working at a high level:

- ProtonBridge runs headless in Docker
- mbsync pulls mail from Bridge into Maildir
- indexer parses and threads messages
- Ollama embeddings are stored in SQLite with FTS5 + sqlite-vec
- MCP server exposes mailbox tools over HTTP/SSE
- Python services now use per-service `uv` projects with `pyproject.toml` and `uv.lock`
- Bridge TLS cert extraction is automated in `mbsync/entrypoint.sh`
- Bridge password is handled as a Docker Compose secret

Known limitations:

- initial sync may take a long time on large mailboxes
- attachments are not indexed yet
- `mcp-server` still expects `BRIDGE_PASS` from the environment instead of the current secret flow
- `mcp-server` SMTP send path still disables TLS verification
- read-only protections are not fully enforced yet
- intelligence tools rely too heavily on stored snippets instead of full-thread context
- test coverage is incomplete
- schema migration support is still minimal
- some MCP action features are incomplete
- `list_threads(filter_type=...)` does not yet match the documented interface

## Daily-Driver Gaps

The core sync and indexing path is close, but the following gaps still block
confident daily-driver use.

### 1. Secure live Bridge access for `mcp-server`

Why it matters:
- retrieval and action tools depend on live IMAP/SMTP access through Bridge
- current wiring does not cleanly provide Bridge credentials to `mcp-server`
- the current implementation still expects `BRIDGE_PASS` from the environment, while Compose only provides the Bridge password to `mbsync`
- SMTP/TLS handling should be aligned with the stricter cert-pinned approach used by `mbsync`
- SMTP send currently disables hostname and certificate verification entirely

Tasks:
- replace the current `BRIDGE_PASS` environment dependency with a secret-handling path that actually reaches `mcp-server`
- make `mcp-server` use the same trust model as `mbsync` for Bridge TLS where practical
- remove `CERT_NONE` / `check_hostname = False` from the SMTP path unless there is a narrowly justified pinned-cert alternative
- verify live retrieval, send, move, and flag operations against a real Bridge session

### 2. Enforce read-only safety by default

Why it matters:
- mailbox mutation should be opt-in, not available by default
- current action tools are registered without a read-only guard
- `send_email`, `move_message`, `mark_read`, and `flag_message` are still exposed by default

Tasks:
- set `MCP_READ_ONLY=true` by default in `.env.example`
- gate action tool registration in `mcp-server/src/main.py`
- add a read-only guard in `mcp-server/src/tools/actions.py`
- ensure all mutating paths fail safely with a clear user-facing message

### 3. Improve intelligence fidelity

Why it matters:
- mailbox Q&A is one of the main reasons to use this project daily
- current `ask_mailbox`, `summarize_thread`, and `extract_from_emails` flows rely too much on snippets, which can miss key context from earlier messages in a thread
- some current prompts describe snippet-based context as if it were full-thread content

Tasks:
- use full-thread stored context, or fetch richer thread content, when building LLM prompts
- stop presenting snippet-derived context as full thread content in prompts or tool descriptions
- verify answers remain grounded in retrieved email content
- add tests covering missed-context regressions

### 4. Expand MCP test coverage

Why it matters:
- indexer coverage is in decent shape, but MCP behavior is still lightly tested
- search, retrieval, ranking, and action regressions are too risky to rely on manual verification alone

Tasks:
- add tests for SQLite keyword, semantic, and hybrid/RRF search behavior
- add tests for MCP tool registration and user-facing error handling
- add tests for retrieval and action flows using mocks instead of a live Bridge instance

### 5. Tighten incomplete or misleading tool behavior

Why it matters:
- daily use requires the tool surface to match what it claims to support
- incomplete tools and partially implemented filters create trust issues quickly
- `list_threads(filter_type=...)` still ignores the filter parameter
- `reply_to_thread` and `create_draft` are still stubs

Tasks:
- implement or hide `reply_to_thread`
- implement or hide `create_draft`
- make `list_threads` filter behavior match the documented interface, or reject unsupported filter values clearly
- add attachment download support only after the read-only policy is explicit

## Active Priorities

Work these in order.

### 1. Confirm stable first-run and sync behavior

Goal:
- ensure first-run, Bridge auth, cert extraction, and initial mail sync are reliable

Definition of done:
- first-run succeeds without leaking credentials to Docker logs
- mbsync reliably connects after Bridge comes up
- sync behavior is repeatable after container restarts

### 2. Secure live Bridge access for `mcp-server`

Goal:
- make retrieval and action tools work reliably against Bridge without weakening secret handling or TLS safety

Tasks:
- wire Bridge credentials into `mcp-server` safely using a real secret-backed path
- align Bridge TLS handling with current `mbsync` trust expectations
- remove the current SMTP no-verify fallback unless there is a pinned-cert replacement
- verify retrieval and action tools against a real session

Definition of done:
- `get_thread` / `get_message` can fetch live content reliably
- send/move/flag operations authenticate successfully when enabled
- secret handling remains Docker-secret-first and local-only

### 3. Enforce read-only safety by default

Goal:
- make mailbox mutation opt-in instead of opt-out

Tasks:
- set `MCP_READ_ONLY=true` by default in `.env.example`
- gate mutating tool registration by read-only mode
- add a read-only guard in `mcp-server/src/tools/actions.py`
- mount SQLite volume read-only in `mcp-server` where appropriate

Definition of done:
- default startup does not allow mail-changing operations
- action tools fail safely with a clear message when read-only mode is enabled

### 4. Improve intelligence fidelity

Goal:
- make mailbox Q&A and summarization trustworthy enough for routine use

Tasks:
- reduce reliance on thread snippets when building prompts
- use richer thread context in `ask_mailbox`, `summarize_thread`, and `extract_from_emails`
- make prompt wording accurately reflect the actual context provided
- add regression tests for multi-message context loss

Definition of done:
- answers and summaries consistently reflect whole-thread context
- structured extraction is based on more than the latest snippet

### 5. Harden network boundaries

Goal:
- reduce unnecessary cross-container access

Tasks:
- split Docker networks so Bridge and mbsync are isolated from the rest where possible
- verify only required services can reach Bridge IMAP/SMTP

Definition of done:
- Bridge-facing traffic is limited to the minimum necessary containers
- architecture docs reflect the final network layout

### 6. Expand test coverage

Goal:
- make parser, threader, database, and MCP behavior safer to change

Tasks:
- keep parser/threader/database tests passing
- add `mcp-server` tests starting with SQLite search and RRF logic
- add tests for `list_threads(filter_type=...)` behavior
- add tests for read-only action-tool gating and user-facing failure paths
- add integration coverage for indexer watchdog behavior using mocks

Definition of done:
- core indexing and retrieval paths have automated coverage
- risky refactors can be validated without manual mailbox testing

## Near-Term Backlog

### mbsync improvements
- investigate IMAP IDLE as a replacement for the sync sleep loop
- confirm current Patterns and expunge behavior remain safe
- keep sync strictly pull-only

### MCP feature completion
- implement `reply_to_thread`
- implement `create_draft`
- add attachment download support
- verify action tools respect read-only guardrails
- either implement `list_threads` filters or narrow the documented interface

### Attachment indexing
- index attachment filenames and MIME types for search/filtering
- evaluate safe local text extraction for common attachment types
- do not store raw attachment binaries in SQLite
- add tests for attachment parsing and search behavior

### Schema and embeddings
- build a real migration runner around `SCHEMA_VERSION`
- document and enforce embedding dimension assumptions
- make model-switch behavior explicit and safe

## Later Backlog

- per-session LLM mode toggle
- extract Bridge container work into standalone repo after stabilization
- improve operational observability and health reporting
- add `/health` endpoint for `mcp-server` and wire it into `HEALTHCHECK`
- decide whether `mcp-server` should eventually use live IMAP retrieval only as fallback once richer thread context is available locally

## Blockers and Risks

### Initial Proton sync duration
Large mailboxes may take hours before useful indexing begins.
Do not assume indexing bugs until Bridge internal sync has completed.

### Bridge TLS and cert behavior
Bridge cert behavior is tied to `vault.enc` and patched SAN handling.
Do not modify this casually.

### Schema sensitivity
Changes to SQLite schema, embedding dimensions, or thread model can invalidate existing assumptions and stored data.

## Open Decisions

### 1. Network split design
Need final decision on exact Docker network topology:
- one shared network with tighter service rules
- or two explicit networks separating Bridge/mbsync from the rest

### 2. Read-only policy surface
Need final decision on whether read-only mode blocks:
- only action tools
- action tools plus any SMTP send path
- all mutating paths including draft creation and move/flag operations

### 3. IMAP strategy
Need decision on whether polling remains acceptable or whether IMAP IDLE is worth the added complexity.

## Recently Completed

- automated Bridge TLS cert extraction on container start
- Bridge password moved to Docker Compose secret
- `Sync All` changed to `Sync Pull`
- `All Mail` and `Labels/*` excluded from mbsync Patterns
- explicit sync state path added in `mbsyncrc.template`

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Treat this file as the current execution plan, not as permission to ignore architectural constraints.
- When a task is completed, move it to `Recently Completed` or remove it.
- Keep this file concise and current.

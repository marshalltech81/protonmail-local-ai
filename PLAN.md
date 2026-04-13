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

Validate the new safe local-first baseline under real first-run/sync conditions,
then improve retrieval fidelity, test coverage, and tool completeness.

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
- `mcp-server` now runs in read-only mode by default and serves retrieval from the local SQLite index
- mail-changing MCP tools are no longer registered in the default deployment
- Bridge-facing traffic is now isolated from the application network
- `mbsync` and `mcp-server` now use a tighter runtime profile with read-only root filesystems, `tmpfs`, `no-new-privileges`, dropped capabilities, and `pids_limit`
- the long-lived `bridge` and `mbsync` service users now use non-login shells
- the Bridge build now uses a shared patch helper plus a dedicated patch-drift check and build/runtime smoke-test path for version bumps

Known limitations:

- initial sync may take a long time on large mailboxes
- attachments are not indexed yet
- per-message live retrieval and mail-changing actions are disabled in the default deployment until a safe action backend is implemented
- intelligence tools rely too heavily on stored snippets instead of full-thread context
- test coverage is incomplete
- schema migration support is still minimal
- some MCP action features are incomplete
- `list_threads(filter_type=...)` does not yet match the documented interface
- the new hardened first-run/sync path still needs real end-to-end validation against a live Bridge session

## Daily-Driver Gaps

The core sync and indexing path is close, but the following gaps still block
confident daily-driver use.

### 1. Validate the hardened first-run and sync path

Why it matters:
- the Tier 1 safety baseline is now implemented in code and Compose, but it still needs live operational proof
- `mbsync` now depends on `/tmp` runtime state under a read-only root filesystem
- first-run, cert extraction, and steady-state sync are the remaining practical questions before calling the baseline routine-use ready

Tasks:
- run the new first-run and sync flow against a real Bridge session
- verify `mbsync` cert extraction and sync looping still behave correctly with the new `/tmp` runtime paths
- verify the default deployment remains useful when Bridge is unavailable and only the local index is present
- capture any operator-facing recovery or troubleshooting notes that fall out of the live validation

### 2. Improve intelligence fidelity

Why it matters:
- mailbox Q&A is one of the main reasons to use this project daily
- current `ask_mailbox`, `summarize_thread`, and `extract_from_emails` flows rely too much on snippets, which can miss key context from earlier messages in a thread
- some current prompts describe snippet-based context as if it were full-thread content

Tasks:
- use full-thread stored context, or fetch richer thread content, when building LLM prompts
- stop presenting snippet-derived context as full thread content in prompts or tool descriptions
- verify answers remain grounded in retrieved email content
- add tests covering missed-context regressions

### 3. Expand MCP test coverage

Why it matters:
- indexer coverage is in decent shape, but MCP behavior is still lightly tested
- the new local-only/read-only baseline should be protected against regression
- search, retrieval, ranking, and action regressions are too risky to rely on manual verification alone

Tasks:
- add tests for SQLite keyword, semantic, and hybrid/RRF search behavior
- add tests for MCP tool registration and user-facing error handling
- add tests for the local-only/read-only default behavior using mocks instead of a live Bridge instance
- add tests for retrieval and action flows using mocks instead of a live Bridge instance

### 4. Tighten incomplete or misleading tool behavior

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

### 1. Confirm stable first-run and sync behavior under the hardened baseline

Goal:
- ensure first-run, Bridge auth, cert extraction, and initial mail sync remain reliable after the Tier 1 hardening changes

Tasks:
- validate first-run against a real Bridge account
- verify `mbsync` works correctly with generated config/cert material under `/tmp`
- verify the default deployment still provides useful local retrieval/intelligence when Bridge is unavailable
- document any live operational caveats discovered during validation

Definition of done:
- first-run succeeds without leaking credentials to Docker logs
- mbsync reliably connects after Bridge comes up
- sync behavior is repeatable after container restarts

### 2. Improve intelligence fidelity

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

### 3. Expand test coverage

Goal:
- make parser, threader, database, and MCP behavior safer to change

Tasks:
- keep parser/threader/database tests passing
- add `mcp-server` tests starting with SQLite search and RRF logic
- add tests for `list_threads(filter_type=...)` behavior
- add tests for read-only action-tool gating, non-registration, and user-facing failure paths
- add tests for local-only retrieval and system-status behavior
- add integration coverage for indexer watchdog behavior using mocks

Definition of done:
- core indexing and retrieval paths have automated coverage
- risky refactors can be validated without manual mailbox testing

### 4. Tighten incomplete or misleading tool behavior

Goal:
- make the exposed MCP surface truthful and dependable

Tasks:
- implement or hide `reply_to_thread`
- implement or hide `create_draft`
- make `list_threads` filter behavior match the documented interface, or reject unsupported filter values clearly
- keep action-tool docs and registration behavior aligned

Definition of done:
- the documented tool surface matches runtime behavior
- unsupported paths fail clearly instead of implying functionality that does not exist

### 5. Preserve the Tier 1 safety baseline

Goal:
- keep the new local-only, read-only, and split-network defaults from regressing over time

Tasks:
- keep `mcp-server` local-index-first and independent from Bridge availability
- keep mail-changing action tools out of the default registration path
- verify only required services can reach Bridge IMAP/SMTP
- add tests and docs that make accidental regression obvious

Definition of done:
- the default deployment remains local-first and read-only
- direct Bridge access remains limited to `mbsync`

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
- add OCR-based text extraction for image attachments such as `.png` and `.jpg`
- add OCR-based text extraction for scanned PDFs
- evaluate transcription support for common audio/video attachment types
- add content-hash-based deduping for expensive extraction work so identical attachments are not OCRed or transcribed repeatedly
- preserve per-message attachment occurrences even when extracted content is deduped
- do not store raw attachment binaries in SQLite
- log unsupported or unparsed attachment types in application logs so parser gaps are visible in real-world use
- add tests for attachment parsing and search behavior

### Guarded live Bridge integration CI
- keep normal PR CI secret-free and mocked; do not put live Proton login in the default PR workflow
- add a separate `.github/workflows/bridge-integration.yml` triggered only by `workflow_dispatch` and optionally a nightly schedule on `main`
- never run the live Bridge workflow on fork PRs or with `pull_request_target`
- use a protected GitHub Environment such as `proton-integration` for all live-test secrets and approvals
- if using GitHub-hosted runners without `PROTON_TEST_TOTP_SECRET` and without pre-provisioned Bridge state, use a dedicated paid Proton test account with 2FA disabled
- store only `PROTON_TEST_EMAIL` and `PROTON_TEST_PASSWORD` in the protected environment for the GitHub-hosted no-2FA path
- build a single-session PTY helper such as `scripts/bridge-first-run.expect` that logs in and captures `info` within the same Bridge CLI session
- keep the current first-run no-log compose override in place while the helper runs
- have the helper write `BRIDGE_USER` into a CI-only env file and `BRIDGE_PASS` into `.secrets/bridge_pass.txt` with `700` on `.secrets` and `600` on the secret file
- do not echo Proton credentials, Bridge credentials, TOTP codes, or full `info` output back to workflow logs
- keep the live workflow focused on smoke coverage only: Bridge health, mbsync auth, cert extraction, and one or two retrieval/action checks
- let the runner tear down ephemeral Bridge state after the run; do not upload Bridge data volumes as artifacts

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

### Search and intelligence expansion
- add attachment-aware retrieval with provenance so results can cite message ID, attachment filename, and page/time range where applicable
- move toward dual retrieval units: thread-level for conversation understanding plus message/attachment-level chunks for precise lookup
- make intelligence answers consistently grounded in exact message and attachment evidence instead of summary-only responses
- add structured extraction for high-value mailbox entities such as invoices, dates, addresses, contracts, approvals, and calendar details
- improve sender/contact identity normalization across aliases, display names, and mailing-list patterns
- add near-duplicate handling for repeated attachments, forwards, and duplicated content across folders
- add faceted search by date, sender, recipient, domain, folder, attachment type, and extracted document/entity signals
- add targeted reindex and parser-version-driven reprocessing so parser improvements do not require full rebuilds

### Product experience expansion
- add a trusted "ask my mailbox, show receipts" experience with direct citations to the exact supporting messages and attachments
- surface action-oriented views such as unanswered threads, waiting-on-me, waiting-on-them, deadlines, invoices due, and contracts needing attention
- build stronger attachment intelligence for PDFs, scanned documents, images, audio, and video once extraction pipelines are in place
- build a local entity memory for people, companies, projects, dates, invoices, addresses, and commitments derived from email and attachments
- add thread-state understanding that highlights decisions, unresolved questions, owners, and what changed since the last reply
- support saved queries and persistent monitors for high-signal conditions such as large invoices, outage mentions, expiring contracts, or new security alerts

### Bridge strategy improvements
- continue treating Bridge as the Proton-facing ingress/egress boundary, not the primary retrieval backend for search and Q&A
- keep reads centered on Maildir and SQLite where possible, and resist reintroducing live Bridge retrieval into the default data plane
- if a future opt-in Bridge-adjacent write backend is added, give it its own secret-handling path instead of reviving `BRIDGE_PASS` environment wiring in `mcp-server`
- align any future Bridge-adjacent write transport with the stricter cert-pinned trust model already used by `mbsync`
- improve Bridge readiness checks so they reflect useful IMAP availability, not just an open TCP port
- preserve degraded local-search mode when Bridge or mbsync are unavailable, with only future opt-in live paths disabled
- add a safer single-session first-run helper that keeps Proton login interactive but automates same-session `info` capture into `.env` and `.secrets/bridge_pass.txt`
- extend the Bridge smoke-test path beyond build/runtime validation to cover live auth, cert, and IMAP readiness once guarded live Bridge CI exists
- document and test backup, restore, and rollback handling for the `bridge-data` volume so Bridge upgrades and recovery are safer
- add better operator tooling such as a `make bridge-status`-style diagnostic path for auth state, Gluon sync state, recent logs, and IMAP readiness
- keep validating the new read-only Bridge rootfs baseline as Bridge versions change
- pin production images by digest where practical and keep Bridge runtime packages to the smallest set the service actually needs
- audit Bridge and `mbsync` runtime packages regularly and remove unused tools or libraries once verified unnecessary
- preserve and strengthen default seccomp/AppArmor confinement; only loosen profiles when Bridge proves it requires it
- add resource controls such as memory limits, `pids_limit`, and log rotation so Bridge failure modes are more contained
- document host-level hardening expectations for Docker itself, including full-disk encryption for Docker data, encrypted backups, and stronger daemon isolation such as rootless Docker, `userns-remap`, or Docker Desktop Enhanced Container Isolation where available
- tighten Bridge-facing network boundaries further if any future Bridge-adjacent service is added beyond `mbsync`

## Blockers and Risks

### Initial Proton sync duration
Large mailboxes may take hours before useful indexing begins.
Do not assume indexing bugs until Bridge internal sync has completed.

### Bridge TLS and cert behavior
Bridge cert behavior is tied to `vault.enc` and patched SAN handling.
Do not modify this casually.

### Schema sensitivity
Changes to SQLite schema, embedding dimensions, or thread model can invalidate existing assumptions and stored data.

### Live Bridge CI credential limits
Without `PROTON_TEST_TOTP_SECRET`, pre-provisioned `BRIDGE_USER` / `BRIDGE_PASS`,
or an already authenticated persistent Bridge state, GitHub-hosted automation
only works cleanly with a dedicated Proton test account that has 2FA disabled.
Otherwise keep live Bridge testing manual or move it to a trusted self-hosted runner.

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

### 4. Live Bridge integration lane
Need final decision on the long-term home for live Bridge smoke tests:
- GitHub-hosted manual/nightly workflow using a dedicated no-2FA Proton test account
- or a trusted self-hosted runner with persistent authenticated Bridge state

## Recently Completed

- automated Bridge TLS cert extraction on container start
- fail-closed `mbsync` cert extraction so sync refuses to proceed without Bridge cert pinning
- Bridge password moved to Docker Compose secret
- `Sync All` changed to `Sync Pull`
- `All Mail` and `Labels/*` excluded from mbsync Patterns
- explicit sync state path added in `mbsyncrc.template`
- explicit `700` permissions on sensitive Bridge state directories under `/data`
- read-only Bridge root filesystem with compose-level smoke coverage for writable-path expectations
- Bridge writable runtime paths narrowed to `/data`, `/tmp`, and a bridge-owned `/home/bridge` tmpfs

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Treat this file as the current execution plan, not as permission to ignore architectural constraints.
- When a task is completed, move it to `Recently Completed` or remove it.
- Keep this file concise and current.

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
- add test for circular `In-Reply-To` references (document expected behavior even if not explicitly handled)
- add `pytest-cov` to CI and include coverage output in the test report

Definition of done:
- core indexing and retrieval paths have automated coverage
- risky refactors can be validated without manual mailbox testing
- coverage output is visible in CI so regressions are caught early

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

### 6. Address review-identified safety and correctness gaps

Goal:
- fix concrete correctness and security issues identified during codebase review

Tasks:
- wrap multi-step database writes (threads, message_thread_map, indexed_files, FTS5, vec0) in explicit `BEGIN IMMEDIATE / COMMIT / ROLLBACK` transactions in `indexer/src/database.py`
- add deduplication guard before accumulating `body_text` so re-indexed messages are not appended twice
- restore TLS validation in `mcp-server/src/lib/imap.py`: set `verify_mode = ssl.CERT_REQUIRED` and `check_hostname = True`
- add redaction for `ANTHROPIC_API_KEY` and similar credential values in error log paths when `LLM_MODE=cloud`
- add a max-retry exit to the mbsync sync loop so the container does not silently loop on persistent failure
- parameterize Claude model name via `CLAUDE_MODEL` env var with a pinned default instead of hardcoding in `intelligence.py`
- validate ISO 8601 format on date filter inputs in `mcp-server/src/lib/sqlite.py` before string comparison

Definition of done:
- database writes are atomic; a partial failure rolls back cleanly with no inconsistent state left behind
- TLS is validated on all live service connections
- credential values do not appear in error logs or tracebacks
- the mbsync sync loop exits and triggers a container restart after repeated consecutive failures

## Near-Term Backlog

### mbsync improvements
- investigate IMAP IDLE as a replacement for the sync sleep loop
- confirm current Patterns and expunge behavior remain safe
- keep sync strictly pull-only

### MCP feature completion
- add attachment download support once the read-only action path is defined
- verify action tools respect read-only guardrails

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

### Bridge build and operations
- consolidate `BRIDGE_VERSION` to a single source of truth (`.env.example`) and remove the duplicate hardcoded defaults from `docker-compose.yml` and `bridge/Dockerfile` so version bumps only require one change
- parameterize the Go toolchain version as an `ARG` in `bridge/Dockerfile` alongside `BRIDGE_VERSION` for consistency
- pin the `golang` builder image in `bridge/Dockerfile` to a digest in addition to its version tag; the runtime was pinned but the builder was not, leaving the build toolchain open to silent upstream changes
- pin all apt packages in `bridge/Dockerfile` to exact versions in both the builder stage (`git`, `make`, `gcc`, `pkg-config`, `libsecret-1-dev`, `libfido2-dev`, `libcbor-dev`) and the runtime stage (`bash`, `pass`, `gnupg2`, `libfido2-1`, `libsecret-1-0`) so a Debian package update cannot silently change the build or runtime environment between identical `BRIDGE_VERSION` builds
- build the Bridge binary with stripped debug symbols by passing `-ldflags="-s -w"` via `GOFLAGS` or an explicit build override in `bridge/Dockerfile`; this reduces binary size and removes embedded source file paths from the shipped binary
- add OCI image labels (`org.opencontainers.image.source`, `org.opencontainers.image.version`, `org.opencontainers.image.revision`) to `bridge/Dockerfile` so every built image carries build provenance that can be traced back to the exact Bridge release and Dockerfile revision
- verify the Proton release tag signature before building: import Proton's published signing key into the builder stage, hardcode the expected fingerprint, and run `git verify-tag ${BRIDGE_VERSION}` after cloning so a tampered or substituted tag fails the build
- add a `check-secrets` pre-flight target to the Makefile that validates `.secrets/bridge_pass.txt` has `600` permissions before `make up` proceeds
- add a pre-flight check to `make first-run` that detects an existing `bridge-data` volume and warns the operator before proceeding, since a populated volume means Bridge is already logged in and the interactive CLI will not behave as expected
- wrap all `gpg` and `pass` calls in `bridge/entrypoint.sh` with `timeout` so a stalled GPG agent or hung pass operation cannot cause the container to hang indefinitely at startup; apply to the `--list-keys`, `--quick-gen-key`, and `pass init` invocations
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to add `bash --version` to the binary checks so the healthcheck dependency is verified
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify that the `bridge --version` output contains `BRIDGE_VERSION` so a version mismatch between the built binary and the configured release is caught without requiring a live Bridge session
- add `GIT_TERMINAL_PROMPT=0` before the `git clone` in `scripts/bridge-patch-drift.sh` to prevent interactive credential prompts from hanging CI when the upstream repository is unreachable, and wrap the clone with a `timeout` so a slow or stalled network connection does not consume the full CI job budget
- add a `setup-go` step with a pinned Go version to the `bridge-patch-drift` job in `.github/workflows/bridge.yml`; the drift check calls `patch-source.sh` which runs `go build`, so it depends on Go being present at the correct version — currently it relies on whatever Go ships with the `ubuntu-latest` runner, which can silently diverge from Bridge's minimum required version
- add `timeout-minutes` to both the `bridge-patch-drift` and `bridge-smoke` jobs in `.github/workflows/bridge.yml` so a hung git clone or long-running Docker build does not consume the full GitHub Actions 6-hour job limit
- pin `actions/checkout` to a commit SHA in `.github/workflows/bridge.yml` instead of a mutable version tag to eliminate supply-chain risk from tag mutation
- add a Trivy Go vulnerability scan targeting the Bridge Go module graph (`bridge/go.sum` or the built image) in `.github/workflows/security.yml`; the current Trivy scan only covers Python services and leaves Bridge Go dependencies unscanned for CVEs
- fix `bridge/patch-source.sh` to show Go compiler output on post-patch compilation failure; the current `go build ... >/dev/null 2>&1` discards all output so when compilation fails the operator sees only "post-patch compilation failed" with no indication of the actual error
- document the `bridge-v3` vault path as version-baked in `bridge/entrypoint.sh`; if a future Bridge major version stores the vault at `bridge-v4/vault.enc`, account detection silently fails and the container drops to the interactive CLI every restart with no explanation — add a logged warning when the expected vault path does not exist and the operator should confirm the path is correct for the current Bridge version
- add a Makefile message to `make first-run` warning operators that `logging: driver: none` is active for this session, so any startup failure will not appear in `docker logs`; direct operators to re-run `make first-run` to see live terminal output if the container exits unexpectedly
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify directory ownership (`stat -c "%u %g"`) on `/data/config`, `/data/local`, `/data/cache`, `/data/gnupg`, and `/data/pass` in addition to permissions; a Dockerfile change that breaks the `chown -R bridge:bridge` step would currently pass the smoke test undetected
- update `.hadolint.yaml` to remove or scope the `DL3008` suppression once apt package version pinning is implemented for Bridge; the current global suppression with the comment "impractical on Debian stable" will prevent the linter from enforcing the pinning after it is added, defeating the purpose of the change
- update `SECURITY.md` to accurately describe the two-network topology (`bridge-net` for ProtonBridge and mbsync, `app-net` for indexer, Ollama, and mcp-server); the current text says "All containers run on an isolated Docker bridge network" which no longer reflects the implemented split-network design
- replace `echo -n 'bridge-generated-pass'` in `docs/setup.md` with `printf '%s' 'bridge-generated-pass'` to avoid portability issues across sh and zsh implementations where `-n` may not suppress the trailing newline
- add a "Bridge Build and Patching" section to `docs/architecture.md` explaining the two source-level patches applied during the Docker build (bind address changed from `127.0.0.1` to `0.0.0.0`, TLS SAN extended to include `protonmail-bridge` and `localhost`), why they exist, what `patch-source.sh` and `bridge-patch-drift.sh` do, and what operators should expect when running `make bridge-upgrade-check` during a version bump
- clarify `docs/setup.md` cert regeneration instructions to make explicit that removing `vault.enc` triggers a full re-authentication, not just a cert refresh; consider adding a `make refresh-cert` target with a clear warning

### Hardening and observability
- add resource limits (`memory`, `cpus`, `pids_limit`) to all Compose services that currently lack them (especially `ollama` and `indexer`)
- add explicit log rotation to the `protonmail-bridge` service in `docker-compose.yml` (`json-file` driver with `max-size: 10m` and `max-file: 3`) so Bridge logs cannot grow unbounded during long-running deployments
- add `HEALTHCHECK` to `indexer/Dockerfile` so stalled indexing is detectable
- pin `python` and `uv` base images to digest in addition to version tag across `indexer/` and `mcp-server/`
- add `bandit -r src/` to pre-commit for Python security scanning
- add `validate-env.sh` pre-flight check that verifies required `.env` fields are present before `make up`
- add `docs/troubleshooting.md` covering common failure patterns: Ollama not ready, cert extraction failure, sync stalled, schema migration

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
- extend the Bridge smoke-test path beyond build/runtime validation to cover live auth, cert, and IMAP readiness once guarded live Bridge CI exists
- document and test backup, restore, and rollback handling for the `bridge-data` volume so Bridge upgrades and recovery are safer
- add better operator tooling such as a `make bridge-status`-style diagnostic path for auth state, Gluon sync state, recent logs, and IMAP readiness
- keep validating the new read-only Bridge rootfs baseline as Bridge versions change
- audit Bridge and `mbsync` runtime packages regularly and remove unused tools or libraries once verified unnecessary
- evaluate a custom seccomp profile for the Bridge container that restricts syscalls to only what Bridge requires; document the profile and gate it behind a tested list of allowed calls
- document the empty GPG passphrase design constraint in `SECURITY.md` so operators understand that container shell access is equivalent to credential access; note that seccomp, AppArmor, and volume isolation are the primary mitigations
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

### 1. Read-only policy surface
Need final decision on whether read-only mode blocks:
- only action tools
- action tools plus any SMTP send path
- all mutating paths including draft creation and move/flag operations

### 2. IMAP strategy
Need decision on whether polling remains acceptable or whether IMAP IDLE is worth the added complexity.

### 3. Live Bridge integration lane
Need final decision on the long-term home for live Bridge smoke tests:
- GitHub-hosted manual/nightly workflow using a dedicated no-2FA Proton test account
- or a trusted self-hosted runner with persistent authenticated Bridge state

## Recently Completed

- Bridge entrypoint hardened: XDG paths unset before export, explicit error guards on GPG key generation and `pass init`, vault existence cross-checked against GPG key presence so a broken vault is caught at startup
- Bridge `patch-source.sh`: post-patch compilation check added so a malformed patch fails the build rather than producing a broken binary
- Bridge `Dockerfile`: `bash` added as an explicit runtime dependency; `debian:bookworm-slim` runtime base pinned to digest
- Bridge `docker-compose.yml`: `start_period` increased from `15s` to `45s` to allow GPG initialization to complete before the healthcheck fires
- Bridge `Makefile`: `make update` now requires `bridge-upgrade-check` to pass before rebuilding
- two-network Docker topology implemented: `bridge-net` isolates Bridge/mbsync from `app-net` used by indexer and mcp-server
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

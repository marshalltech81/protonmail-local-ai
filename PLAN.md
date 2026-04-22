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
- `mbsync` now validates Bridge auth prerequisites, bounds startup/sync failure loops, captures cert extraction diagnostics, exposes a healthcheck, and allows current TLS negotiation defaults
- the Bridge bootstrap now time-bounds `gpg`/`pass`, repairs missing `.gpg-id`, and the indexer now has a hardened runtime profile plus a heartbeat healthcheck
- `mcp-server` cloud error paths now redact secrets, Claude model selection is configurable via `CLAUDE_MODEL`, and insecure live Bridge transport paths fail closed unless a cert-pinned backend is explicitly configured
- the Bridge build now uses a shared patch helper plus a dedicated patch-drift check and build/runtime smoke-test path for version bumps
- the indexer now ships an opt-in deletion reconciler: mbsync `T`-flag detection (startup sweep + watchdog `on_moved`), grace-window reaping with thread rebuild, and a mass-delete safety brake; `mbsync` keeps `Expunge None` unchanged
- schema v4 rebuilds `threads_fts` with `contentless_delete=1` and tracks `threads.fts_rowid`; this fixes stale-token accumulation on thread updates and the MCP keyword-search JOIN that was returning empty results due to UNINDEXED-column null reads
- `upsert_thread` now merges `message_ids`, `participants`, `has_attachments`, and `date_first` with the existing row rather than replacing them from the single newly-arrived message; RFC 2822 dates normalize to UTC-aware so thread sorting no longer mixes aware and naive datetimes
- `mcp-server` opens SQLite via `file:{path}?mode=ro` URI and the sqlite volume is mounted writable so WAL reads work; the indexer `Database` wraps its shared connection in a per-instance `RLock` so watchdog-thread writes cannot interleave with reconciler-loop writes
- indexer `on_moved` now indexes the destination of Maildir rename events (standard delivery writes tmp → new/cur) so new mail no longer has to wait for a restart to appear in the index; the initial-index scan refreshes the heartbeat every 25 messages so large mailboxes do not exceed the 90-second healthcheck window
- intelligence tools (`ask_mailbox`, `summarize_thread`, `extract_from_emails`) now feed the LLM the accumulated thread body text (bounded per thread) instead of the 200-character snippet; keyword search sanitizes user input into safe FTS5 phrases with a LIKE fallback, and filtered searches oversample raw candidates to preserve recall
- `make pull-models` self-starts the ollama container and waits for readiness; `scripts/validate-env.sh` uses a portable `stat` helper so `make up` works on macOS; `make status` now reports real index counts instead of a hardcoded `{"status": "ok"}`

Known limitations:

- initial sync may take a long time on large mailboxes
- attachments are not indexed yet
- per-message live retrieval and mail-changing actions are disabled in the default deployment until a safe action backend is implemented
- test coverage is incomplete
- some MCP action features are incomplete
- `list_threads(filter_type=...)` does not yet match the documented interface
- the new hardened first-run/sync path still needs real end-to-end validation against a live Bridge session
- deletion reconciliation is opt-in (`INDEXER_DELETION_ENABLED=true`) and needs real-world validation; `INDEXER_UNLINK_ON_REAP=true` only removes the `.eml` file when Maildir is mounted read-write (default mount is read-only)

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

### 2. Validate intelligence fidelity end-to-end

Goal:
- confirm the body-text-based RAG path produces noticeably better answers and summaries than the previous snippet-only behavior under real mailbox conditions

Tasks:
- exercise `ask_mailbox`, `summarize_thread`, and `extract_from_emails` against a live mailbox and compare against the prior snippet-only behavior
- tune `PER_THREAD_CHAR_BUDGET` if local LLM context limits are hit in practice
- add regression tests that would catch a reversion to snippet-only prompts

Definition of done:
- answers and summaries consistently reflect whole-thread context in practice, not just in unit tests
- structured extraction is based on full accumulated thread bodies rather than the latest snippet

### 3. Expand test coverage

Goal:
- make parser, threader, database, and MCP behavior safer to change

Tasks:
- keep parser/threader/database tests passing
- add tests for `list_threads(filter_type=...)` behavior
- add tests for read-only action-tool gating, non-registration, and user-facing failure paths
- add tests for local-only retrieval and system-status behavior
- add integration coverage for indexer watchdog behavior using mocks
- add test for circular `In-Reply-To` references (document expected behavior even if not explicitly handled)
- expand `mcp-server` coverage beyond `src/lib` (tool handlers, service bootstrap) and then widen the coverage scope accordingly

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

### Bridge release monitoring
- add a scheduled GitHub Actions workflow that queries the proton-bridge GitHub releases API and compares the latest release tag to `BRIDGE_VERSION` in `.env.example`; when the repo is behind a newer release, open a GitHub issue or post a workflow summary so the operator is notified to evaluate the release and run `make bridge-upgrade-check`; Dependabot monitors Docker base images but has no mechanism to track the upstream Bridge release version, leaving version drift entirely manual

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
- pin all apt packages in `bridge/Dockerfile` to exact versions in both the builder stage (`git`, `make`, `gcc`, `pkg-config`, `libsecret-1-dev`, `libfido2-dev`, `libcbor-dev`) and the runtime stage (`bash`, `pass`, `gnupg2`, `libfido2-1`, `libsecret-1-0`) so a Debian package update cannot silently change the build or runtime environment between identical `BRIDGE_VERSION` builds
- add OCI image labels (`org.opencontainers.image.source`, `org.opencontainers.image.version`, `org.opencontainers.image.revision`) to `bridge/Dockerfile` so every built image carries build provenance that can be traced back to the exact Bridge release and Dockerfile revision
- verify the Proton release tag signature before building: import Proton's published signing key into the builder stage, hardcode the expected fingerprint, and run `git verify-tag ${BRIDGE_VERSION}` after cloning so a tampered or substituted tag fails the build
- add a pre-flight check to `make first-run` that detects an existing `bridge-data` volume and warns the operator before proceeding, since a populated volume means Bridge is already logged in and the interactive CLI will not behave as expected
- update the `make clean` printed warning message to explicitly mention that Bridge credentials are deleted and `make first-run` will be required to re-authenticate; the current `@echo "WARNING: This will delete all containers, volumes, and your email index."` mentions the email index but not Bridge credentials, while the Makefile comment above the target correctly calls out both; an operator who reads only the printed warning may not anticipate needing to re-authenticate after `make clean`
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to add `bash --version` to the binary checks so the healthcheck dependency is verified
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify that the `bridge --version` output contains `BRIDGE_VERSION` so a version mismatch between the built binary and the configured release is caught without requiring a live Bridge session
- add a `setup-go` step with a pinned Go version to the `bridge-patch-drift` job in `.github/workflows/bridge.yml`; the drift check calls `patch-source.sh` which runs `go build`, so it depends on Go being present at the correct version — currently it relies on whatever Go ships with the `ubuntu-latest` runner, which can silently diverge from Bridge's minimum required version
- add `timeout-minutes` to both the `bridge-patch-drift` and `bridge-smoke` jobs in `.github/workflows/bridge.yml` so a hung git clone or long-running Docker build does not consume the full GitHub Actions 6-hour job limit
- add `timeout-minutes` to the `build-images` job in `.github/workflows/docker.yml` so a stalled Bridge git clone or long Go compile during the full `docker compose build` does not consume the full GitHub Actions 6-hour limit; also add path filters to the `docker.yml` workflow trigger so a Python-only change does not unnecessarily rebuild the Bridge image (the Bridge build clones from GitHub and compiles from source and is significantly slower than other service builds)
- update `.github/workflows/lint.yml` to remove `DL3008` from the `hadolint` job's `ignore: DL3008,DL3059` action parameter when apt package version pinning is implemented for Bridge; the CI hadolint action has its own hardcoded ignore list separate from `.hadolint.yaml` — if only `.hadolint.yaml` is updated, CI will still silently suppress the "pin apt package versions" rule and the pinning constraint will not be enforced in CI
- update `.pre-commit-config.yaml` to remove `--ignore DL3008` from the `hadolint-docker` hook args when apt package version pinning is implemented; the pre-commit hook has its own arg list separate from both `.hadolint.yaml` and `.github/workflows/lint.yml` — it is a third location where the suppression lives, and skipping it means local pre-commit still silently allows unpinned apt installs even after the other two locations are updated
- pin `actions/checkout` to a commit SHA in `.github/workflows/bridge.yml` instead of a mutable version tag to eliminate supply-chain risk from tag mutation
- add a Trivy Go vulnerability scan targeting the Bridge Go module graph (`bridge/go.sum` or the built image) in `.github/workflows/security.yml`; the current Trivy scan only covers Python services and leaves Bridge Go dependencies unscanned for CVEs
- fix the `\t\t` portability bug in `bridge/patch-source.sh`: the sed replacement string contains literal backslash-t characters (confirmed by inspecting raw bytes); GNU sed interprets `\t` as a tab in replacements but BSD sed on macOS does not, producing invalid Go source with literal `\t\t` characters and causing `go build` to fail with a syntax error; the script's portability comment explicitly says it should work on macOS — fix using actual tab characters or `$'\t'` in the replacement expression
- document the `bridge-v3` vault path as version-baked in `bridge/entrypoint.sh`; if a future Bridge major version stores the vault at `bridge-v4/vault.enc`, account detection silently fails and the container drops to the interactive CLI every restart with no explanation — add a logged warning when the expected vault path does not exist and the operator should confirm the path is correct for the current Bridge version
- add a Makefile message to `make first-run` warning operators that `logging: driver: none` is active for this session, so any startup failure will not appear in `docker logs`; direct operators to re-run `make first-run` to see live terminal output if the container exits unexpectedly
- reference `make init-secrets` in `README.md` and `docs/setup.md` setup steps (target already exists and is wired in as a `make first-run` dependency, but the setup docs don't yet mention it)
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify directory ownership (`stat -c "%u %g"`) on `/data/config`, `/data/local`, `/data/cache`, `/data/gnupg`, and `/data/pass` in addition to permissions; a Dockerfile change that breaks the `chown -R bridge:bridge` step would currently pass the smoke test undetected
- update `.hadolint.yaml` to remove or scope the `DL3008` suppression once apt package version pinning is implemented for Bridge; the current global suppression with the comment "impractical on Debian stable" will prevent the linter from enforcing the pinning after it is added, defeating the purpose of the change
- replace `echo -n 'bridge-generated-pass'` in `docs/setup.md` with `printf '%s' 'bridge-generated-pass'` to avoid portability issues across sh and zsh implementations where `-n` may not suppress the trailing newline
- add a "Bridge Build and Patching" section to `docs/architecture.md` explaining the two source-level patches applied during the Docker build (bind address changed from `127.0.0.1` to `0.0.0.0`, TLS SAN extended to include `protonmail-bridge` and `localhost`), why they exist, what `patch-source.sh` and `bridge-patch-drift.sh` do, and what operators should expect when running `make bridge-upgrade-check` during a version bump
- update the Docker Volumes table in `docs/architecture.md` for `bridge-data`: the current description "Bridge credentials, GPG key, config — Yes, losing this requires re-login" is incomplete; the volume also holds the Gluon IMAP cache (`/data/local`), and losing it forces not only re-authentication but also a full mailbox re-download from Proton that can take hours on large mailboxes; the backup recommendation should clarify this so operators understand the full recovery cost and the distinction between essential auth material and the optional-but-expensive-to-rebuild Gluon cache
- update the org-level `marshalltech81/.github` `CONTRIBUTING.md` to add `mbsync/` paths (specifically `entrypoint.sh`, `mbsyncrc.template`, cert handling, and STARTTLS config) as a trigger for running `make bridge-upgrade-check`; changes to the Bridge ↔ mbsync interface directly affect Bridge connectivity but are not currently listed alongside `bridge/` and `BRIDGE_VERSION` in the recommended checks
- correct `AGENTS.md` line 118: "the cert is not read from plain files on disk" is inaccurate — at runtime, mbsync extracts and writes the Bridge TLS cert to `/tmp/mbsync/bridge-cert.pem` (a tmpfs file); the accurate statement is that the cert is not baked into any image or persisted in a volume, but IS written to a tmpfs file by mbsync's entrypoint on every container start
- add automated tests for `bridge/entrypoint.sh` shell logic using BATS (Bash Automated Testing System) or a comparable shell test framework; the GPG bootstrap path, the vault-exists-but-key-missing error path, the normal noninteractive launch, and the first-run interactive path all have no automated coverage; regressions in these paths currently require a full manual first-run cycle to discover
- document Gluon IMAP cache compatibility in `docs/setup.md` for the Bridge version upgrade procedure (`make update`); the Gluon cache in `bridge-data:/data/local` may be incompatible with a new Bridge version, requiring Bridge to re-download the entire mailbox from Proton's API (potentially hours); document whether Bridge handles Gluon cache migration automatically, what log patterns indicate a re-sync is in progress, and what the operator should do if Bridge fails to migrate the cache
- add operator guidance to `docs/setup.md` for a failed `make bridge-upgrade-check`; currently the output shows a patch drift error but there is no documented next step — operators should be told not to proceed with `make update`, to check whether Proton changed the surrounding source layout, and to wait for a repo patch update rather than attempting manual intervention
- add a Gluon cache corruption recovery procedure to `docs/setup.md`; if Bridge is force-killed during heavy Gluon sync and the Gluon database is corrupted, Bridge may fail to start or serve IMAP; the targeted recovery is to delete only `/data/local/protonmail/bridge-v3/` while preserving auth material in `/data/config`, `/data/gnupg`, and `/data/pass` — this is distinct from the full volume wipe and re-authentication flow documented for GPG/pass corruption
- clarify `docs/setup.md` cert regeneration instructions to make explicit that removing `vault.enc` triggers a full re-authentication, not just a cert refresh; consider adding a `make refresh-cert` target with a clear warning
- add `# syntax=docker/dockerfile:1` as the first line of `bridge/Dockerfile`; without a parser directive the BuildKit Dockerfile frontend version is unpinned and different Docker Engine or BuildKit versions may parse the same Dockerfile differently, producing subtly different images; this matters most for the multi-stage Bridge build where `--from` resolution and layer caching rely on stable parse behavior
### Hardening and observability
- add resource limits (`memory`, `cpus`, `pids_limit`) to all Compose services that currently lack them; `protonmail-bridge` holds live Proton credentials in memory and is the highest-priority target — a `mem_limit` prevents a runaway or exploited process from exhausting host memory; `ollama` and `indexer` are also missing limits
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
- document and test backup, restore, and rollback handling for the `bridge-data` volume so Bridge upgrades and recovery are safer; distinguish essential from optional subdirectories: `/data/config`, `/data/gnupg`, and `/data/pass` must all be included (vault.enc is unreadable without the GPG key stored in gnupg/), while `/data/local` (Gluon IMAP cache) and `/data/cache` can be omitted since Bridge rebuilds them from Proton's API; a backup that omits gnupg/ is silently useless
- add better operator tooling such as a `make bridge-status`-style diagnostic path for auth state, Gluon sync state, recent logs, and IMAP readiness
- keep validating the new read-only Bridge rootfs baseline as Bridge versions change
- audit Bridge and `mbsync` runtime packages regularly and remove unused tools or libraries once verified unnecessary
- evaluate a custom seccomp profile for the Bridge container that restricts syscalls to only what Bridge requires; document the profile and gate it behind a tested list of allowed calls
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

- test coverage wired into CI: `pytest-cov` added to both services with a 90% fail-under floor; coverage scope is `indexer/src` (omitting `src/main.py`) and `mcp-server/src/lib`; coverage XML uploaded as an artifact per job
- `mcp-server` unit-test suite added covering `security`, `sqlite` (hybrid/keyword/semantic/RRF/filters/validators), `ollama` (async httpx MockTransport), and `imap` (TLS context, fail-closed refusal, async aioimaplib mocks, smtplib mocks, RFC822 parser)
- fixed a latent bug in `mcp-server/src/lib/security.py` where `redact_sensitive_text` raised `re.PatternError` on every call because the `sk-ant-...` pattern had no capture group but the substitution template used `\1`
- `bandit` static analysis added as a dedicated CI job scanning `indexer/src` and `mcp-server/src` at `-ll` (medium+) severity
- Bridge CI jobs now invoke `make bridge-patch-check` and `make bridge-smoke` directly so the `make bridge-upgrade-check` gate documented for local version bumps has the same shape in CI; `timeout-minutes` added to both jobs
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

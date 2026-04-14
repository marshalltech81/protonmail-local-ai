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
- restore TLS validation in `mcp-server/src/lib/imap.py`: set `verify_mode = ssl.CERT_REQUIRED` and `check_hostname = True` in `send_email`; also add STARTTLS negotiation to `IMAPClient._connect` — the method uses `aioimaplib.IMAP4` (plaintext) without initiating STARTTLS, meaning `client.login()` credentials would be sent unencrypted if this code path were ever active; Bridge listens on port 1143 with STARTTLS required, so the fix is to call `await client.starttls()` with a proper SSL context before `login`, or switch to `aioimaplib.IMAP4_SSL` on a dedicated implicit-TLS port
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
- add a max-retry count (or cumulative timeout) to the `until nc -z` Bridge IMAP wait loop in `mbsync/entrypoint.sh`; the loop currently runs indefinitely so if Bridge never becomes reachable the mbsync container silently appears "Up" while stuck — this is a distinct gap from the sync loop max-retry in Active Priority #6 which covers the `while true` poll loop
- capture `openssl s_client` stderr to a temporary file during Bridge cert extraction in `mbsync/entrypoint.sh` and print it only on failure; the current `2>/dev/null` discards all TLS and network error output, so when cert extraction fails the operator sees "cert extraction failed" with no indication of whether the cause was a network error, TLS handshake failure, or Bridge not responding
- add a `timeout` wrapper around the `openssl s_client` cert extraction call in `mbsync/entrypoint.sh`; the `until nc -z` loop confirms the port is open but not that Bridge is ready to complete a STARTTLS handshake — if Bridge is still loading and not responding to IMAP, the openssl call hangs indefinitely; this is a distinct gap from the stderr capture item above, which is about diagnostics rather than preventing an infinite hang
- remove the `SSLVersions TLSv1.2` restriction from `mbsync/mbsyncrc.template` or extend it to include `TLSv1.3`; Bridge's Go TLS stack supports TLS 1.3 by default and the current restriction unnecessarily prevents using the stronger protocol
- add a pre-flight check to `mbsync/entrypoint.sh` that validates `BRIDGE_USER` is non-empty before generating the mbsyncrc from the template; an empty `BRIDGE_USER` produces a syntactically valid config that silently fails IMAP authentication with an unhelpful "LOGIN failed" error — fail fast with a clear message so the operator knows exactly what to fix
- add a pre-flight check to `mbsync/entrypoint.sh` that verifies `/run/secrets/bridge_pass` exists and is non-empty before proceeding; if Docker secrets are misconfigured the config is generated correctly but `PassCmd` returns empty output and mbsync fails with a confusing auth error; an explicit check at startup surfaces the configuration problem immediately
- add a `HEALTHCHECK` to the `mbsync` service (either in `mbsync/Dockerfile` or the `docker-compose.yml` service definition) so a stalled or crashed sync loop is detectable by Docker's health monitoring; the container can appear "Up" while the sync loop has silently failed, and there is currently no signal that distinguishes an actively syncing container from one that has stopped producing output

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
- pin the `golang` builder image in `bridge/Dockerfile` to a digest in addition to its version tag; the runtime was pinned but the builder was not, leaving the build toolchain open to silent upstream changes; note that digest-pinning disables Dependabot's ability to auto-update the image, so the bridge version bump workflow (`make update` and `make bridge-upgrade-check`) must also cover refreshing the builder digest
- pin all apt packages in `bridge/Dockerfile` to exact versions in both the builder stage (`git`, `make`, `gcc`, `pkg-config`, `libsecret-1-dev`, `libfido2-dev`, `libcbor-dev`) and the runtime stage (`bash`, `pass`, `gnupg2`, `libfido2-1`, `libsecret-1-0`) so a Debian package update cannot silently change the build or runtime environment between identical `BRIDGE_VERSION` builds
- build the Bridge binary with stripped debug symbols by passing `-ldflags="-s -w"` via `GOFLAGS` or an explicit build override in `bridge/Dockerfile`; this reduces binary size and removes embedded source file paths from the shipped binary; add alongside the already-implemented `-trimpath` flag
- add OCI image labels (`org.opencontainers.image.source`, `org.opencontainers.image.version`, `org.opencontainers.image.revision`) to `bridge/Dockerfile` so every built image carries build provenance that can be traced back to the exact Bridge release and Dockerfile revision
- verify the Proton release tag signature before building: import Proton's published signing key into the builder stage, hardcode the expected fingerprint, and run `git verify-tag ${BRIDGE_VERSION}` after cloning so a tampered or substituted tag fails the build
- add a `check-secrets` pre-flight target to the Makefile that validates `.secrets/bridge_pass.txt` has `600` permissions before `make up` proceeds
- add a pre-flight check to `make first-run` that detects an existing `bridge-data` volume and warns the operator before proceeding, since a populated volume means Bridge is already logged in and the interactive CLI will not behave as expected
- update the `make clean` printed warning message to explicitly mention that Bridge credentials are deleted and `make first-run` will be required to re-authenticate; the current `@echo "WARNING: This will delete all containers, volumes, and your email index."` mentions the email index but not Bridge credentials, while the Makefile comment above the target correctly calls out both; an operator who reads only the printed warning may not anticipate needing to re-authenticate after `make clean`
- wrap all `gpg` and `pass` calls in `bridge/entrypoint.sh` with `timeout` so a stalled GPG agent or hung pass operation cannot cause the container to hang indefinitely at startup; apply to the `--list-keys`, `--quick-gen-key`, and `pass init` invocations
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to add `bash --version` to the binary checks so the healthcheck dependency is verified
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify that the `bridge --version` output contains `BRIDGE_VERSION` so a version mismatch between the built binary and the configured release is caught without requiring a live Bridge session
- add `GIT_TERMINAL_PROMPT=0` before the `git clone` in `scripts/bridge-patch-drift.sh` to prevent interactive credential prompts from hanging CI when the upstream repository is unreachable, and wrap the clone with a `timeout` so a slow or stalled network connection does not consume the full CI job budget
- strip trailing carriage returns (`\r`) from the version string extracted by `resolve_bridge_version` in `scripts/bridge-patch-drift.sh`; the `grep | cut` pipeline does not strip Windows-style CRLF line endings from `.env` or `.env.example`, so a contributor who edits these files on Windows produces a version string with a trailing `\r` that corrupts the `git clone --branch` argument with an obscure "remote ref not found" error rather than a clear version-parse failure
- add a `setup-go` step with a pinned Go version to the `bridge-patch-drift` job in `.github/workflows/bridge.yml`; the drift check calls `patch-source.sh` which runs `go build`, so it depends on Go being present at the correct version — currently it relies on whatever Go ships with the `ubuntu-latest` runner, which can silently diverge from Bridge's minimum required version
- add `timeout-minutes` to both the `bridge-patch-drift` and `bridge-smoke` jobs in `.github/workflows/bridge.yml` so a hung git clone or long-running Docker build does not consume the full GitHub Actions 6-hour job limit
- add `timeout-minutes` to the `build-images` job in `.github/workflows/docker.yml` so a stalled Bridge git clone or long Go compile during the full `docker compose build` does not consume the full GitHub Actions 6-hour limit; also add path filters to the `docker.yml` workflow trigger so a Python-only change does not unnecessarily rebuild the Bridge image (the Bridge build clones from GitHub and compiles from source and is significantly slower than other service builds)
- update `.github/workflows/lint.yml` to remove `DL3008` from the `hadolint` job's `ignore: DL3008,DL3059` action parameter when apt package version pinning is implemented for Bridge; the CI hadolint action has its own hardcoded ignore list separate from `.hadolint.yaml` — if only `.hadolint.yaml` is updated, CI will still silently suppress the "pin apt package versions" rule and the pinning constraint will not be enforced in CI
- update `.pre-commit-config.yaml` to remove `--ignore DL3008` from the `hadolint-docker` hook args when apt package version pinning is implemented; the pre-commit hook has its own arg list separate from both `.hadolint.yaml` and `.github/workflows/lint.yml` — it is a third location where the suppression lives, and skipping it means local pre-commit still silently allows unpinned apt installs even after the other two locations are updated
- pin `actions/checkout` to a commit SHA in `.github/workflows/bridge.yml` instead of a mutable version tag to eliminate supply-chain risk from tag mutation
- add a Trivy Go vulnerability scan targeting the Bridge Go module graph (`bridge/go.sum` or the built image) in `.github/workflows/security.yml`; the current Trivy scan only covers Python services and leaves Bridge Go dependencies unscanned for CVEs
- fix `bridge/patch-source.sh` to show Go compiler output on post-patch compilation failure; the current `go build ... >/dev/null 2>&1` discards all output so when compilation fails the operator sees only "post-patch compilation failed" with no indication of the actual error
- fix the `\t\t` portability bug in `bridge/patch-source.sh`: the sed replacement string contains literal backslash-t characters (confirmed by inspecting raw bytes); GNU sed interprets `\t` as a tab in replacements but BSD sed on macOS does not, producing invalid Go source with literal `\t\t` characters and causing `go build` to fail with a syntax error; the script's portability comment explicitly says it should work on macOS — fix using actual tab characters or `$'\t'` in the replacement expression
- document the `bridge-v3` vault path as version-baked in `bridge/entrypoint.sh`; if a future Bridge major version stores the vault at `bridge-v4/vault.enc`, account detection silently fails and the container drops to the interactive CLI every restart with no explanation — add a logged warning when the expected vault path does not exist and the operator should confirm the path is correct for the current Bridge version
- add a Makefile message to `make first-run` warning operators that `logging: driver: none` is active for this session, so any startup failure will not appear in `docker logs`; direct operators to re-run `make first-run` to see live terminal output if the container exits unexpectedly
- ~~fix the first-run secret file pre-flight~~ — implemented: `make init-secrets` target creates placeholder files for `bridge_pass.txt` and `anthropic_api_key.txt` if absent; `make first-run` now depends on `init-secrets`; `README.md` and `docs/setup.md` still need to reference `make init-secrets` in the setup steps
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify directory ownership (`stat -c "%u %g"`) on `/data/config`, `/data/local`, `/data/cache`, `/data/gnupg`, and `/data/pass` in addition to permissions; a Dockerfile change that breaks the `chown -R bridge:bridge` step would currently pass the smoke test undetected
- update `.hadolint.yaml` to remove or scope the `DL3008` suppression once apt package version pinning is implemented for Bridge; the current global suppression with the comment "impractical on Debian stable" will prevent the linter from enforcing the pinning after it is added, defeating the purpose of the change
- update `SECURITY.md` to accurately describe the two-network topology (`bridge-net` for ProtonBridge and mbsync, `app-net` for indexer, Ollama, and mcp-server); the current text says "All containers run on an isolated Docker bridge network" which no longer reflects the implemented split-network design
- replace `echo -n 'bridge-generated-pass'` in `docs/setup.md` with `printf '%s' 'bridge-generated-pass'` to avoid portability issues across sh and zsh implementations where `-n` may not suppress the trailing newline
- add a "Bridge Build and Patching" section to `docs/architecture.md` explaining the two source-level patches applied during the Docker build (bind address changed from `127.0.0.1` to `0.0.0.0`, TLS SAN extended to include `protonmail-bridge` and `localhost`), why they exist, what `patch-source.sh` and `bridge-patch-drift.sh` do, and what operators should expect when running `make bridge-upgrade-check` during a version bump
- update the Docker Volumes table in `docs/architecture.md` for `bridge-data`: the current description "Bridge credentials, GPG key, config — Yes, losing this requires re-login" is incomplete; the volume also holds the Gluon IMAP cache (`/data/local`), and losing it forces not only re-authentication but also a full mailbox re-download from Proton that can take hours on large mailboxes; the backup recommendation should clarify this so operators understand the full recovery cost and the distinction between essential auth material and the optional-but-expensive-to-rebuild Gluon cache
- update `CONTRIBUTING.md` to add `mbsync/` paths (specifically `entrypoint.sh`, `mbsyncrc.template`, cert handling, and STARTTLS config) as a trigger for running `make bridge-upgrade-check`; changes to the Bridge ↔ mbsync interface directly affect Bridge connectivity but are not currently listed alongside `bridge/` and `BRIDGE_VERSION` in the recommended checks
- correct `AGENTS.md` line 118: "the cert is not read from plain files on disk" is inaccurate — at runtime, mbsync extracts and writes the Bridge TLS cert to `/tmp/mbsync/bridge-cert.pem` (a tmpfs file); the accurate statement is that the cert is not baked into any image or persisted in a volume, but IS written to a tmpfs file by mbsync's entrypoint on every container start
- add automated tests for `bridge/entrypoint.sh` shell logic using BATS (Bash Automated Testing System) or a comparable shell test framework; the GPG bootstrap path, the vault-exists-but-key-missing error path, the normal noninteractive launch, and the first-run interactive path all have no automated coverage; regressions in these paths currently require a full manual first-run cycle to discover
- document Gluon IMAP cache compatibility in `docs/setup.md` for the Bridge version upgrade procedure (`make update`); the Gluon cache in `bridge-data:/data/local` may be incompatible with a new Bridge version, requiring Bridge to re-download the entire mailbox from Proton's API (potentially hours); document whether Bridge handles Gluon cache migration automatically, what log patterns indicate a re-sync is in progress, and what the operator should do if Bridge fails to migrate the cache
- add operator guidance to `docs/setup.md` for a failed `make bridge-upgrade-check`; currently the output shows a patch drift error but there is no documented next step — operators should be told not to proceed with `make update`, to check whether Proton changed the surrounding source layout, and to wait for a repo patch update rather than attempting manual intervention
- add a Gluon cache corruption recovery procedure to `docs/setup.md`; if Bridge is force-killed during heavy Gluon sync and the Gluon database is corrupted, Bridge may fail to start or serve IMAP; the targeted recovery is to delete only `/data/local/protonmail/bridge-v3/` while preserving auth material in `/data/config`, `/data/gnupg`, and `/data/pass` — this is distinct from the full volume wipe and re-authentication flow documented for GPG/pass corruption
- clarify `docs/setup.md` cert regeneration instructions to make explicit that removing `vault.enc` triggers a full re-authentication, not just a cert refresh; consider adding a `make refresh-cert` target with a clear warning
- add `# syntax=docker/dockerfile:1` as the first line of `bridge/Dockerfile`; without a parser directive the BuildKit Dockerfile frontend version is unpinned and different Docker Engine or BuildKit versions may parse the same Dockerfile differently, producing subtly different images; this matters most for the multi-stage Bridge build where `--from` resolution and layer caching rely on stable parse behavior
- harden the bootstrap check in `bridge/entrypoint.sh` to also verify the pass store is initialized (`$PASSWORD_STORE_DIR/.gpg-id`) as a separate condition from the GPG key check; the current logic only checks `gpg --list-keys "ProtonBridge"` — if the container stops after `gpg --quick-gen-key` succeeds but before `pass init` completes, the next start finds the GPG key and skips the entire bootstrap block, leaving the pass store uninitialized with no `.gpg-id`; if Bridge requires the pass store it will then fail with an unhelpful error; the fix is to add a second conditional so `pass init` is run whenever `.gpg-id` is absent regardless of whether the GPG key exists
- add `binutils` as an explicit `apt-get install` dependency in the builder stage of `bridge/Dockerfile`; the post-build hardening verification step uses `readelf` which comes from `binutils`, currently installed only transitively as a dependency of `gcc`; if Debian ever loosens that transitive dependency, `readelf` silently disappears and the PIE/BIND_NOW/RELRO verification step stops running without failing the build; making `binutils` explicit removes this fragile implicit dependency
- set `--chmod=555` on the `COPY --from=builder /build/bridge /usr/local/bin/bridge` instruction in `bridge/Dockerfile`; the binary currently lands as root:root 755; since the rootfs is read-only at runtime this cannot be exploited, but `555` (no write bit for anyone, including root during image construction) makes the intent explicit and removes any ambiguity that the binary is modifiable

### Hardening and observability
- add resource limits (`memory`, `cpus`, `pids_limit`) to all Compose services that currently lack them; `protonmail-bridge` holds live Proton credentials in memory and is the highest-priority target — a `mem_limit` prevents a runaway or exploited process from exhausting host memory; `ollama` and `indexer` are also missing limits
- add explicit log rotation to the `protonmail-bridge` service in `docker-compose.yml` (`json-file` driver with `max-size: 10m` and `max-file: 3`) so Bridge logs cannot grow unbounded during long-running deployments
- add `stop_grace_period: 30s` (or a tuned value) to the `protonmail-bridge` service in `docker-compose.yml`; Docker's default 10-second stop timeout may not be enough for Bridge to flush pending Gluon IMAP cache writes, close active IMAP sessions, and complete any in-progress vault operations before being force-killed; data corruption in the Gluon database is the risk
- add `HEALTHCHECK` to `indexer/Dockerfile` so stalled indexing is detectable
- pin `python` and `uv` base images to digest in addition to version tag across `indexer/` and `mcp-server/`
- pin `mbsync/Dockerfile` base image (`debian:bookworm-slim`) to a digest; it is currently unpinned while the bridge runtime image is already digest-pinned; mbsync is Bridge's only direct IMAP consumer and receives the bridge password via Docker secret, making it the second highest-trust component
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
- document and test backup, restore, and rollback handling for the `bridge-data` volume so Bridge upgrades and recovery are safer; distinguish essential from optional subdirectories: `/data/config`, `/data/gnupg`, and `/data/pass` must all be included (vault.enc is unreadable without the GPG key stored in gnupg/), while `/data/local` (Gluon IMAP cache) and `/data/cache` can be omitted since Bridge rebuilds them from Proton's API; a backup that omits gnupg/ is silently useless
- add better operator tooling such as a `make bridge-status`-style diagnostic path for auth state, Gluon sync state, recent logs, and IMAP readiness
- keep validating the new read-only Bridge rootfs baseline as Bridge versions change
- audit Bridge and `mbsync` runtime packages regularly and remove unused tools or libraries once verified unnecessary
- evaluate a custom seccomp profile for the Bridge container that restricts syscalls to only what Bridge requires; document the profile and gate it behind a tested list of allowed calls
- document the empty GPG passphrase design constraint in `SECURITY.md` so operators understand that container shell access is equivalent to credential access; note that seccomp, AppArmor, and volume isolation are the primary mitigations
- document host-level hardening expectations for Docker itself, including full-disk encryption for Docker data, encrypted backups, and stronger daemon isolation such as rootless Docker, `userns-remap`, or Docker Desktop Enhanced Container Isolation where available
- ~~add network egress isolation for `app-net`~~ — implemented: `docker-compose.hardened.yml` added; apply with `docker compose -f docker-compose.yml -f docker-compose.hardened.yml up -d` after pulling Ollama models; marks `app-net` as `internal: true`; incompatible with `LLM_MODE=cloud` and with `make pull-models` — documented in the file header
- ~~move `ANTHROPIC_API_KEY` to a Docker Compose secret~~ — implemented: removed from `mcp-server` environment block; `anthropic_api_key` secret added to `docker-compose.yml` backed by `.secrets/anthropic_api_key.txt`; `mcp-server/src/main.py` reads the secret file with env-var fallback for backward compat; `make init-secrets` creates placeholder files and is now a dependency of `make first-run`; `.env.example` updated with instructions
- ~~add `-trimpath` to the Bridge Go build flags~~ — implemented: `GOFLAGS="-trimpath"` set in `bridge/Dockerfile` before `make build-nogui`; permanent guidance added to `AGENTS.md` Go Build Conventions section
- ~~add SLSA build provenance attestation~~ — implemented: `bridge-smoke` job now extracts the Bridge binary and attests it via `actions/attest-build-provenance@v2`; `id-token: write` and `attestations: write` permissions scoped to that job only
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

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

The local-inference layer is fully consolidated onto MLX. The LLM
runs on `mlx-lm-server` (`mlx_lm.server` LaunchAgent on port 8002)
alongside the existing `mlx-service` LaunchAgent (embeddings +
reranking on port 8001). Both reach Apple Metal directly; neither
goes through Ollama. Cloud-Claude (`LLM_MODE=cloud`) remains as the
user-facing quality escape hatch.

What remains: documentation polish (Phase D below) and the previously
paused pre-MLX priorities resume after that.

## Current State

The MLX-rebuild objective (PR #83 + #84 + #85) is **merged to main**.
The stack now runs:

- **ProtonBridge** — Docker, headless, IMAP/SMTP on `bridge-net` only.
- **mbsync** — Docker, pulls into Maildir, `chmod go+r` after each sync.
- **indexer** — Docker, parses Maildir, threads, embeds via the MLX
  service, writes SQLite. Schema is at v15 with 4096-dim vectors.
  The Ollama embed-fallback path was removed in Phase C; embedding
  is MLX-only.
- **mcp-server** — Docker, hybrid search + intelligence tools.
  `hybrid_search` calls the MLX reranker (`Qwen3-Reranker-4B-mxfp8`)
  after RRF. The LLM-synthesis step is dispatched by `LLM_MODE`:
  `local` → `LocalLLMClient.complete()` against the OpenAI-compatible
  `/v1/chat/completions` endpoint at `LLM_BASE_URL` (default
  `http://host.docker.internal:8002/v1`, served by mlx-lm-server),
  `cloud` → Anthropic Claude API (the daily-driver mode at the
  moment — `LLM_MODE=cloud` in `.env`, model `claude-sonnet-4-6`).
  The Tier-2–11 manual-eval pass on 2026-05-04 was run with
  `LLM_MODE=cloud`; that remains the standing quality baseline.
- **mlx-service** — host LaunchAgent on `127.0.0.1:8001`, serves
  `/embed` (Qwen3-Embedding-8B-mxfp8, 4096-dim) and `/rerank`
  (Qwen3-Reranker-4B-mxfp8). Reachable from Docker via
  `host.docker.internal:8001`.
- **mlx-lm-server** — host LaunchAgent on `127.0.0.1:8002`, serves
  the local LLM (default `mlx-community/Qwen3-32B-4bit`) over
  OpenAI-compatible `/v1/chat/completions`. Reachable from Docker
  via `host.docker.internal:8002`.

Memory reality (36 GB M5 Max): the active state (Qwen3-32B-4bit
~17 GB + MLX embed/rerank ~12 GB + macOS ~7 GB) is tight but fits
in physical RAM. With `LLM_MODE=cloud` daily, the local LLM only
loads when the operator explicitly switches modes, so steady-state
RAM is closer to ~19 GB. The single-inference-framework win lands
here: one runtime to tune memory budgets against, no llama.cpp
wrapper overhead.

## Active Priorities

The MLX consolidation is complete; the four pre-MLX priorities below
(intelligence-fidelity validation, test-coverage expansion,
tool-behavior tightening, Tier 1 safety preservation) are now
unpaused.

The MLX consolidation (Phases A–C of the prior plan) shipped together
in 2026-05-04: the LLM client moved to OpenAI-compatible
`/v1/chat/completions`, the class was renamed `OllamaClient` →
`LocalLLMClient`, the Ollama embed-fallback path was deleted (chunker
now bundles the Qwen3-Embedding-8B tokenizer), `mlx_lm.server` was
stood up as a host LaunchAgent on port 8002 serving
`mlx-community/Qwen3-32B-4bit`, and the host Ollama LaunchAgent + the
`pull-models` Makefile target + `scripts/check-host-ollama.sh` were
removed. See "Recently Completed" below for the detailed entry.

### Out of scope for this objective

- Any change to retrieval quality (embedder, reranker, chunker
  defaults). The retrieval stack ships unchanged from PR #83.
- Switching off MLX entirely. The escape hatch is `LLM_MODE=cloud`
  (Claude API) — flip the env var, no code revert needed.
- Adding new MCP tools or changing the MCP API surface.

### MLX-rebuild carryover (still-pending follow-ups from PRs #83/#84/#85)

Independent of the consolidation work above. These are tuning /
validation tasks that should happen regardless of the LLM-engine swap.

1. **Rerank-side quality experiment.** Once retrieval is stable,
   compare `RERANK_ENABLED=true` vs `false` on the manual eval
   set to measure what the rerank stage actually buys you.
2. **Indexer healthcheck threshold.** Currently flagged
   "unhealthy" during sustained MLX-pace indexing because
   `HEALTH_MAX_AGE_SECONDS` was tuned for the prior fast-embed
   path (~50-200ms per call), not Qwen3-Embedding's ~1-3s per
   chunk. Functional impact zero; small follow-up to bump the
   threshold default and/or call `touch_health_file()` more
   aggressively.

The detailed PR-#83 session notes live in the project memory file
``project_mlx_rebuild_session.md``.

## Operational baseline (unchanged regardless of consolidation)

- Python services use per-service `uv` projects with pinned
  `pyproject.toml` + `uv.lock`. Both meet a 90% coverage floor in CI.
- Bridge built from upstream Proton source via `make build-nogui`; a
  patch-drift check + smoke test gate version bumps.
- All long-running services run as non-root with `cap_drop: ["ALL"]`,
  `no-new-privileges`, read-only root filesystems, `pids_limit`, and
  `init: true` for proper signal handling.
- Bridge password lives in `.secrets/bridge_pass.txt` (Docker
  Compose secret), never `.env`. `make first-run` uses
  `logging: driver: none` to keep credentials out of Docker logs.
- Deletion reconciliation is opt-in
  (`INDEXER_DELETION_ENABLED=true`) with a grace window, mass-delete
  brake, and atomic reap-or-rollback.
- A durable `indexing_jobs` queue retries transient failures with
  exponential backoff and dead-letters persistent ones for operator
  visibility.

## Known limitations

- initial sync may take a long time on large mailboxes
- audio / video attachment transcription is not yet supported; other
  formats (PDF / DOCX / XLSX / HTML / TXT / images) are extracted,
  chunked, and searchable
- per-message live retrieval and mail-changing actions are disabled
  in the default deployment until a safe action backend is
  implemented
- test coverage covers `indexer/src` and the full `mcp-server/src`
  package except service bootstrap entrypoints; MCP tool handlers are
  unit-tested through lightweight FastMCP stubs
- `list_threads(filter_type=...)` rejects unsupported values
  cleanly; unread/flagged state remains unindexed
- deletion reconciliation is opt-in and not yet validated under
  long-running real-world conditions; `INDEXER_UNLINK_ON_REAP=true`
  only removes the `.eml` when Maildir is mounted read-write (the
  default is read-only)

## Active priorities (unpaused after MLX consolidation)

These four pre-MLX priorities stay paused until the consolidation
above lands. Don't start them mid-Phase.

_(The four priority blocks below were the active workstream before the MLX rebuild displaced them. With the MLX consolidation now landed, they resume here.)_


### 1. Validate intelligence fidelity end-to-end

Goal:
- confirm the body-text-based RAG path produces noticeably better answers and summaries than the previous snippet-only behavior under real mailbox conditions

Tasks:
- exercise `ask_mailbox`, `summarize_thread`, and `extract_from_emails` against a live mailbox and compare against the prior snippet-only behavior
- tune `PER_THREAD_CHAR_BUDGET` if local LLM context limits are hit in practice
- add regression tests that would catch a reversion to snippet-only prompts

Definition of done:
- answers and summaries consistently reflect whole-thread context in practice, not just in unit tests
- structured extraction is based on full accumulated thread bodies rather than the latest snippet

### 2. Expand test coverage

Goal:
- make parser, threader, database, and MCP behavior safer to change

Tasks:
- keep parser/threader/database tests passing
- keep `list_threads(filter_type=...)` validation tests passing
- add tests for read-only action-tool gating, non-registration, and user-facing failure paths
- add tests for local-only retrieval and system-status behavior
- add integration coverage for indexer watchdog behavior using mocks
- add test for circular `In-Reply-To` references (document expected behavior even if not explicitly handled)
- keep MCP tool-handler coverage in the widened `src` scope; add only
  targeted bootstrap coverage where it can be tested without standing up
  a live transport

Definition of done:
- core indexing and retrieval paths have automated coverage
- risky refactors can be validated without manual mailbox testing
- coverage output is visible in CI so regressions are caught early

### 3. Tighten incomplete or misleading tool behavior

Goal:
- make the exposed MCP surface truthful and dependable

Tasks:
- implement or hide `reply_to_thread`
- implement or hide `create_draft`
- keep `list_threads` filter documentation and validation aligned
- keep action-tool docs and registration behavior aligned

Definition of done:
- the documented tool surface matches runtime behavior
- unsupported paths fail clearly instead of implying functionality that does not exist

### 4. Preserve the Tier 1 safety baseline

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
- confirm current Patterns and expunge behavior remain safe
- keep sync strictly pull-only
- move `BRIDGE_USER` out of the Compose environment and into a file-backed config/secret consumed by `mbsync/entrypoint.sh`; the username is less sensitive than the Bridge password, but it is still authentication material currently exposed via container metadata
- add explicit log rotation plus memory/CPU limits to the `mbsync` service so a noisy sync failure or runaway process cannot fill disk or starve the rest of the stack
- evaluate pinning `mbsync` runtime package versions and/or scanning the image in CI; the base image is digest-pinned, but the installed Debian packages still float at build time

### MCP feature completion
- add attachment download support once the read-only action path is defined
- verify action tools respect read-only guardrails

### Attachment indexing — remaining work
Most of this section is implemented: filenames/MIME indexed in
`attachments_fts`, per-format extraction (PDF / DOCX / XLSX / HTML / TXT /
images) feeds chunks through the existing `message_chunks` lane, dedup runs
via the per-content-hash `attachment_extractions` cache, raw bytes are not
persisted, unsupported types log at debug. What's still open:

- audio / video transcription via Whisper (Tier 3 — model storage + CPU/GPU
  cost is significant; deferred until there's a clear use case)
- expand integration coverage with real-world fixtures (scanned PDFs from
  multiple OCR engines, Office docs from various authoring tools, HEIC
  images, password-protected PDFs)
- consider an attachments-search MCP tool that surfaces attachment-only hits
  (filename + extracted excerpt) separately from thread-level retrieval, for
  "find that PDF" queries that don't need the parent email body

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
- document and enforce embedding dimension assumptions
- make model-switch behavior explicit and safe

### Bridge build and operations
- consolidate `BRIDGE_VERSION` to a single source of truth (`.env.example`) and remove the duplicate hardcoded defaults from `docker-compose.yml` and `bridge/Dockerfile` so version bumps only require one change
- parameterize the Go toolchain version as an `ARG` in `bridge/Dockerfile` alongside `BRIDGE_VERSION` for consistency
- pin all apt packages in `bridge/Dockerfile` to exact versions in both the builder stage (`git`, `make`, `gcc`, `pkg-config`, `libsecret-1-dev`, `libfido2-dev`, `libcbor-dev`) and the runtime stage (`bash`, `pass`, `gnupg2`, `libfido2-1`, `libsecret-1-0`) so a Debian package update cannot silently change the build or runtime environment between identical `BRIDGE_VERSION` builds
- add OCI image labels (`org.opencontainers.image.source`, `org.opencontainers.image.version`, `org.opencontainers.image.revision`) to `bridge/Dockerfile` so every built image carries build provenance that can be traced back to the exact Bridge release and Dockerfile revision
- verify the Proton release tag signature before building: import Proton's published signing key into the builder stage, hardcode the expected fingerprint, and run `git verify-tag ${BRIDGE_VERSION}` after cloning so a tampered or substituted tag fails the build
- add a pre-flight check to `make first-run` that detects an existing `bridge-data` volume and warns the operator before proceeding, since a populated volume means Bridge is already logged in and the interactive CLI will not behave as expected
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to add `bash --version` to the binary checks so the healthcheck dependency is verified
- extend the Bridge smoke test in `scripts/bridge-smoke.sh` to verify that the `bridge --version` output contains `BRIDGE_VERSION` so a version mismatch between the built binary and the configured release is caught without requiring a live Bridge session
- add a `setup-go` step with a pinned Go version to the `bridge-patch-drift` job in `.github/workflows/bridge.yml`; the drift check calls `patch-source.sh` which runs `go build`, so it depends on Go being present at the correct version — currently it relies on whatever Go ships with the `ubuntu-latest` runner, which can silently diverge from Bridge's minimum required version
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
- add a "Bridge Build and Patching" section to `docs/architecture.md` explaining the three source-level patches applied during the Docker build (bind address changed from `127.0.0.1` to `0.0.0.0`, TLS SAN extended to include `protonmail-bridge` and `localhost`, and vault default `AutoUpdate` flipped from `true` to `false` so Bridge's in-process auto-updater does not silently bypass `BRIDGE_VERSION`), why they exist, what `patch-source.sh` and `bridge-patch-drift.sh` do (string-count guards + post-patch compile + synthetic `go test` against `internal/vault.newDefaultSettings`), what `bridge-smoke.sh` adds (end-to-end `Vault loaded ... autoUpdate="false"` log assertion), and what operators should expect when running `make bridge-upgrade-check` during a version bump
- update the Docker Volumes table in `docs/architecture.md` for `bridge-data`: the current description "Bridge credentials, GPG key, config — Yes, losing this requires re-login" is incomplete; the volume also holds the Gluon IMAP cache (`/data/local`), and losing it forces not only re-authentication but also a full mailbox re-download from Proton that can take hours on large mailboxes; the backup recommendation should clarify this so operators understand the full recovery cost and the distinction between essential auth material and the optional-but-expensive-to-rebuild Gluon cache
- update the org-level `marshalltech81/.github` `CONTRIBUTING.md` to add `mbsync/` paths (specifically `entrypoint.sh`, `mbsyncrc.template`, cert handling, and STARTTLS config) as a trigger for running `make bridge-upgrade-check`; changes to the Bridge ↔ mbsync interface directly affect Bridge connectivity but are not currently listed alongside `bridge/` and `BRIDGE_VERSION` in the recommended checks
- add automated tests for `bridge/entrypoint.sh` shell logic using BATS (Bash Automated Testing System) or a comparable shell test framework; the GPG bootstrap path, the vault-exists-but-key-missing error path, the normal noninteractive launch, and the first-run interactive path all have no automated coverage; regressions in these paths currently require a full manual first-run cycle to discover
- document Gluon IMAP cache compatibility in `docs/setup.md` for the Bridge version upgrade procedure (`make update`); the Gluon cache in `bridge-data:/data/local` may be incompatible with a new Bridge version, requiring Bridge to re-download the entire mailbox from Proton's API (potentially hours); document whether Bridge handles Gluon cache migration automatically, what log patterns indicate a re-sync is in progress, and what the operator should do if Bridge fails to migrate the cache
- add operator guidance to `docs/setup.md` for a failed `make bridge-upgrade-check`; currently the output shows a patch drift error but there is no documented next step — operators should be told not to proceed with `make update`, to check whether Proton changed the surrounding source layout, and to wait for a repo patch update rather than attempting manual intervention
- add a Gluon cache corruption recovery procedure to `docs/setup.md`; if Bridge is force-killed during heavy Gluon sync and the Gluon database is corrupted, Bridge may fail to start or serve IMAP; the targeted recovery is to delete only `/data/local/protonmail/bridge-v3/` while preserving auth material in `/data/config`, `/data/gnupg`, and `/data/pass` — this is distinct from the full volume wipe and re-authentication flow documented for GPG/pass corruption
- clarify `docs/setup.md` cert regeneration instructions to make explicit that removing `vault.enc` triggers a full re-authentication, not just a cert refresh; consider adding a `make refresh-cert` target with a clear warning
- add `# syntax=docker/dockerfile:1` as the first line of `bridge/Dockerfile`; without a parser directive the BuildKit Dockerfile frontend version is unpinned and different Docker Engine or BuildKit versions may parse the same Dockerfile differently, producing subtly different images; this matters most for the multi-stage Bridge build where `--from` resolution and layer caching rely on stable parse behavior
### Hardening and observability
- add resource limits (`memory`, `cpus`, `pids_limit`) to the Compose services that still lack them; `protonmail-bridge` holds live Proton credentials in memory and is the highest-priority target — a `mem_limit` prevents a runaway or exploited process from exhausting host memory. (`indexer` already has `mem_limit: 6g` and `pids_limit: 128`. Empirical tuning history during a real backfill of a ~22k-message attachment-heavy mailbox: 1 GiB was too tight under normal indexing bursts; 2 GiB OOM-killed three times during initial backfill; 4 GiB OOM-killed once more on the same backfill; 6 GiB has been stable. Steady-state RSS is much lower; the right long-term fix is throttling the indexer's parallel embed dispatch — peak memory scales with concurrency, not mailbox size — at which point the cap can return to 2-3 GiB. The in-stack `ollama` container was removed by PR #86, so the prior note about Ollama lacking limits no longer applies.)
- add `docs/troubleshooting.md` covering common failure patterns: Ollama not ready, cert extraction failure, sync stalled, schema migration
- evaluate optional bearer-token auth for `mcp-server` as defense-in-depth on top of the localhost-only bind; today any local process that can reach `127.0.0.1:MCP_PORT` can query the index. This is a posture change, not a bug fix — weigh the operational complexity of a shared token against the threat model (malware on the host, misconfigured port forward) before implementing
- emit a loud, one-shot `LLM_MODE=cloud` warning at `mcp-server` startup reminding the operator that retrieved email excerpts will be sent to Anthropic; the cloud path is already opt-in but the warning would surface drift if someone flips the mode and forgets
- migrate the Python type checker from `mypy` to `pyright` for both `indexer` and `mcp-server`; the user's global default for personal Python projects (`~/.claude/memory/tools/uv-python-stack.md`) is `pyright` and this project is the outlier. The trigger should be a real signal (mypy gets slow, mypy misses a bug, friction from context-switching against other projects), not a pre-emptive sweep — pyright is generally stricter at narrowing and will surface a multi-PR cleanup before CI is green again. When migrating, touch `pyproject.toml` (both services), `.pre-commit-config.yaml`, the `make typecheck*` targets, the relevant CI workflow, and the AGENTS.md typecheck guidance in one branch.

## Later Backlog

- per-session LLM mode toggle
- extract Bridge container work into standalone repo after stabilization
- improve operational observability and health reporting
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

### 2. Live Bridge integration lane
Need final decision on the long-term home for live Bridge smoke tests:
- GitHub-hosted manual/nightly workflow using a dedicated no-2FA Proton test account
- or a trusted self-hosted runner with persistent authenticated Bridge state

## Recently Completed

The repository was simplified end-to-end to drop migration debt that had
accumulated over the prior schema-v1-through-v12 history. Net effect:
~1,200 lines of code deleted, ~500 lines simplified. Highlights:

- **Schema collapsed** into one ``_apply_initial_schema`` (the v1→v12
  per-version migration methods, the ``schema_version`` reset
  workaround, the ``ALTER TABLE`` / ``try/except OperationalError``
  patterns, and every "legacy row" defensiveness path are gone). A
  schema version mismatch now raises a clear "wipe the volume" error
  rather than silently running migrations.
- **`src/backfill.py` deleted** along with `make backfill-chunks` and
  the per-version migration tests; the chunker runs on every new
  message in the steady-state path and there's no pre-existing data to
  fill in.
- **Cross-container Maildir handoff simplified** from the
  mailshare/SGID/umask dance to a one-liner post-sync `chmod go+r`.
  mbsync stays UID 1001, indexer stays UID 1002, indexer's mount stays
  `:ro`, defense-in-depth preserved.
- **`scripts/maildir-perms-smoke.sh` deleted** along with `make
  maildir-perms-smoke`.
- **`build_merged_body` removed** and `body=` parameter dropped from
  `upsert_thread`; merge-on-update logic is internal-only.
- **`senders → participants` legacy fallback removed** from the MCP
  filter and row reader; senders is always populated on writes.
- **PLAN.md "Recently Completed" history** reduced from a 25-entry
  log to this summary.

The functional surface — chunker, attachment extractors (PDF/DOCX/
XLSX/HTML/TXT/image-OCR), per-occurrence + per-content-hash dedup,
hybrid RRF retrieval with chunk lane, intelligence-tool chunk
evidence, deletion reconciler, durable indexing queue — all stays.

### 2026-05-04 — Hybrid-search index fix (schema v15)

The chunk- and thread-keyword lanes of `hybrid_search` were JOINing
their FTS5 virtual tables back to base tables on `<base>.fts_rowid`
without an index on that column. SQLite planned `SCAN <base>` per
FTS5 hit; on a populated mailbox (~15k FTS hits × 73k chunk rows)
that produced ~1.1B row comparisons per query, blocking
`search_emails` for 5–7 minutes and blowing Claude Desktop's 4-min
MCP request timeout. Fix shipped as `SCHEMA_VERSION = 15` plus
`indexer/src/migrations/0015_fts_rowid_indexes.sql`, with the same
indexes added to `_apply_initial_schema` for fresh installs and an
`ANALYZE` so the planner picks them up immediately. End-to-end
`hybrid_search` went from ~400 s to ~0.5 s (≈800× speedup); the
isolated chunk-keyword lane went 350 s → 21 ms (≈17,500×). Memory:
`project_fts5_join_missing_index.md`. Misdiagnoses logged at the
bottom of that memory so future regressions don't re-walk WAL
pinning / FTS5 fragmentation as causes.

### 2026-05-04 — RERANK_CANDIDATES tuning (carryover #3)

Defaulted to 50, tuned during the session. While diagnosing the
slow-search bug above, lowered to 8 under a wrong rerank-cost
theory, then restored to 20 once the SQL stage was identified as
the real bottleneck. Final value of 20 balances funnel size against
chunk-lane fetch cost (`fetch_limit = max(limit, candidates) × 10`).

### 2026-05-04 — mcp-server WAL pinning fix

`Database._conn` was a single shared `?mode=ro` `sqlite3.Connection`
that pinned a WAL read mark and blocked `PRAGMA
wal_checkpoint(TRUNCATE)` from the indexer (writer) side, letting
`mail.db-wal` grow unbounded under sustained writer activity (159 MB
observed). Refactored production reads to open a fresh connection and
close it explicitly after each query. Verified live: checkpoint now
returns `(0, 0, 0)` SUCCESS with mcp-server running, WAL truncates to
0 bytes.
All 314 mcp-server tests pass at 92.81% coverage. Memory:
`project_mcp_wal_pinning.md`.

### 2026-05-04 — MLX consolidation Phases A + B + C (Ollama abandoned)

The local-inference layer is fully consolidated onto MLX. Phase A
(LLM-client OpenAI-compat refactor), Phase B (operational MLX-LM
swap), and Phase C (Ollama embed-fallback + scaffolding teardown)
all shipped together.

- **Phase B — Stand up `mlx_lm.server` and abandon Ollama.** New
  ``mlx-lm-server/`` peer to ``mlx-service/``: pinned ``mlx-lm==0.31.3``
  in its own uv project, ``com.local.mlx-lm-server`` LaunchAgent
  bound to ``127.0.0.1:8002`` serving
  ``mlx-community/Qwen3-32B-4bit`` over OpenAI-compatible
  ``/v1/chat/completions``. ``--max-tokens 4096`` set as the server
  default to give Qwen3's thinking-mode answers headroom when
  callers don't override per-request. ``.env.example`` and
  ``docker-compose.yml`` defaults flipped to point at MLX-LM
  (``LLM_BASE_URL=http://host.docker.internal:8002/v1``,
  ``LLM_MODEL=mlx-community/Qwen3-32B-4bit``). End-to-end smoke
  test confirmed: ``LocalLLMClient.complete()`` against the live
  server returns a correct grounded answer in production shape.
  Open WebUI overlay reconfigured to use Open WebUI's
  OpenAI-compatible client pointed at the same ``LLM_BASE_URL``
  (``ENABLE_OLLAMA_API=false``, ``ENABLE_OPENAI_API=true``).
- **Ollama teardown.** ``com.local.ollama-host`` LaunchAgent
  bootout'd, plist deleted from ``~/Library/LaunchAgents/``.
  ``scripts/check-host-ollama.sh`` deleted. ``pull-models``
  Makefile target deleted along with its help-text entry and the
  ``.PHONY`` listing. ``AGENTS.md`` non-negotiables rewritten to
  reflect the host MLX servers (no more brew-upgrade verification
  rules, no more 0.0.0.0:11434 firewall guidance — both
  LaunchAgents bind 127.0.0.1 with macOS' loopback exemption).
  ``docs/setup.md`` and ``docs/architecture.md`` rewritten end-to-end
  to remove the Ollama section and document the
  mlx-service / mlx-lm-server pair as the supported deployment shape.

#### Earlier in the same session — Phase A + Phase C "remove" branch

Phase A (LLM-client OpenAI-compat refactor + class rename) and the
Phase C "remove embed fallback" branch shipped together as the
preparatory work for Phase B's operational MLX-LM swap.

- **Phase A — Decouple the local-LLM client.** ``OllamaClient`` →
  ``LocalLLMClient`` (file ``mcp-server/src/lib/ollama.py`` →
  ``local_llm.py``). ``complete()`` now POSTs the OpenAI-compatible
  ``/v1/chat/completions`` shape and unwraps
  ``choices[0].message.content``; new ``LLM_BASE_URL`` and
  ``LLM_MODEL`` env vars replace ``OLLAMA_LLM_MODEL``. The same
  client targets both Ollama (``:11434/v1``) and ``mlx_lm.server``
  with no per-backend branching, so the engine swap in Phase B
  becomes an env-var change.
- **Phase C — Remove the Ollama embed fallback.** Deleted ``class
  Embedder``, ``_make_embedder``, ``_validate_embed_model_tokenizer``,
  ``USE_MLX_EMBEDDER``, ``OLLAMA_EMBED_MODEL``, ``OLLAMA_HOST`` (from
  indexer + mcp-server), the ``LocalLLMClient.embed()`` Ollama
  branch, and the bundled ``nomic-embed-text/tokenizer.json``.
  Replaced the chunker's bundled tokenizer with the
  ``Qwen3-Embedding-8B`` BPE tokenizer (matches the production
  embedder; chunk budgets recomputed against real tokens). Updated
  the CJK regression test threshold to reflect Qwen3's more efficient
  multilingual BPE.
- **Test surface.** ``FakeOllama`` → ``FakeLocalLLM``, ``fake_ollama``
  fixture → ``fake_llm``; tests dropped for the deleted Ollama
  embed-readiness model-pull paths.

Verified end-to-end: 455 indexer tests + 315 mcp-server tests pass
(both at 93%+ coverage), ``make typecheck`` and ``ruff check`` clean,
``docker compose config`` validates. The diff is net-removal: the
slimmed embedder module shrinks from 193 → 75 lines, the slimmed LLM
client constructor goes from 7 params (with two optional Ollama-embed
kwargs) to 3 required ones.

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Treat this file as the current execution plan, not as permission to ignore architectural constraints.
- When a task is completed, move it to `Recently Completed` or remove it.
- Keep this file concise and current.

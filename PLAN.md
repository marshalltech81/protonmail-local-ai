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

Rebuild the embedding + reranking layer to run natively on MLX (Apple
Metal) on the host, while keeping Ollama as the LLM. The existing
SQLite index has already been wiped — this is a fresh-start rebuild,
not a migration. Goal is meaningfully better retrieval (richer
embeddings + a true cross-encoder rerank stage) for `ask_mailbox` and
the rest of the intelligence surface.

## Current State

Stack today is unchanged in shape (see `docs/architecture.md` for the
data flow). The change in this objective is to **add a sixth service**
running bare-metal on the host:

- **ProtonBridge** — Docker, headless, IMAP/SMTP on `bridge-net` only.
- **mbsync** — Docker, pulls into Maildir, `chmod go+r` after each sync.
- **indexer** — Docker, parses Maildir, threads, embeds, writes SQLite
  (FTS5 + `sqlite-vec`). Embedding client will be redirected to MLX.
- **Ollama** — host or Docker depending on overlay. **Stays as the LLM**.
  Existing `OLLAMA_EMBED_MODEL` (`nomic-embed-text`) becomes a fallback
  path only, gated by `USE_MLX_EMBEDDER=false`.
- **mcp-server** — Docker, hybrid search + intelligence tools on
  `localhost:3000`. Hybrid search gains a rerank stage after RRF.
- **mlx-service** *(new)* — host process, FastAPI, Metal-backed, binds
  `127.0.0.1:8001`. Serves `/embed`
  (`mlx-community/Qwen3-Embedding-8B-mxfp8`, 4096-dim, ~8 GB resident)
  and `/rerank` (`mlx-community/Qwen3-Reranker-4B-mxfp8`, ~4 GB
  resident, generation-style yes/no logit scoring per Qwen3-Reranker's
  trained behavior — *not* a yes/no workaround) via `mlx-embeddings`.
  Lazy-loads each model on first request, keeps resident. Started by
  LaunchAgent at login. Reachable from Docker via
  `host.docker.internal:8001`.

  Memory budget on the 36 GB M5 Max: LLM ~19 GB + embedder ~8 GB +
  reranker ~4 GB + macOS baseline ~7 GB ≈ 38 GB nominal, with mxfp8
  model footprints typically a bit under nameplate in practice. If
  steady-state pressure shows, fallback is `Qwen3-Embedding-8B-4bit-DWQ`
  (~4 GB) for the embedder.

The index is empty. Schema will be rebuilt from scratch with the new
4096-dim vector tables; no migration tooling needed for this objective.

## Active Priorities

The previous priorities (intelligence-fidelity validation, test-coverage
expansion, tool-behavior tightening, Tier 1 safety preservation) are
**paused** behind the MLX rebuild. They resume after step 5 below.

### Status as of 2026-05-02

Steps 1-4 are code-complete and green across all three test suites
(mlx-service: 6, mcp-server: 311, indexer: 460 — 777 total). Step 5
deferred until the operator can monitor for OOM and quality
regressions. Live subset reindex (the verify step inside step 3)
also deferred — needs a Docker rebuild + supervised run.

The detailed pickup checklist lives in the project memory file
``project_mlx_rebuild_session.md`` (autonomous-session snapshot).

### 1. Build mlx-service standalone

Goal:
- a working host-side FastAPI app that serves `/embed`, `/rerank`,
  and `/health` against Qwen3-Embedding-8B and BGE-reranker-v2-m3,
  validated by curl, with no indexer or mcp-server changes yet.

Tasks:
- create a new top-level `mlx-service/` directory (uv-managed Python
  project, follows `~/.claude/memory/tools/uv-python-stack.md`)
- pick the Qwen3-Embedding-8B precision (mxfp8 vs bf16) based on M5 Max
  memory headroom alongside the current Ollama LLM
- implement endpoints:
  - `POST /embed` — `{"input": str | list[str]}` → response shape
    matching Ollama's `/api/embeddings` so the indexer's parser is
    URL-only change
  - `POST /rerank` — `{"query": str, "documents": list[str], "top_n": int?}`
    → `[{index, score}, ...]` sorted desc; cross-encoder scoring via
    the model's classification head (`pooler_output`), not embedding
    cosine
  - `GET /health` — which models are loaded, approximate resident memory
    per model
- lazy-load each model on first request; keep resident across calls
- bind `127.0.0.1:8001` only
- run as a `uvicorn` app inside a single process

Verify:
- `curl` against `/embed` returns a 4096-dim float vector for a known
  string; deterministic across calls
- `curl` against `/rerank` for a known (query, [docs]) tuple returns
  scores that rank an obviously-relevant doc above an obviously-
  irrelevant one
- `/health` reflects model load state and memory before/after first call
- the process holds memory steady (no leak) across ~50 mixed calls

Definition of done:
- `mlx-service/` runs on the host with `uv run uvicorn ...` and passes
  the three curl checks above
- **stop and confirm with the operator before step 2**

### 2. LaunchAgent + firewall

Goal:
- mlx-service starts at login, survives reboot, and does not produce
  Mac firewall prompts on every restart.

Tasks:
- write `~/Library/LaunchAgents/com.local.mlx-service.plist` modeled on
  the existing `com.local.ollama-host.plist` pattern
  (`~/.claude/memory/tools/brew-services.md` — separate-label LaunchAgent
  so we own the lifecycle, not brew)
- decide between codesigning the `python` / `uvicorn` binary or adding
  an explicit `socketfilterfw` allow rule for the venv interpreter
  (`~/.claude/memory/tools/macos-firewall.md`); pick whichever is
  reproducible across `uv` venv rebuilds
- redirect stdout/stderr to a rotated log file under `~/Library/Logs/`

Verify:
- `launchctl print "gui/$(id -u)/com.local.mlx-service"` shows loaded
- `lsof -iTCP:8001 -sTCP:LISTEN` shows bind on `127.0.0.1` only
- reboot, then re-run both checks plus the curl checks from step 1
- no firewall prompt appears across at least two restarts

Definition of done:
- LaunchAgent installed and reboot-stable
- **stop and confirm before step 3**

### 3. Wire indexer to mlx-service behind a feature flag

Goal:
- indexer embedding calls route to MLX when `USE_MLX_EMBEDDER=true`
  (the new default), and fall back to Ollama embed when `false`.

Tasks:
- add env vars (and document them in `.env.example` with brief comments):
  - `USE_MLX_EMBEDDER` (default `true`)
  - `MLX_SERVICE_URL` (default `http://host.docker.internal:8001`)
  - `RERANK_CANDIDATES` (default `50`)
  - `RERANK_TOP_N` (default `10`)
  - `INDEXER_CHUNK_MAX_TOKENS` (raise from current ~500 to `1500` —
    Qwen3-Embedding handles long context; the prior cap was a nomic
    constraint)
- introduce a thin `EmbeddingClient` interface in the indexer with two
  implementations (Ollama, MLX); select at startup from `USE_MLX_EMBEDDER`
- update SQLite schema for 4096-dim vectors; bump `SCHEMA_VERSION` and
  add a forward-migration file even though no live data exists, per
  the AGENTS.md schema rule
- update `indexer/pyproject.toml` if needed; keep it minimal
- run an end-to-end fresh index against a small subset (a single folder
  or a date range — operator picks) to validate before full reindex

Verify:
- `make build && make up` plus a small-subset reindex finishes without
  errors; vector rows have the expected dimension
- toggling `USE_MLX_EMBEDDER=false` falls back cleanly to Ollama for a
  separate test database
- per-message and per-chunk vector counts match what the chunker emits

Definition of done:
- subset reindex green on MLX path
- **stop and confirm before step 4**

### 4. Wire mcp-server hybrid search to call /rerank

Goal:
- hybrid search returns `RERANK_CANDIDATES` from RRF, sends
  `(query, candidate_texts)` to `/rerank`, takes `RERANK_TOP_N`, and
  hands those to the LLM. No new MCP tools, no API surface change.

Tasks:
- add `RerankerClient` in mcp-server (mirrors EmbeddingClient pattern
  in the indexer)
- thread `query` text through to the rerank stage (today's RRF stage
  already has it; just plumb to the new step)
- add unit tests for the rerank stage with a fake reranker that
  returns deterministic scores; verify ordering, top_n cutoff, and
  failure behavior (rerank service down → degrade to RRF order, log
  the failure, do not silently return zero results)
- exercise `ask_mailbox`, `summarize_thread`, and `extract_from_emails`
  against the small-subset index from step 3; capture before/after
  responses for the manual eval set
  (`mcp-server/tests/eval/eval-queries.md`,
  `feedback_manual_eval_preference.md`)
- watch the `ask_mailbox slow on populated mailbox` failure mode
  (project memory `project_ask_mailbox_slow.md`) — reranking on top of
  hybrid retrieval can make this worse if `RERANK_CANDIDATES` is too
  high; tune accordingly

Verify:
- existing mcp-server unit tests still pass; coverage stays ≥90%
- new rerank-stage tests cover the success path, the cutoff, and the
  degraded-when-rerank-down path
- manual eval shows a noticeable quality lift on at least the queries
  that previously failed under snippet-only / nomic embeddings

Definition of done:
- subset-index manual eval looks meaningfully better
- **stop and confirm before step 5**

### 5. Full reindex of the whole mailbox

Goal:
- production-shape index built from scratch on the MLX path, against
  the entire Maildir.

Tasks:
- `make clean` (or equivalent volume reset) to ensure no half-state,
  then full `make up` and indexer backfill
- watch `mlx-service` resident memory across the run; restart and tune
  precision (mxfp8 vs bf16) if memory headroom is tight
- watch the indexer's `mem_limit: 6g` against the new chunk-token cap
  (1500 vs 500 raises peak memory per concurrent embed call); tune
  concurrency or bump the cap if OOMs reappear
- after the reindex, re-run the manual eval set against the full mailbox

Verify:
- backfill completes without OOM or queue dead-lettering
- `ask_mailbox` against full mailbox stays under the Open WebUI ~60s
  client timeout for typical queries (project memory
  `project_ask_mailbox_slow.md`)
- manual eval is at least as good on the full mailbox as on the subset

Definition of done:
- full mailbox indexed and searched on MLX
- documentation updated (`docs/architecture.md`, `docs/setup.md`,
  `docs/mcp-tools.md`, `README.md`, `AGENTS.md` non-negotiables for
  the new host service, `.env.example`) per the AGENTS.md doc rule
- **stop, confirm, and unpause the previously paused priorities**

## Out of Scope for This Objective

- migrating chat to MLX — Ollama stays as the LLM
- alongside-and-swap reindex tooling — the index is already gone
- adding new MCP tools or changing the MCP API surface — this is
  purely a retrieval-quality upgrade
- removing the Ollama embed code path — it remains as a feature-flag
  fallback so we can roll back integration without re-indexing

Operational baseline:

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

Known limitations:

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

## Active Priorities

Work these in order.

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
- add resource limits (`memory`, `cpus`, `pids_limit`) to the Compose services that still lack them; `protonmail-bridge` holds live Proton credentials in memory and is the highest-priority target — a `mem_limit` prevents a runaway or exploited process from exhausting host memory; `ollama` is also missing limits. (`indexer` already has `mem_limit: 6g` and `pids_limit: 128`. Empirical tuning history during a real backfill of a ~22k-message attachment-heavy mailbox: 1 GiB was too tight under normal indexing bursts; 2 GiB OOM-killed three times during initial backfill; 4 GiB OOM-killed once more on the same backfill; 6 GiB has been stable. Steady-state RSS is much lower; the right long-term fix is throttling the indexer's parallel embed dispatch — peak memory scales with concurrency, not mailbox size — at which point the cap can return to 2-3 GiB.)
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

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Treat this file as the current execution plan, not as permission to ignore architectural constraints.
- When a task is completed, move it to `Recently Completed` or remove it.
- Keep this file concise and current.

# AGENTS.md

## Purpose

This repository provides a fully local, privacy-first AI search and intelligence layer for ProtonMail.

The default stack consists of five containers:

- ProtonBridge
- mbsync
- Ollama
- indexer
- MCP server

An optional Open WebUI overlay can be started for a local browser UI. It must
reuse the existing Ollama and MCP server containers; do not add a second Ollama
instance for the UI.

Core behavior:

- email stays local by default
- Bridge is the only path to Proton
- mbsync pulls mail into Maildir
- indexer parses and stores thread-level data in SQLite
- MCP exposes search, retrieval, intelligence, and action tools over SSE and/or
  Streamable HTTP depending on `MCP_TRANSPORT`

## Priorities

When making changes, follow these priorities in order:

1. Preserve privacy and local-first behavior.
2. Do not weaken secret handling.
3. Do not broaden network exposure.
4. Preserve the current architecture unless a change is explicitly required.
5. Prefer the smallest safe change over broad refactors.
6. Keep runtime images minimal and non-root.
7. Preserve thread-level indexing and hybrid search behavior.

## Read This Before Editing

Before making non-trivial changes, read:

- `PLAN.md` for current implementation priorities and active work
- `docs/architecture.md` for system design and data flow
- `docs/setup.md` before changing Bridge, first-run flow, TLS, or credentials
- `docs/mcp-tools.md` before changing MCP tool behavior

If a change touches container boundaries, TLS, Bridge auth, mbsync behavior, indexing strategy, or schema design, read the relevant docs first.

## Architecture Summary

High-level data flow:

1. ProtonBridge connects to ProtonMail.
2. mbsync pulls from Bridge into Maildir.
3. indexer parses Maildir messages, builds conversation threads, generates embeddings via Ollama, and writes SQLite.
4. MCP server reads from SQLite and exposes tools over SSE and/or Streamable HTTP.
5. Only the MCP server is exposed to the host on `localhost:3000` by default.
   The optional Open WebUI overlay may additionally expose a localhost-only
   browser UI on `localhost:8080`.

Important architecture facts:

- the index is SQLite with FTS5 plus `sqlite-vec`
- retrieval is hybrid keyword plus vector search with RRF
- indexing is thread-level, not message-level
- MCP defaults to SSE transport; `MCP_TRANSPORT=streamable-http` enables
  Streamable HTTP, and `MCP_TRANSPORT=dual` serves both `/sse` and `/mcp`
- `ollama` uses the official image with no custom Dockerfile

## Non-Negotiable Constraints

Do not make any of the following changes unless the repository owner explicitly asks for them.

### Platform and base image constraints

- Do not switch runtime images to Alpine.
- Do not introduce distroless images for Bridge.
- Do not add Qt dependencies to Bridge.
- Bridge must continue using `make build-nogui`.

### Network and exposure constraints

- Do not expose any container port other than `mcp-server:3000` to the host.
  Exception: the optional Open WebUI overlay may expose only
  `127.0.0.1:${OPEN_WEBUI_PORT:-8080}:8080`.
- Do not add `network_mode: host`.
- Do not give `mcp-server` direct IMAP access to Bridge.
- Do not give `indexer` direct IMAP access to Bridge.
- mbsync is the only container that should talk directly to Bridge IMAP.
- Do not give Open WebUI direct access to Bridge, Maildir, or SQLite volumes.
- Do not expose Open WebUI on `0.0.0.0` or a LAN interface without explicit
  owner approval.

### Mail sync and safety constraints

- Do not change mbsync to write back to Proton.
- `mbsyncrc.template` must remain pull-only.
- Do not remove `Expunge None`.
- Do not add `All Mail` or `Labels/*` to mbsync Patterns.
- Do not weaken or bypass TLS verification casually.

### TLS and connection security constraints

- Never set `ssl.CERT_NONE` or `check_hostname = False` in any service.
- Always fail closed when TLS cert extraction or validation fails.
- Do not add or extend any TLS bypass without explicit owner approval.
- Treat a disabled TLS verification path as a security regression, not a convenience.

### Data model constraints

- Threads remain the **coarse unit of indexing and retrieval**. Per-message
  chunks (`message_chunks` / `message_chunks_fts` / `message_chunks_vec`)
  are an *additive* precision-retrieval layer on top of thread-level
  indexing — they do not replace it. Do not remove thread-level rows or
  vectors. The thread vector is derived as the mean of a thread's chunk
  vectors so coarse and precise retrieval share source data.
- Do not change the SQLite schema without incrementing `SCHEMA_VERSION`.
  The indexer fails fast on a stored-vs-code version mismatch rather than
  running migrations; until a real migration runner is added, schema
  changes require wiping `sqlite-volume` so the indexer rebuilds from
  Maildir.
- Do not change embedding dimensions or model assumptions without verifying schema and context-window implications.
- Do not change chunk ID derivation away from the deterministic
  `sha256(message_pk || index || text)` shape — re-runs depend on identical
  inputs producing identical IDs so the diff-write path skips already-
  embedded chunks. Attachment chunks use
  `message_pk = f"{message_id}::{attachment_id}"`.
- Do not store raw attachment payload bytes in SQLite. The current schema
  keeps bytes only in the `.eml` on disk. ``attachment_extractions`` caches
  the extracted *text* per content hash so OCR / parse cost runs at most
  once per unique payload, not the bytes themselves.

## Bridge-Specific Guardrails

Bridge has special behavior and must be handled carefully.

Important facts:

- Bridge is built from Proton source using `make build-nogui`.
- Bridge runs as non-root user `bridge` with UID 1000.
- all required XDG variables must be set or Bridge may fall back to unexpected paths
- Bridge account detection checks for:
  `$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc`
- Bridge binds to `0.0.0.0` via a source patch so mbsync can reach it from another container
- Bridge TLS SANs are patched so the cert is valid for `protonmail-bridge` and `localhost`
- Bridge v3 stores credentials and TLS cert material in `vault.enc`
- the cert is not read from plain files on disk
- mbsync cert extraction is done from a live connection with `openssl s_client`

Operational implications:

- if you touch Bridge build logic, TLS logic, auth storage, or XDG paths, review setup and recovery behavior first
- do not assume rebuilding Bridge updates the existing cached cert in `vault.enc`
- do not replace pass/gpg-based behavior with a weaker shortcut

## Secret Handling

Secrets are a hard boundary.

### Rules

- Never commit secrets.
- Never print secrets to logs if avoidable.
- Never move credentials from Docker secrets into `.env` for convenience.
- Prefer Docker secrets over environment variables for sensitive values.
- Treat accidentally committed secrets as compromised immediately.

### Sensitive files that must never be committed

- `.env`
- `.secrets/bridge_pass.txt`
- `mbsync/bridge-cert.pem`
- any `.pem`, `.key`, `.p12`, or `.pfx` file
- any ad hoc export containing credentials, tokens, or private keys

### Credential-specific rules

- `BRIDGE_USER` comes from Bridge CLI `info`, not the Proton account password
- `BRIDGE_PASS` belongs in `.secrets/bridge_pass.txt`, not `.env`
- `LLM_MODE=local` uses Ollama
- `LLM_MODE=cloud` uses Claude API

### Commit hygiene

Before staging or committing, check for secrets:

```bash
git diff --staged | grep -iE '(password|pass|secret|token|key|credential)' | grep '^\+'
```

If a secret was committed, rotate it and remove it from git history using `git filter-repo`.

Do not rely on `git commit --amend` or interactive rebase for secret removal.

## Change Strategy

When working in this repo:

- prefer narrow, surgical edits
- preserve existing interfaces unless there is a clear reason to change them
- keep comments and code aligned
- make multi-step logic easy to follow in code by keeping the flow explicit and adding brief comments or docstrings where the steps would otherwise be unclear
- update docs when behavior changes
- always review the relevant documentation after code, config, workflow, or runtime changes and adjust it so the repository docs stay in sync with the implementation
- if code or config changes create likely doc drift, update the relevant docs or explicitly suggest the needed doc or `AGENTS.md` follow-up
- avoid introducing new dependencies without a clear need
- install only the minimum necessary packages, libraries, and dependencies for the current implementation, whether in Docker images, Python projects, system packages, or tooling
- pin all new dependencies to exact versions, except apt packages — see Dockerfile Conventions
- avoid speculative refactors
- preserve local-first defaults

## Commit Message Style

When creating commits in this repository:

- follow the existing lowercase Conventional Commit style used in history
- default to `type(scope): imperative summary`
- omit the scope only when the change is truly repo-wide or no single scope fits cleanly
- use concise scopes such as `bridge`, `mbsync`, `indexer`, `database`, `parser`, `docker`, `setup`, `mcp-tools`, `makefile`, `pre-commit`, `test`, or `deps`
- use types like `docs`, `fix`, `feat`, `chore`, `test`, or `style`
- keep the subject short, imperative, and specific to the user-visible change
- avoid mixing unrelated changes into one commit when separate commits would read more clearly in history

Examples:
- `fix(bridge): patch TLS cert SAN for protonmail-bridge`
- `docs(setup): add mbsync verification steps`
- `chore(pre-commit): add detect-secrets baseline`
- `style: apply pre-commit autofixes across repo`

## Common Commands

```bash
make build
make first-run
make pull-models
make up
make logs
make status
make clean
```

Use `make clean` only when destructive cleanup is intended.

## Dockerfile Conventions

All Dockerfiles in this repository must follow these rules:

- every runtime image runs as a dedicated non-root user with explicit UID/GID
- current expected UIDs are:
  - `bridge=1000`
  - `mbsync=1001`
  - `indexer=1002`
  - `mcp=1003`
- the indexer reads mbsync-written Maildir files via "other" permission
  bits, not a shared group. mbsync's entrypoint runs `chmod go+r` on new
  Maildir files after each sync (because mbsync ignores umask and
  `open()`s with mode 0600); the indexer mounts `/maildir` as `:ro` so
  the cross-UID separation also blocks any write path even on a future
  malicious-attachment-driven indexer compromise.
- use multi-stage builds when toolchains are needed
- build toolchains must not remain in runtime images
- copy dependency manifests before source files for layer caching
- use `apt-get install --no-install-recommends`
- do not pin apt package versions; Debian/Ubuntu point releases drop old versions from the archive, so pinned `apt-get install pkg=x.y.z` lines break unpredictably when the base image refreshes. Rely on the pinned base image digest plus `apt-get update` for reproducibility instead.
- remove `/var/lib/apt/lists/*` in the same layer as install
- use `pip --no-cache-dir`
- prefer `COPY --chmod=755` over separate `RUN chmod`
- pre-create directories and set ownership before declaring `VOLUME`
- explicitly set restrictive permissions on sensitive runtime directories and files
- add a `HEALTHCHECK` when practical
- pin base images to specific versions
- do not use `:latest`
- every service directory should include a `.dockerignore`
- harden images and containers as far as practical using Docker and Linux best practices, while preserving the repo's required functionality
- prefer a read-only root filesystem, `tmpfs` for ephemeral writable paths, `no-new-privileges`, and dropped Linux capabilities when the service will tolerate them
- keep runtime packages minimal, avoid unnecessary shells/tools in runtime images, and pin images by digest where practical
- remove unused runtime packages, binaries, and libraries once the service is confirmed not to need them
- prefer non-login service-account shells such as `/usr/sbin/nologin` unless the service genuinely depends on a login shell
- do not assume operational flows rely on `su` or login-shell access; entrypoints and explicit `docker exec <cmd>` paths should remain sufficient
- prefer exec-form `ENTRYPOINT` / `CMD` so the service receives signals directly
- keep default seccomp/AppArmor confinement in place and do not loosen container security profiles casually
- never use `privileged`, mount the Docker socket, add host devices, or broaden kernel/container privileges without explicit owner approval

## Container Runtime Hardening

When changing Docker Compose service definitions or runtime behavior:

- prefer `read_only: true` with narrowly-scoped writable volumes and `tmpfs` mounts instead of broad writable filesystems
- use `security_opt` such as `no-new-privileges:true` and `cap_drop: ["ALL"]` by default, then add back only what a service proves it needs
- keep seccomp/AppArmor confinement in place by default and avoid unconfined profiles
- add resource controls such as memory limits, `pids_limit`, and log rotation when practical
- keep container-to-container network access as narrow as the architecture allows
- prefer degraded modes over broadening privileges, relaxing confinement, or exposing more of the host

## Go Build Conventions

Bridge is the only Go service in this repository. When modifying the Bridge
build in `bridge/Dockerfile` or any future Go service, follow these rules:

### Build target

- invoke `make build-nogui` using Proton's upstream Makefile without injecting
  custom `GOFLAGS`, `CGO_CFLAGS`, or `CGO_LDFLAGS`
- Proton is a security company; their upstream build configuration reflects their
  own security requirements — do not second-guess it by layering additional flags
- if a future evaluation concludes that specific additional flags are warranted,
  document the rationale clearly and get explicit owner approval before adding them

### Toolchain version safety

- set `GOTOOLCHAIN=local` as a builder-stage `ENV`; this prevents the Go
  toolchain from auto-downloading a different Go version at build time if `go.mod`
  carries a `toolchain` directive requesting a newer version — the pinned base
  image is the source of truth and must not be silently overridden

### Module integrity

- run `go mod download && go mod verify` after cloning source and before building;
  `go mod verify` confirms every cached module matches its checksum in `go.sum`,
  failing the build if any module has been tampered with or corrupted

### CGO build mode

- never build with `CGO_ENABLED=0`; Bridge links against libfido2 and libsecret
  and requires cgo at build time

## Bash Conventions

All shell scripts must follow these rules:

- start with:

```bash
#!/bin/bash
set -Eeuo pipefail
```

- pass `shellcheck -S style` with no warnings or errors
- use `find` instead of `ls` for file selection
- quote variable expansions
- use `[[ ... ]]` for Bash conditionals
- prefer `printf` over `echo` when escaping may matter
- declare function-local variables with `local`
- use `|| true` only when failure is intentionally acceptable
- do not silently suppress errors with bare fallback patterns
- validate required environment variables early in entrypoints; fail with a clear message rather than proceeding with an empty or missing value
- do not redirect subprocess stderr to `/dev/null` unconditionally; capture and log failures so errors are not silently lost
- add a max-retry limit to any indefinite retry loop; do not allow a service to loop silently on persistent failure — exit so the container restarts and the failure is visible

## Python Conventions

- Python version is `3.14`
- do not add `type: ignore` unless explicitly approved
- fix types properly instead
- MCP server code should remain async
- indexer is sync except where the watchdog/event loop requires otherwise
- local Python dependency management uses `uv`
- for `indexer/` and `mcp-server/`, treat `pyproject.toml` and `uv.lock` as the source of truth
- pin all new Python dependencies to exact versions in `pyproject.toml` and regenerate `uv.lock`
- always wrap multi-step database writes in an explicit transaction; roll back on any error rather than committing partial state
- never log API keys, passwords, or credential values in error messages or tracebacks; redact or omit before propagating to logs
- always set an explicit per-request timeout on outbound async HTTP calls; do not rely solely on client-level defaults for per-call deadlines
- do not hardcode LLM model names; use an environment variable with a pinned default string so the model can be updated without a code change

## Service Responsibilities

### `bridge/`

Purpose:

- runs ProtonBridge
- manages Bridge auth/keychain bootstrap
- provides IMAP/SMTP endpoints internally

Notes:

- runtime must support `pass` and `gpg`
- keep non-root operation intact
- preserve XDG path behavior

### `mbsync/`

Purpose:

- syncs Bridge mail into Maildir

Notes:

- this is the only container that should speak IMAP directly to Bridge
- keep sync pull-only
- preserve TLS cert extraction behavior
- do not add writeback behavior

### `indexer/`

Purpose:

- parses Maildir messages
- threads messages into conversations
- embeds content through Ollama
- writes SQLite, FTS5, and vector data

Notes:

- preserve thread-level indexing
- review schema implications before changing embedding or storage assumptions

### `mcp-server/`

Purpose:

- exposes mailbox tools over SSE and/or Streamable HTTP
- reads SQLite for retrieval/search
- performs mail actions when enabled

Notes:

- keep FastMCP-based implementation unless there is a strong reason to change it
- preserve read-only posture as the default design direction
- do not broaden direct access to Bridge
- keep `MCP_TRANSPORT=sse` as the default unless the owner asks to change the
  default client posture; use `dual` for Claude Desktop plus Open WebUI.

### `open-webui` optional overlay

Purpose:

- provides a local browser UI for Ollama-backed chat and MCP tools

Notes:

- defined only in `docker-compose.open-webui.yml`; do not add it to the default
  stack unless explicitly asked
- reuse the existing `ollama` service via `http://ollama:11434`
- connect to the MCP server via `http://mcp-server:3000/mcp`
- keep the UI bound to localhost only
- require the Open WebUI session key as a Docker Compose secret backed by
  `.secrets/open_webui_secret_key.txt` (consumed via `WEBUI_SECRET_KEY_FILE`);
  do not move it into `.env` or hardcode a real value
- leave signup enabled only for first local admin creation, then document or
  default toward `OPEN_WEBUI_ENABLE_SIGNUP=false`

## Testing Expectations

### General

- use `pytest` for Python services
- run `make typecheck` for mypy checks when Python service code changes
- run `pre-commit run --all-files` when practical before opening a PR or finalising a substantial change
- for Docker Compose or env wiring changes, run `docker compose config --quiet`
- for Dockerfile, build, or container-runtime changes, run the smallest relevant `docker compose build ...` subset when practical
- for Bridge build, patch, or version-bump changes, run `make bridge-upgrade-check`
- prefer real `.eml` fixtures for parser tests
- integration tests should mock IMAP rather than hitting a live Bridge instance
- add or update tests when behavior changes

### Coverage expectations

- both `indexer` and `mcp-server` enforce a 90% coverage floor via `--cov-fail-under=90` in each service's `pyproject.toml`; a PR that drops coverage below 90% will fail CI
- for `indexer`, coverage scope is `src/` with `src/main.py` omitted (service bootstrap is covered by docker-compose integration, not unit tests); for `mcp-server`, coverage scope is `src/lib` only — tool handlers, MCP framework wiring, and the main entrypoint do not yet have unit tests
- when widening `mcp-server` unit coverage into `src/tools` or `src/main.py`, expand the `source` list in `mcp-server/pyproject.toml` under `[tool.coverage.run]` rather than lowering the threshold
- CI runs `pytest --cov` in `.github/workflows/tests.yml` and uploads `coverage.xml` as an artifact per service

### Minimum expectations by area

- parser changes should add or update parser fixtures/tests
- threader changes should verify threading, subject fallback, references, and participant handling
- database changes should verify schema creation, migration, and upsert/query behavior
- MCP search changes should verify hybrid/RRF behavior where applicable
- before opening PRs that touch TLS, auth, logging, subprocess execution, or credential handling, run `bandit -r src/` and resolve any findings rated medium or higher (a CI job in `.github/workflows/security.yml` enforces this at medium+ severity for both services)

Run tests with:

```bash
cd indexer    && uv run pytest
cd mcp-server && uv run pytest
make typecheck
```

## Documentation Expectations

Update docs when changing:

- architecture
- setup and first-run flow
- TLS/cert handling
- MCP tool behavior
- schema or migration behavior
- environment variables
- repository workflows, security reporting flow, or contributor-facing automation
- operational recovery steps

If a change may stale `README.md`, `PLAN.md`, `docs/`, or `AGENTS.md`, update it or proactively suggest the follow-up. Contributor-facing defaults (`CONTRIBUTING.md`, `SECURITY.md`, issue/PR templates) live in the org-level `marshalltech81/.github` repo; flag follow-ups there when a change in this repo makes them stale.

## When to Stop and Ask

Stop and ask for direction before proceeding if a proposed change would:

- expose new host ports
- alter local-only/privacy expectations
- change auth or secret storage model
- change thread-level indexing
- change schema shape or embedding dimensions
- allow writeback sync to Proton
- remove TLS verification or other security controls
- replace the current Bridge build/runtime assumptions
- disable or weaken TLS verification in any service (`CERT_NONE`, `check_hostname = False`)
- suppress or remove credential redaction from any log or error path

## Out of Scope for Root AGENTS.md

The following should live in separate docs instead of this file:

- backlog items
- implementation queue
- future-project list
- one-time recovery procedures
- long troubleshooting walkthroughs

Suggested companion files:

- `docs/ops-notes.md`
- `docs/troubleshooting.md`
- `PLAN.md`

## Repository Map

```text
bridge/        ProtonBridge container
mbsync/        Mail sync container
indexer/       Parser, threader, embeddings, SQLite writer
mcp-server/    MCP server and tool layer
docs/          Architecture, setup, and tool documentation
```

## Bottom Line

Preserve privacy, preserve architecture, preserve secret safety, and make the smallest safe change.

When unsure, choose the more conservative implementation.

# AGENTS.md

## Purpose

This repository provides a privacy-first AI search and intelligence layer for ProtonMail.

The stack consists of four containers:

- ProtonBridge (container)
- mbsync (container)
- indexer (container)
- MCP server (container)

Inference and embedding are operator-supplied — the project itself
ships no model-serving components. The indexer and mcp-server speak
the OpenAI-compatible `/v1/embeddings` shape and (for OpenAI-mode
inference) the `/v1/chat/completions` shape, plus an Anthropic-compatible
Messages API for `INFERENCE_MODE=anthropic`. Whether the operator
points these at a remote provider or a host-side server they install
themselves (LM Studio, vLLM, mlx_lm.server, TEI, etc.) is a deployment
choice, not a project concern.

Core behavior:

- email storage, sync, and indexing stay local
- Bridge is the only path to Proton
- mbsync pulls mail into Maildir
- indexer parses and stores thread-level data in SQLite
- MCP exposes read-only search, retrieval, intelligence, and status tools over SSE and/or
  Streamable HTTP depending on `MCP_TRANSPORT`
- whether retrieved email content leaves the host depends on which
  embedder + inference endpoint the operator wires up

## Priorities

When making changes, follow these priorities in order:

1. Preserve privacy guarantees and the local-only deployment option.
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
- `docs/troubleshooting.md` before changing Bridge, mbsync, TLS, or recovery behavior
- `docs/mcp-tools.md` before changing MCP tool behavior

If a change touches container boundaries, TLS, Bridge auth, mbsync behavior, indexing strategy, or schema design, read the relevant docs first.

## Architecture Summary

High-level data flow:

1. ProtonBridge connects to ProtonMail.
2. mbsync pulls from Bridge into Maildir.
3. indexer parses Maildir messages, builds conversation threads, generates embeddings via an OpenAI-compatible `/v1/embeddings` endpoint (operator-supplied), and writes SQLite.
4. MCP server reads from SQLite and exposes tools over SSE and/or Streamable HTTP.
5. Only the MCP server is exposed to the host on `localhost:3000` by default.

Important architecture facts:

- the index is SQLite with FTS5 plus `sqlite-vec`
- retrieval is hybrid keyword plus vector search with RRF
- indexing is thread-level, not message-level
- MCP defaults to SSE transport; `MCP_TRANSPORT=streamable-http` enables
  Streamable HTTP, and `MCP_TRANSPORT=dual` serves both `/sse` and `/mcp`
- each operator-supplied layer (inference / embed / rerank) follows the
  same env-var shape: `{LAYER}_MODE` selects the SDK/protocol;
  `{LAYER}_BASE_URL` / `{LAYER}_MODEL` / `{LAYER}_API_KEY` configure
  the chosen mode. `mode=none` disables a layer. Missing required
  vars are a startup error — there is no inter-mode fallback.
- **The required-vars contract is uniform and intentional:** for any
  enabled layer, `{LAYER}_API_KEY` and `{LAYER}_MODEL` must be
  non-empty; `{LAYER}_BASE_URL` may be empty. An empty base URL
  means "use the SDK's documented default" — Anthropic Messages API
  for `INFERENCE_MODE=anthropic`, OpenAI proper
  (`https://api.openai.com/v1`) for `INFERENCE_MODE=openai`, OpenAI
  proper for `EMBED_MODE=openai`, Cohere API for `RERANK_MODE=cohere`.
  **The required `{LAYER}_API_KEY` is the explicit-intent signal**: an
  operator with a real `sk-...` in `.secrets/<layer>_api_key.txt` has
  unambiguously chosen their provider, so an empty base URL is
  interpreted as "I want the SDK default" rather than "I forgot to
  configure." A typo or forgotten env var can't ship inbox content to
  a remote provider because it can't produce a real bearer credential.
  Operators pointing at an unauthenticated host-side server (LM
  Studio, vLLM, `mlx_lm.server`, TEI) set `{LAYER}_BASE_URL` to the
  host endpoint and supply any non-empty placeholder string (e.g.
  `unauthenticated`) for `{LAYER}_API_KEY`; the compat server ignores
  the bearer header.
- inference is selected by `INFERENCE_MODE` (`anthropic|openai|none`).
  `anthropic` (default) uses the official `anthropic` SDK against the
  Messages API; leave `INFERENCE_BASE_URL` empty for the SDK default
  (`https://api.anthropic.com`). `openai` uses the official `openai`
  SDK against any OpenAI-compatible chat-completions endpoint: leave
  `INFERENCE_BASE_URL` empty for OpenAI proper, or set it to a host
  endpoint (LM Studio, vLLM, `mlx_lm.server` — containers reach a
  host-side server via OrbStack's `host.docker.internal`) or to an
  alternative provider (DeepInfra, OpenRouter, etc.). `none` skips
  registration of the intelligence tools.
- embeddings go through the official `openai` SDK against any
  OpenAI-compatible `/v1/embeddings` endpoint: leave `EMBED_BASE_URL`
  empty for OpenAI proper or set it to a host endpoint or alternative
  provider. Indexer + mcp-server must point at the same provider +
  model so query vectors are comparable to indexed vectors.
  `EMBED_MODE=openai` is the only valid value; embed has no disabled
  mode because semantic / hybrid search is the headline retrieval
  feature and the indexer cannot run without an embedder.
- reranking is opt-in (`RERANK_MODE=none` by default). `RERANK_MODE=cohere`
  uses the official `cohere` SDK against the Cohere rerank API; set
  `RERANK_MODEL` (e.g. `rerank-v4.0-pro`) and provide
  `RERANK_API_KEY`. Leave `RERANK_BASE_URL` empty for the SDK
  default; set it only for proxies / gateways / region overrides.

## Non-Negotiable Constraints

Do not make any of the following changes unless the repository owner explicitly asks for them.

### Platform and base image constraints

- Do not switch runtime images to Alpine.
- Do not introduce distroless images for Bridge.
- Do not add Qt dependencies to Bridge.
- Bridge must continue using `make build-nogui`.

### Network and exposure constraints

- Do not expose any container port other than `mcp-server:3000` to the host.
- When the operator points `EMBED_BASE_URL`, `INFERENCE_BASE_URL`,
  or `RERANK_BASE_URL` at a host-side server, that server should
  bind to `127.0.0.1` only. Containers reach it via
  `host.docker.internal`. Do not configure containers to use the
  host's LAN IP — `host.docker.internal` is required so the wiring
  survives changing networks.
- Do not add `network_mode: host`.
- Do not give `mcp-server` direct IMAP access to Bridge.
- Do not give `indexer` direct IMAP access to Bridge.
- mbsync is the only container that should talk directly to Bridge IMAP.
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
- Do not change the SQLite schema without incrementing `SCHEMA_VERSION`
  **and** shipping a forward migration file at
  `indexer/src/migrations/<NNNN>_<slug>.sql` covering the new version.
  Fresh installs apply `_apply_initial_schema` directly and stamp the
  current version; existing installs run the migration runner
  (`indexer/src/migrations/runner.py`) to catch up. Each migration runs
  in its own `BEGIN IMMEDIATE`/`COMMIT`, so a failure leaves the
  database stamped at the last successfully applied version and the
  next startup retries from the failing migration onward. Downgrades
  (stored version > code) and gaps (no migration file for an intermediate
  version) both fail closed at startup with actionable error messages.
  History through v21 is squashed into `_apply_initial_schema`
  (`SCHEMA_BASELINE_VERSION`); the first new migration is `0022`, and a
  database older than the baseline fails closed with rebuild
  instructions.
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
- When a change makes an attachment extractor return different text for
  the same bytes, bump that module's entry in
  `indexer/src/extractors/__init__.py` `EXTRACTOR_VERSIONS`. The
  extraction cache is otherwise served forever, so the fix would never
  reach mail indexed before it; with the bump, stale rows re-extract and
  the startup sweep re-queues the messages carrying them once.
  Dead-lettered messages are skipped and keep their stale chunks until
  an operator runs `make requeue-dead`.

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
- Bridge's vault default `AutoUpdate: true` is patched to `false` so the
  in-process auto-updater stays off. Without this patch Bridge fetches
  `proton.me/download/bridge/linux/x86/v1/version.json` on every startup,
  downloads the latest release (the Qt/GUI variant — exactly what
  `build-nogui` exists to avoid), and stages it under
  `/data/local/protonmail/bridge-v3/updates/<version>/`. The image ships
  no launcher, so a staged build is not executed, but the fetch is
  unpinned network traffic and leaves unpinned code on the data volume.
  The patch changes only the default for new vaults; vaults created
  before it keep `AutoUpdate: true` (#245). The patch is verified at
  three layers: source string-count guards in `bridge/patch-source.sh`,
  a synthetic `go test` against
  `internal/vault.newDefaultSettings` run during the build, and an
  end-to-end `"Vault loaded ... autoUpdate=\"false\""` log assertion in
  `scripts/bridge-smoke.sh`.
- Bridge v3 stores credentials and TLS cert material in `vault.enc`
- the cert is not baked into any image or persisted in a volume — but mbsync's
  entrypoint extracts it from a live connection with `openssl s_client` on
  every container start and writes it to a tmpfs file at
  `/tmp/mbsync/bridge-cert.pem` for the duration of the run
- mbsync cert extraction is done from a live connection with `openssl s_client`

Operational implications:

- if you touch Bridge build logic, TLS logic, auth storage, or XDG paths, review setup and recovery behavior first
- do not assume rebuilding Bridge updates the existing cached cert in `vault.enc`
- do not replace pass/gpg-based behavior with a weaker shortcut
- if a future change adds a fourth patch hunk to `bridge/patch-source.sh`,
  follow the existing three-layer pattern: pre/post `require_count` guards,
  add the touched package to `compile_patched_packages`, and (if the patch
  flips a runtime default rather than just a binding/string) add a `go test`
  assertion plus an end-to-end log/behavior check in `bridge-smoke.sh`

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
- Each operator-supplied layer has one Docker secret:
  `.secrets/inference_api_key.txt`, `.secrets/embed_api_key.txt`,
  `.secrets/rerank_api_key.txt`. Each file must exist with mode 600
  so the docker-compose `secrets:` reference resolves cleanly. The
  file must be **non-empty** when the matching layer's `*_MODE` is
  enabled (set to anything other than `none`); for unauthenticated
  host-side servers the operator supplies any placeholder string
  (e.g. `unauthenticated`) so the no-fallback startup contract holds
  uniformly. The file may be empty only when the matching layer's
  `*_MODE=none` (currently only `INFERENCE_MODE` and `RERANK_MODE`
  have a `none` mode; `EMBED_MODE` is always `openai`).
- Which `*_MODE` values exist, and which `*_BASE_URL` / `*_MODEL` /
  `*_API_KEY` each requires, is defined once in the Architecture
  Summary above; keep it there rather than restating it here.

### Commit hygiene

Before staging or committing, check for secrets:

```bash
git diff --staged | grep -iE '(password|pass|secret|token|key|credential)' | grep '^\+'
```

If a secret was committed, rotate it and remove it from git history using `git filter-repo`.

Do not rely on `git commit --amend` or interactive rebase for secret removal.

## Untrusted Mail Content

Every message and attachment is attacker-controlled input, and its
content is as private as a credential.

### Keep mailbox content out of logs and errors

- Never write mailbox content (bodies, subjects, names, addresses,
  attachment filenames and text), provider response fields, or
  content-bearing tool-argument values to logs,
  `indexing_jobs.last_error`, or exception messages that reach them.
  The credential redaction rules above do not cover this: the text is
  arbitrary, not a known secret. Tool arguments allowlisted in
  `mcp-server/src/lib/security.py` `_LOGGABLE_TOOL_PARAMS` may be
  logged when the value passes that field's own check (enums, numbers,
  booleans and ISO dates today); `log_tool_call` withholds everything
  else, including an allowlisted field whose value fails its check.
- At a provider-call boundary, log an SDK status error as its type plus
  status code. Keep the full text only of exceptions that cannot carry
  provider or mail data: connection and timeout errors, and our own
  fixed-message errors such as `EmbedResponseError`. Log anything else
  (response parsing, validation, conversion) as its type alone, since
  those errors quote the values they reject and a provider's response
  can echo the text sent to it. `scrub_embed_error` and the reranker
  follow this. In the MCP server, `safe_provider_exception_text`
  applies this rule for both the log line and the caller's
  `ToolError`; raise `ProviderResponseError` (both in
  `mcp-server/src/lib/security.py`) for a new fixed-message provider
  failure so its text is kept.
- Exceptions from the standard library's email parser and generator
  and from the codecs quote their input: `email.errors.HeaderWriteError`
  embeds the header it refused, `UnicodeEncodeError` its data, and a
  parser defect raised under a `raise_on_defect` policy
  (`FirstHeaderLineIsContinuationDefect` carries the offending line;
  `MessageDefect` is a `ValueError`, not a `MessageError`), and a
  codec lookup on a sender-supplied charset label raises `LookupError`
  with the label in its message. Catch `email.errors.MessageError`,
  `email.errors.MessageDefect`, `UnicodeError` and (at each decode
  site) `LookupError` at the boundary that produced them and degrade
  with a fixed message or a utf-8 fallback; never let them reach
  `indexing_jobs.last_error` through `_stage_error`.
- Every value written to `indexing_jobs.last_error` must be fixed
  text (with paths, sizes or counts at most), an exception type name,
  or the output of a formatter that enforces this: `scrub_embed_error`
  (`indexer/src/embedder.py`) for the embed stage and `_stage_error`
  (`indexer/src/main.py`, a short fixed-text allowlist, `OSError` from
  its errno, everything else by type) for the other stages. Some paths
  write fixed strings directly (for example the oversized and no
  `Message-ID` dead letters, the trashed-file defer and the queue's
  interruption marker); any direct write, existing or new, must follow
  the same rule. A
  query-path failure in the MCP server (an SQLite or conversion error)
  is logged and returned as its type name alone. Rows written before
  these rules (2026-10-01, #257) may still hold old text; nothing
  rewrites them. File any new leak you find as its own issue.
- Messages built from provider responses use fixed text and counts,
  never the returned values.
- A validation error quoting a tool argument may be returned to the
  caller, but log only the field name (see `InvalidFilterError`).
- Test with a synthetic marker: assert it is absent from `caplog` and
  from any persisted error.

### Bound the work per input

- Parsing, regex, and extraction work must be bounded per input
  (linear, or capped) before any size or character cap is applied to
  the result. A small crafted email or attachment must not be able to
  stall the single ingestion worker.
- A work bound covers every dimension the operation's cost depends
  on, in one guard. List them first (for `email.generator`: parts,
  header fields, header bytes, body bytes; for a regex: input length
  and match count) and budget them together, one counter per message.
  A guard that models one dimension of a multi-dimensional cost is a
  review round waiting to happen.
- Extractors must not let input trigger `RecursionError` or
  `MemoryError`: the dispatcher re-raises both as host pressure rather
  than recording a `failed` extraction.
- A fix gets a regression test with a synthetic worst case and a
  generous time bound, and the test must assert the work done (a call
  count, bytes copied, pages visited) as well as the result: elapsed
  time often cannot tell a bounded path from an unbounded one, and a
  test that checks only the output passes whether or not the work
  happened. Measure the worst case with a plain timing before sizing
  a bound; figures taken under a profiler (`cProfile`, `tracemalloc`)
  overstate it several times over.
- A review finding that calls for new parsing of untrusted input, or
  any new mechanism rather than a guard (a check, a cap, a fallback),
  is a design decision: stop and ask, with "document the limitation"
  as the first option. Do not build a parser inside a bug fix.

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
- preserve operator-controlled provider choice; do not silently force a particular embedder or inference endpoint

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

## Pull Requests and Review

- When a PR first lands, one test-first commit per issue, with a
  `Fixes #N` line for each issue the PR closes; behaviour-changing or
  schema-adjacent fixes get their own PR.
- Every push starts a Codex code and security review. A round is done
  when Codex's summary comment shows both rows completed on the head
  commit; a 👍 reaction means no findings. Replies you post to review
  threads also count as reviews on the head, so do not use "a review
  exists" as the signal.
- Verify each finding against the code before agreeing. Fix a review
  round's findings test-first in one commit for that round (they answer
  the same review, even when they touch different issues), and add a
  "Review round N" section to the PR description.
- Resolve a thread only once it is fixed or the owner has deferred it;
  merging is blocked while line threads are open.
- Merge (squash) only on the owner's explicit go-ahead.

## Common Commands

```bash
make build
make first-run
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
- do not add `type: ignore` in `src/` unless explicitly approved; fix
  types properly instead
- tests may use a code-scoped `# type: ignore[<code>]` for deliberate
  test doubles (monkeypatching an SDK, library or project object, such
  as replacing `client.embeddings.create` or a `Database` method) and
  for negative tests that pass an invalid type or mutate a frozen
  dataclass on purpose
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
- embeds content through an OpenAI-compatible `/v1/embeddings` endpoint
  (operator-supplied — remote provider or host-side server)
- writes SQLite, FTS5, and vector data

Notes:

- preserve thread-level indexing
- review schema implications before changing embedding or storage assumptions
- indexer and mcp-server must point at the same `EMBED_BASE_URL` +
  `EMBED_MODEL` so query vectors are comparable to indexed vectors —
  swapping the embedder requires a full reindex if the new model
  produces vectors of a different shape or distribution
- embed has no disabled mode; see the Architecture Summary for the
  provider contract

### `mcp-server/`

Purpose:

- exposes mailbox tools over SSE and/or Streamable HTTP
- reads SQLite for retrieval/search

Notes:

- the server is built on the standalone `fastmcp` package (pinned in
  `mcp-server/pyproject.toml`), not the official SDK's former built-in
  `mcp.server.fastmcp`; keep it unless there is a strong reason to change it
- every HTTP route sits behind `_HostOriginGuard` in `src/main.py`, which
  applies the MCP SDK's Host/Origin validator to the `_TRANSPORT_SECURITY`
  allowlist; do not remove it or swap in fastmcp's own guard, which
  accepts more Hosts and Origins
- Streamable HTTP apps must be built with an explicit
  `session_idle_timeout` (`MCP_SESSION_IDLE_TIMEOUT_SECS`); fastmcp's
  default never ends an idle session
- keep the server read-only: do not add mail-changing tools (send, move,
  flag, draft) without explicit owner approval
- do not give it any access to Bridge
- keep `MCP_TRANSPORT=sse` as the default unless the owner asks to change the
  default client posture; use `dual` only when a client needs both SSE and
  Streamable HTTP on the same localhost-bound port.

## Testing Expectations

### General

- use `pytest` for Python services
- run `make typecheck` for mypy checks when Python service code changes
- run `pre-commit run --all-files` when practical before opening a PR or finalising a substantial change; the `hadolint-docker` hook needs a running Docker daemon, so when Docker is down and no Dockerfile changed, run with `SKIP=hadolint-docker` and say so in the PR
- for Docker Compose or env wiring changes, run `docker compose config --quiet`
- for Dockerfile, build, or container-runtime changes, run the smallest relevant `docker compose build ...` subset when practical
- for Bridge build, patch, or version-bump changes, run `make bridge-upgrade-check`
- prefer real `.eml` fixtures for parser tests
- integration tests should mock IMAP rather than hitting a live Bridge instance
- add or update tests when behavior changes

### Coverage expectations

- both `indexer` and `mcp-server` enforce a 90% coverage floor via `--cov-fail-under=90` in each service's `pyproject.toml`; a PR that drops coverage below 90% will fail CI
- for `indexer`, coverage scope is `src/` with `src/main.py` omitted (it holds service bootstrap and the two-phase indexing pipeline; `tests/test_main.py` exercises the pipeline, but it does not count toward the figure); for `mcp-server`, coverage scope is `src/` with `src/main.py` omitted — tool handlers run against the `FakeMCPServer` stub in `tests/conftest.py`, and `main.py`'s testable helpers are unit-tested directly
- when coverage drops, add tests rather than lowering the threshold or widening `omit`
- CI runs `pytest --cov` in `.github/workflows/tests.yml` and uploads `coverage.xml` as an artifact per service

### Minimum expectations by area

- parser changes should add or update parser fixtures/tests; before
  rewriting a traversal, classifier or serializer, pin what the current
  code does on the shapes it handles with tests written against the
  revision being rewritten (`main` for a fresh PR, the PR head for a
  rewrite in a later review round), then state the new invariant in
  one sentence and test that
- a finding of the form "X differs from Y for input shape S" is fixed
  by class, not by shape: first build a differential check over a
  catalogue of shapes (plain, multipart, empty, 8-bit, folded and long
  headers, nested containers, and so on), measure which diverge, fix
  the class, and commit the catalogue as a parametrized test so the
  class cannot return one review round at a time. Where an exact path
  exists (a base64 decode is lossless), use it as ground truth for the
  lossy one
- threader changes should verify threading, subject fallback, references, and participant handling
- database changes should verify schema creation, migration, and upsert/query behavior
- MCP search changes should verify hybrid/RRF behavior where applicable
- mbsync entrypoint changes should update `mbsync/tests/entrypoint_test.sh`, which loads the real functions with external commands mocked
- Bridge entrypoint changes should update `bridge/tests/entrypoint_test.sh`, which does the same with a synthetic GPG keyring and pass store
- indexing, chunking, embedding-storage, or retrieval changes should pass `make baseline`; if ranking changes on purpose, regenerate the snapshot with `make baseline UPDATE=1` and explain the snapshot diff in the PR
- before opening PRs that touch TLS, auth, logging, subprocess execution, or credential handling, run `bandit -r src/` and resolve any findings rated medium or higher (a CI job in `.github/workflows/security.yml` enforces this at medium+ severity for both services)

Run tests with:

```bash
cd indexer    && uv run pytest
cd mcp-server && uv run pytest
make test-mbsync
make test-bridge
make baseline
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
- add hand-written parsing of attacker-controlled input (MIME, headers,
  document formats) where the standard library or an existing
  dependency already parses it, even to fix a review finding

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
docs/          Architecture, setup, troubleshooting, and tool documentation
```

## Bottom Line

Preserve privacy, preserve architecture, preserve secret safety, and make the smallest safe change.

When unsure, choose the more conservative implementation.

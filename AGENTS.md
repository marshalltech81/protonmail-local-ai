# AGENTS.md

## Purpose

This repository provides a privacy-first AI search and intelligence layer for ProtonMail.

The stack consists of the official Proton Mail Bridge app on the host
and three containers:

- Proton Mail Bridge (the official app, on the host, outside Compose;
  owner decision 2026-10-04: the Bridge container was removed)
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
- MCP exposes read-only search, retrieval, intelligence, and status tools over
  Streamable HTTP at `/mcp`
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

- `PLAN.md` for direction, roadmap and decisions; current work is in
  GitHub issues (milestones, `P0`–`P3` and `decision` labels)
- `docs/architecture.md` for system design and data flow
- `docs/setup.md` before changing Bridge, first-time setup, TLS, or credentials
- `docs/troubleshooting.md` before changing Bridge, mbsync, TLS, or recovery behavior
- `docs/mcp-tools.md` before changing MCP tool behavior

If a change touches container boundaries, TLS, Bridge auth, mbsync behavior, indexing strategy, or schema design, read the relevant docs first.

## Architecture Summary

High-level data flow:

1. The Proton Mail Bridge app on the host connects to ProtonMail.
2. mbsync pulls from Bridge (via `host.docker.internal`) into Maildir.
3. indexer parses Maildir messages, builds conversation threads, generates embeddings via an OpenAI-compatible `/v1/embeddings` endpoint (operator-supplied), and writes SQLite.
4. MCP server reads from SQLite and exposes tools over Streamable HTTP at `/mcp`.
5. Only the MCP server is exposed to the host on `localhost:3000` by default.

Important architecture facts:

- the index is SQLite with FTS5 plus `sqlite-vec`
- retrieval is hybrid keyword plus vector search with RRF
- indexing is thread-level, not message-level
- Streamable HTTP at `/mcp` is the only MCP transport (owner, 2026-10-02,
  #498); the legacy SSE transport and `dual` mode were removed, and
  `MCP_TRANSPORT=sse` or `dual` fails startup with migration steps
- each operator-supplied layer (inference / embed / rerank) follows the
  same env-var shape: `{LAYER}_MODE` selects the SDK/protocol;
  `{LAYER}_BASE_URL` / `{LAYER}_MODEL` / `{LAYER}_API_KEY` configure
  the chosen mode. `mode=none` disables a layer. Missing required
  vars are a startup error — there is no inter-mode fallback.
- **The required-vars contract is uniform and intentional:** for any
  enabled layer (inference when `INFERENCE_MODE` is not `none`, embed
  always, rerank when `RERANK_MODE` is not `none`), `{LAYER}_API_KEY`,
  `{LAYER}_MODEL` and `{LAYER}_BASE_URL` must be non-empty after
  trimming. **The destination is always explicit** (owner decision
  2026-10-05, #750): `{LAYER}_BASE_URL` is the endpoint URL, or the
  literal `default` (trimmed, case-insensitive) for the SDK's
  documented default endpoint — Anthropic Messages API
  (`api.anthropic.com`) for `INFERENCE_MODE=anthropic`, OpenAI proper
  (`https://api.openai.com/v1`) for `INFERENCE_MODE=openai` and
  `EMBED_MODE=openai`, Cohere API (`api.cohere.com`) for
  `RERANK_MODE=cohere`. An empty value is a startup error in
  `scripts/validate-env.sh`, the indexer and mcp-server, raised before
  any provider client is built, with fixed text naming the variable
  and both fixes. An API key is not the intent signal: the request
  body (mail text) reaches the provider before it checks the key, so a
  placeholder or stale key plus a forgotten URL would still ship mail.
  Disabled layers (`mode=none`) need no base URL. Operators pointing
  at an unauthenticated host-side server (LM Studio, vLLM,
  `mlx_lm.server`, TEI) set `{LAYER}_BASE_URL` to the host endpoint
  and supply any non-empty placeholder string (e.g. `unauthenticated`)
  for `{LAYER}_API_KEY`; the compat server ignores the bearer header.
- inference is selected by `INFERENCE_MODE` (`anthropic|openai|none`).
  `none` is the default (#750), so a fresh install sends nothing to an
  inference provider until one is chosen. `anthropic` uses the
  official `anthropic` SDK against the Messages API
  (`INFERENCE_BASE_URL=default` for `https://api.anthropic.com`).
  `openai` uses the official `openai` SDK against any
  OpenAI-compatible chat-completions endpoint: `INFERENCE_BASE_URL=default`
  for OpenAI proper, or set it to a host
  endpoint (LM Studio, vLLM, `mlx_lm.server` — containers reach a
  host-side server via OrbStack's `host.docker.internal`) or to an
  alternative provider (DeepInfra, OpenRouter, etc.). `none` skips
  registration of the intelligence tools.
- embeddings go through the official `openai` SDK against any
  OpenAI-compatible `/v1/embeddings` endpoint: `EMBED_BASE_URL=default`
  for OpenAI proper, or set it to a host endpoint or alternative
  provider. Indexer + mcp-server must point at the same provider +
  model so query vectors are comparable to indexed vectors; both check
  this at startup against the index's `vector_generations` record
  (`docs/architecture.md`, "Embedder identity record") and fail closed
  on a mismatch.
  `EMBED_MODE=openai` is the only valid value; embed has no disabled
  mode because semantic / hybrid search is the headline retrieval
  feature and the indexer cannot run without an embedder.
- reranking is opt-in (`RERANK_MODE=none` by default). `RERANK_MODE=cohere`
  uses the official `cohere` SDK against the Cohere rerank API; set
  `RERANK_MODEL` (e.g. `rerank-v4.0-pro`) and provide
  `RERANK_API_KEY`. `RERANK_BASE_URL=default` for the SDK default;
  set a URL for proxies / gateways / region overrides.

## Non-Negotiable Constraints

Do not make any of the following changes unless the repository owner explicitly asks for them.

### Platform and base image constraints

- Do not switch runtime images to Alpine.

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
- mbsync reaches the Bridge app on the host's loopback (`127.0.0.1`)
  through `host.docker.internal` (#497); the same no-LAN-IP,
  no-host-networking and no-published-port rules apply. Do not rebind
  the app beyond `127.0.0.1` or add `network_mode: host` to make it
  reachable (for example on Linux, which is not supported).

### Mail sync and safety constraints

- Do not change mbsync to write back to Proton.
- `mbsyncrc.template` must remain pull-only.
- Do not remove `Expunge None`.
- Do not add `All Mail`, `Labels/*` or the top-level `Starred` to mbsync Patterns.
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
  The initial schema is version 0: earlier history was squashed into
  `_apply_initial_schema` and renumbered, so the first new migration is
  `0001`. The initial schema stamps `SCHEMA_APPLICATION_ID` into the
  SQLite header; a database without it predates the renumbering and
  fails closed with rebuild instructions whatever its version.
  Until the first deployment, schema changes fold into the v0
  `_apply_initial_schema` with no migration file and no
  `SCHEMA_VERSION` bump (owner, 2026-10-01); a database built before
  such a change is rebuilt from Maildir. The first deployment was
  2026-10-03, so schema changes now take the `SCHEMA_VERSION` bump and
  migration file described above.
- Do not change embedding dimensions or model assumptions without verifying schema and context-window implications.
- Do not change chunk ID derivation away from the deterministic
  `sha256(message_pk || index || text)` shape — re-runs depend on identical
  inputs producing identical IDs so the diff-write path skips already-
  embedded chunks. Body chunks use the message's claimant ID as
  `message_pk`; attachment chunks use
  `message_pk = f"{claimant_id}::{attachment_id}"`.
- Every per-message row (`messages`, `message_thread_map`,
  `message_participants`, `message_chunks`, `attachments`,
  `pending_deletions`) is keyed by the claimant ID — the Message-ID plus
  `#` and the first sixteen hex digits of the SHA-256 of the file's raw
  bytes (`indexer/src/parser.py` `claimant_id`) — never by the bare,
  sender-controlled Message-ID, so two files claiming one Message-ID
  cannot overwrite or delete each other's rows (#217). Thread
  membership still resolves by Message-ID.
- Do not store raw attachment payload bytes in SQLite. The current schema
  keeps bytes only in the `.eml` on disk. ``attachment_extractions`` caches
  the extracted *text* per content hash and extractor module (#928) so
  OCR / parse cost runs at most once per unique payload and extractor,
  not the bytes themselves.
- When a change makes an attachment extractor return different text for
  the same bytes, bump that module's entry in
  `indexer/src/extractors/__init__.py` `EXTRACTOR_VERSIONS`. The
  extraction cache is otherwise served forever, so the fix would never
  reach mail indexed before it; with the bump, stale rows re-extract and
  the startup sweep re-queues the messages carrying them once.
  Dead-lettered messages are skipped and keep their stale chunks until
  an operator runs `make requeue-dead`. A bump also re-runs that
  module on every cached payload the startup sweep considers (the
  sweep does nothing while `INDEXER_ATTACHMENT_EXTRACTION_ENABLED` is
  off, and keeps `image-ocr` / `pdf-ocr` rows while OCR is off), so
  before bumping a module whose post-open walk is not yet budgeted,
  read the issue or PR that last chose not to bump it (#1036 declined a `docx` bump
  while #1031 is open; #1068 bumped it anyway and #1075 reverted it).
  A reverted bump leaves its number taken: a build in between may have
  stamped rows with it, and a row is never treated as stale by a
  lower-or-equal version, so the next `docx` bump goes to 7, not 6
  (`test_docx_rows_stamped_by_the_reverted_bump_are_kept` pins this).

## Bridge-Specific Guardrails

Bridge has special behavior and must be handled carefully.

Important facts:

- Bridge is the official Proton Mail Bridge app on the host, outside
  Compose (#497; the only mode since the Bridge container was removed,
  owner, 2026-10-04). It binds IMAP to the host's `127.0.0.1`; login,
  credentials and updates are managed in the app. `docker-compose.yml`
  sets mbsync's `BRIDGE_HOST=host.docker.internal` and
  `BRIDGE_CERT_HOST=127.0.0.1`. The app's certificate names only
  `127.0.0.1` and isync (1.5.1 in the image) checks Bridge's self-signed
  CA certificate against `Host`, so the entrypoint keeps `Host 127.0.0.1`
  and connects
  through an isync `Tunnel` (`socat`); implicit TLS and verification run
  end to end. Because the app's loopback port can be held by another
  local account while the app is down, mbsync never trusts on first use:
  the certificate must match the operator-supplied
  `BRIDGE_CERT_FINGERPRINT` (required; `validate-env.sh` and the
  entrypoint both refuse to start without it) on every start, before the
  persistent pin is consulted and before the password is sent. Never
  make it work by relaxing the check. Details: `docs/architecture.md`
  "Bridge"; checked by `mbsync/tests/entrypoint_test.sh`,
  `mbsync/tests/tls_check.sh` and `scripts/tests/compose_test.sh`.
- Platforms: tested on macOS (OrbStack, Docker Desktop), where
  `host.docker.internal` forwards to the host's loopback; Windows Docker
  Desktop is expected to work the same way but is untested; Linux is
  not supported out of the box, because `host-gateway` reaches the
  docker bridge address, not the loopback the app binds.
- mbsync reaches Bridge IMAP over implicit TLS (RFC 8314, Bridge's "SSL"
  mode), never STARTTLS or plaintext (#638, owner-approved 2026-10-02):
  no plaintext phase precedes the handshake. The operator sets the
  app's IMAP connection mode to SSL. mbsync has no STARTTLS fallback: a
  Bridge still serving STARTTLS fails closed at certificate extraction.
- the cert is not baked into any image or persisted in a volume — but mbsync's
  entrypoint extracts it from a live connection with `openssl s_client` on
  every container start and writes it to a tmpfs file at
  `/tmp/mbsync/bridge-cert.pem` for the duration of the run; only its
  SHA-256 fingerprint is persisted (the pin, in the `mbsync-state` volume)

Operational implications:

- if you touch mbsync's TLS logic, the fingerprint check or the pin, review
  setup and recovery behavior first

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
- `.secrets/mcp_auth_token.txt` (the MCP bearer token), and
  `.secrets/mcp_client_headers.txt` if an older setup left one
- `mbsync/bridge-cert.pem`
- any `.pem`, `.key`, `.p12`, or `.pfx` file
- `config/authority.toml` (the operator's source-authority rules: real
  addresses and domains; only `config/authority.toml.example` with
  `.example` domains is tracked)
- any ad hoc export containing credentials, tokens, or private keys

### Credential-specific rules

- `BRIDGE_USER` comes from the Bridge app's IMAP details, not the Proton
  account password
- `BRIDGE_PASS` (the app's IMAP password) belongs in
  `.secrets/bridge_pass.txt`, not `.env`. `BRIDGE_CERT_FINGERPRINT` is
  not secret and belongs in `.env`
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
- The MCP bearer token is the Docker secret
  `.secrets/mcp_auth_token.txt` (mode 600, always non-empty;
  `make init-secrets` generates it). `validate-env.sh` rejects a missing,
  empty or non-600 file, a token shorter than 32 characters or outside
  the RFC 6750 b64token set (mcp-server startup,
  `scripts/mcp-auth-headers.sh` and the stdio adapter apply the same
  set), and an
  `MCP_AUTH_TOKEN` in `.env`; the
  `MCP_AUTH_TOKEN` env fallback is for running the server outside a
  container only, never the Compose path. Never log the token or the
  `Authorization` header, and never document or script a client setup
  that passes the token as a command argument (other local accounts can
  read the process list): use a file or stdin, as
  `scripts/mcp-auth-headers.sh` (Claude Code's `headersHelper` and
  Codex's `http_headers_helper`) and `mcp-server/src/stdio_adapter.py`
  (Claude Desktop's stdio adapter, which also checks the file is mode
  600) do. Do not recommend third-party stdio bridges such as
  `mcp-remote`.

### Commit hygiene

Before staging or committing, check for secrets:

```bash
git diff --staged | grep -iE '(password|pass|secret|token|key|credential)' | grep '^[+]'
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

### Make degraded behaviour visible

The services log at INFO; a DEBUG line is not visibility.

- Every fallback, cap, skip, retry or partial result that changes what
  is indexed or answered logs at INFO or above (WARNING when it lowers
  quality or loses data), with counts, fixed text, type names and
  config values only, as the rules above require. When per-item lines
  would flood the log, log an aggregate per interval or per call.
- A tool call that degraded says so on its own log line (the timing
  line), not only in a standalone warning that cannot be joined to it.
- An error raised to the caller (including `ToolError`) is logged with
  its fixed-text cause, not only `outcome=error`.
- A recovery is logged as well as the failure (a breaker that closes,
  a retry that succeeds), so an outage has a visible end.
- A healthcheck or status field must not report a component as live
  when a thread or dependency it relies on has stopped.
- A per-item WARNING that untrusted mail or a remote client can trigger
  repeatedly (an extraction failure, a cap, a rejected request) goes
  through a rate limiter from the start, with the suppressed count kept
  in an aggregate line; an unbounded per-item line lets a crafted
  stream evict the bounded Docker log history.
- Tests assert the line appears with the expected counts, and that a
  synthetic mail marker does not.

### Do not export mail in bulk

Mailbox content lives in two places: the Maildir volume and the
SQLite index volume. Do not copy mail out of them in bulk (bodies,
headers, attachment text, or files derived from them, such as candidate
lists, labels or spreadsheets) onto the host or anywhere else for
testing, evaluation or analysis without the owner's explicit consent
for that run (owner, 2026-10-05). A `/tmp` dump of every inbox body
is exactly what this forbids.

- Analyse through the MCP tools and keep results in memory; screen
  on the server with `query_messages` filters (`text`, `sender`, dates)
  rather than downloading every body to screen locally.
- With consent, write only where the owner agreed, with mode 700
  directories and 600 files, delete the data when the run ends, and
  report what was written and that it was removed.
- Reading mail also sends it to the model: say how much mail a task
  will read before reading it in bulk, and prefer the smallest sample
  that answers the question.
- Committed tests and fixtures stay synthetic, as above; never derive
  them from real mail.

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
- A third-party parser whose cost cannot be bounded in-process (it
  allocates or loops on input before our code runs) runs in a child
  process launched through `indexer/src/extractors/_runner.py`
  `run_tool`. It enforces a wall-clock timeout and an output cap, and
  starts the tool through `_launcher.py`, which sets `RLIMIT_AS` and
  `RLIMIT_CPU` to the limits the caller passes before the parser loads;
  every caller passes both, sized by a plain measurement of the tool
  in the image (#995). Measure a
  candidate library on crafted input with plain timing and RSS before
  choosing it: two `.xls` readers failed that test (#935).
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
- make a new fallback, cap, skip or retry visible in the logs (see "Make degraded behaviour visible")
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
- `fix(mbsync): require the Bridge cert fingerprint on every start`
- `docs(setup): add mbsync verification steps`
- `chore(pre-commit): add detect-secrets baseline`
- `style: apply pre-commit autofixes across repo`

## Issue Conventions

Every open issue carries, on GitHub (owner, 2026-10-07):

- exactly one **type** label: `bug` (behaves wrongly or unsafely),
  `enhancement` (new or changed behaviour, including measurement that
  leads to it), `documentation`, `test` (a test, fixture or eval-harness
  gap; nothing shipped changes) or `chore` (tooling, CI, build,
  housekeeping, or an investigation with no product change)
- exactly one **priority** label, `P0`–`P3`, as the review rules above
  use them
- one or more **area** labels, `area/mbsync`, `area/indexer`,
  `area/parser`, `area/extractors`, `area/database`, `area/mcp-server`,
  `area/mcp-tools`, `area/eval`, `area/docker`, `area/ci`,
  `area/tooling`, `area/docs`, matching the commit scopes below
- one **milestone**: a PLAN.md phase, *Corpus and contract follow-ups*
  (Phase 0 and 1 follow-ups), *Operations and hardening* or *Evidence
  model*
- flags where they apply: `decision` (waits on an owner choice between
  stated options) and `security` (privacy, secrets, exposure, TLS,
  supply chain or bounded-work robustness, whatever the type)

Each label's description on GitHub repeats its rule. Dependabot PRs
are labelled `dependencies` as their type plus the area the update
lands in, as `.github/dependabot.yml` sets them.

Relations between issues are GitHub relations, not only prose, set
when the issue is filed or when the relation is found, with the
reason in the body's Relationships section: a dependency is a
"blocked by" relation, and a part or follow-up of a larger issue
(a "Parent:" or "Follow-up to" line, open or closed parent) is a
sub-issue of it. A `Refs` line is context, not a relation.

Titles are `<component>: <what is wrong | what will be true>`, or
`Decision: <the choice, naming the component>`. The component is the
noun a reader would grep for (a service, module, tool name, format or
surface), more specific than the area label. A bug states the observed
behaviour in the present tense; an enhancement states the outcome.
`Decision:` is the only status word allowed; no type, priority, phase
or issue number goes in a title. Sentence case, no trailing period,
under about 80 characters.

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
- Cap review at two fix rounds per PR (owner, 2026-10-01). A finding
  raised in round three or later is verified, filed as its own issue,
  linked from a reply on its thread, and the thread resolved as
  deferred; the PR is then ready for the owner's merge go-ahead once
  CI is green (the go-ahead rule below still applies).
- Exception (owner, 2026-10-05, #751): a verified round-three-or-later
  finding still blocks the merge when it is P0/P1, or when the PR
  itself introduced it (in its first commit or any fix round), at any
  severity. Fix it in the PR, revert the change that caused it, or have
  the owner accept it explicitly as a stated risk, recorded in the PR
  description and a linked issue. A P2/P3 finding the PR did not
  introduce keeps the cap. Record which applied in the "Review round N"
  section. A gap or bug in code, tests or docs that the PR adds counts
  as introduced by it even when nothing is broken today (owner,
  2026-10-07); only a problem that already existed on `main` may be
  deferred under the cap.
- Re-scope trigger (owner, 2026-10-07): from the fourth review round
  on, whenever a round finds problems in the code or text the PR
  added, stop fixing and ask the owner before the next push: cut scope (drop or simplify
  the part that keeps drawing findings), accept the open findings as
  stated risks, or keep fixing. Explain each finding in plain terms,
  and say whether the choice can change what the tools return.
  Choosing to fix one round does not accept later rounds' findings.
  An agent working the PR stops and reports instead of pushing.
- Owner decisions (owner, 2026-10-08): every decision or recommendation
  brought to the owner (a re-scope trigger, a `decision` issue, a
  choice between options) first goes to a two-reviewer panel, Claude
  Fable 5.1 and Codex GPT-6.1 Sol, both at high reasoning effort,
  reviewing read-only against the code, with a round in which each
  answers the other. Recommend the option that gives the most
  accurate outcome, with its cost and any split the panel could not
  settle, for the owner to approve. Briefs carry code and design
  only, never mailbox content.
- File P3 findings as issues rather than fixing them ahead of
  go-live or P1/P2 work. Exception (owner, 2026-10-02): a small P3
  with an agreed fix and no new mechanism may be fixed before go-live.
- Every item in a PR's "Not done" section that is a real remaining gap
  (a limitation, an unverified claim, a deferred finding, a skipped part
  of the issue, a follow-up the code needs) gets its own GitHub issue
  before the PR is reported ready, unless an open issue already tracks
  it; write that issue's number next to the item (owner, 2026-10-07).
  Items that only explain a choice (no docs changed because none apply,
  a check not run because nothing it covers changed) need none. The
  issue holds fixed text, options and links, never mailbox content.
- A pre-existing problem found while working (a review finding the PR
  did not introduce, a bug seen while reading code, a flaky or wrong
  test) that is not fixed in that PR gets its own GitHub issue at the
  time, unless an open issue already tracks it (owner, 2026-10-07). A
  review thread is resolved as pre-existing, out of scope or deferred
  only with that issue's link in the reply, and the PR's "Not done"
  lists it. Nothing found stays only in a thread, a PR body or a
  commit message.
- Merge (squash) only on the owner's explicit go-ahead.

## Common Commands

```bash
make build
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
- give every optional setting's Compose interpolation a default (`${NAME:-}` or the documented value), so a `.env` written before the setting existed does not print an "is not set" warning on every command (#1058, #1074). Required settings stay as bare `${NAME}`; Compose only warns and substitutes an empty string for those, so the requirement is enforced by `scripts/validate-env.sh` (which `make up` runs first) and by each service's own startup check, never by Compose

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

### `mbsync/`

Purpose:

- syncs mail from the Bridge app on the host into Maildir

Notes:

- this is the only container that should speak IMAP directly to Bridge
- keep sync pull-only
- preserve TLS cert extraction behavior
- `BRIDGE_HOST`, `BRIDGE_IMAP_PORT` and `BRIDGE_CERT_HOST` are validated
  as a plain host name or IPv4 address and a port before they reach
  `mbsyncrc` or the tunnel's shell command
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

- exposes mailbox tools over Streamable HTTP at `/mcp`
- reads SQLite for retrieval/search

Notes:

- the server is built on the standalone `fastmcp` package (pinned in
  `mcp-server/pyproject.toml`), not the official SDK's former built-in
  `mcp.server.fastmcp`; keep it unless there is a strong reason to change it
- every HTTP route sits behind `_HostOriginGuard` in `src/main.py`, which
  applies the MCP SDK's Host/Origin validator to the `_TRANSPORT_SECURITY`
  allowlist; do not remove it or swap in fastmcp's own guard, which
  accepts more Hosts and Origins
- the Streamable HTTP app must be built with an explicit
  `session_idle_timeout` (`MCP_SESSION_IDLE_TIMEOUT_SECS`); fastmcp's
  default never ends an idle session
- `/mcp` requires the static bearer token (owner, 2026-10-02):
  `_build_app` sets `_StaticBearerTokenVerifier` (a fastmcp
  `TokenVerifier` comparing with `hmac.compare_digest`) as
  `server.auth`, so `http_app` wraps the `/mcp` route in fastmcp's
  `RequireAuthMiddleware` and a rejected request gets 401 before any
  session exists; `/health` stays unauthenticated, and startup fails
  closed on an empty token. Do not swap in fastmcp's
  `StaticTokenVerifier`, whose dict lookup is not constant-time, and do
  not remove or bypass the check. The trust condition is "processes
  running as the operator are trusted": the token stops other local
  accounts and browser pages, not code running as the operator
- keep the server read-only: do not add mail-changing tools (send, move,
  flag, draft) without explicit owner approval
- every tool passes `annotations=read_only("<Title>")` from
  `src/tools/outputs.py` (#899): `readOnlyHint=true`,
  `destructiveHint=false`, `openWorldHint=false` and its own title. A
  tool that would change mail or state, or reach arbitrary external
  entities, needs explicit owner approval and its own classification.
  `tests/test_tool_annotations.py` enumerates every tool through a real
  client, so add a new tool there deliberately
- do not give it any access to Bridge
- keep Streamable HTTP at `/mcp` as the only transport; do not reintroduce
  the legacy SSE transport or a dual mode without explicit owner approval.

## Testing Expectations

### General

- use `pytest` for Python services
- run `make typecheck` for mypy checks when Python service code changes
- run `pre-commit run --all-files` when practical before opening a PR or finalising a substantial change; the `hadolint-docker` hook needs a running Docker daemon, so when Docker is down and no Dockerfile changed, run with `SKIP=hadolint-docker` and say so in the PR
- for Docker Compose or env wiring changes, run `docker compose config --quiet`
- for Docker Compose or shell script changes, run the Semgrep job from
  `.github/workflows/security.yml` locally:
  `uvx --from semgrep==1.179.0 semgrep test .semgrep`,
  `uvx --from semgrep==1.179.0 bash scripts/tests/semgrep_paths_test.sh` and
  `uvx --from semgrep==1.179.0 semgrep scan --metrics=off --strict --error --config .semgrep/compose.yaml --config .semgrep/shell.yaml .`.
  The rules in `.semgrep/` encode the hardening and exposure rules
  above and cover every `docker-compose*.yml` and `compose*.yml`
  overlay (`.yaml` too) and `*.sh` file.
  Fix a finding; allow-list one only with owner approval, as a
  `# nosemgrep: <rule-id>` comment on the reported line with the reason
  beside it. A rule change gets matching cases in its fixture
  (`.semgrep/compose.test.yml`, `.semgrep/shell.sh`).
- for dependency (`pyproject.toml`, `uv.lock`, `pom.xml`) or Dockerfile
  changes, run `make trivy`: the Trivy jobs from
  `.github/workflows/security.yml` locally (the dependency scans of
  `indexer/` and `mcp-server/` and the offline misconfiguration scan of
  the repository, with the workflow's flags; needs `trivy` on `PATH`,
  and warns when its version is not the pinned one), then the image
  gates of `.github/workflows/docker.yml` (the vuln scans of the built
  indexer, mcp-server and mbsync images, fixable HIGH/CRITICAL only;
  `make trivy-images` runs these alone). The image gates need the
  images `make build` last produced, named as `docker compose build`
  names them, and fail naming the image when one is not built; rebuild
  first, since they scan the image, not the checkout (they warn, without
  failing, when an image's `org.opencontainers.image.revision` label is
  missing or differs from the checkout's commit, and always on a
  `-dirty` checkout, #1103). The flag values
  live in the Makefile and the workflows; `make test-trivy-flags` (part
  of `make test`, no Trivy or Docker needed) fails when they differ, so
  a change to one is made in both.
- for Dockerfile, build, or container-runtime changes, run the smallest relevant `docker compose build ...` subset when practical
- prefer real `.eml` fixtures for parser tests
- a fixture generated with an office application (Word, PowerPoint,
  LibreOffice) records the creating user in its metadata (OLE2
  SummaryInformation, `docProps/core.xml`); set a synthetic author and
  check the file before committing it (#980)
- tests that need a process to die from a signal use `SIGKILL`; map
  other signals (`SIGSEGV`, `SIGABRT`, `SIGXCPU`) with a stubbed exit
  status, and run a real crash signal only on Linux in CI. A real
  `SIGSEGV` on macOS files a crash report and shows the developer a
  "Python quit unexpectedly" dialog on every run (2026-10-07)
- integration tests should mock IMAP rather than hitting a live Bridge instance
- add or update tests when behavior changes

### Coverage expectations

- both `indexer` and `mcp-server` enforce a 90% coverage floor via `--cov-fail-under=90` in each service's `pyproject.toml`; a PR that drops coverage below 90% will fail CI
- for both `indexer` and `mcp-server`, coverage scope is all of `src/`, `src/main.py` included (#757): the indexer's `main.py` holds the two-phase indexing pipeline (exercised by `tests/test_main.py`), and the MCP server's holds the auth and app wiring; tool handlers run against the `FakeMCPServer` stub in `tests/conftest.py`. A path that genuinely cannot run under test gets a narrow `# pragma: no cover` with its reason, not a file-wide exclusion
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
- a list that must cover every item of some kind (settings in a config
  hash, registered MCP tools and their annotations) gets a test that
  derives the items from the code and fails on any it does not cover,
  with an explicit, reasoned exclusion list; a finding of the form
  "the list is missing X" is fixed by adding that test, not only X
- threader changes should verify threading, subject fallback, references, and participant handling
- database changes should verify schema creation, migration, and upsert/query behavior
- MCP search changes should verify hybrid/RRF behavior where applicable
- mbsync entrypoint changes should update `mbsync/tests/entrypoint_test.sh`, which loads the real functions with external commands mocked; changes to mbsync's TLS or connection settings should also pass `make test-mbsync-tls` (the shipped image against a synthetic implicit-TLS server)
- changes to `mbsync/mbsyncrc.template` should pass `make test-mbsync-layout` (the shipped image's isync against synthetic Maildir stores: folder layout, and spurious and genuine UIDVALIDITY changes with the documented recovery)
- a base-image digest bump in `indexer/Dockerfile` or `mcp-server/Dockerfile` (Dependabot's or by hand) moves `PYTHON_IMAGE` in `mbsync/tests/tls_check.sh` with it; `scripts/tests/image_pin_test.sh` (`make test-image-pins`, also in CI) fails while the three differ, since Dependabot does not update the script. The same test fails while `.github/workflows/docker.yml` and `.github/workflows/tests.yml` pass `docker/setup-buildx-action` different `driver-opts: image=moby/buildkit:...` references (#1122): Dependabot does not track a driver option, so a BuildKit bump moves both by hand, together
- Compose changes that touch service selection, dependencies, hardening or ports should keep `scripts/tests/compose_test.sh` passing; it also checks the required hardening on the merged config of every overlay combination the Makefile uses, and that every base service starts with no profile active, so a new overlay, combination or profile-activating target is added to its list
- indexing, chunking, embedding-storage, or retrieval changes should pass `make baseline`; if ranking changes on purpose, regenerate the snapshot with `make baseline UPDATE=1` and explain the snapshot diff in the PR
- a PR that adds or removes baseline-corpus messages also regenerates the parser pin (`cd indexer && PARSER_PIN_UPDATE=1 uv run pytest tests/test_parser_pin.py`); its diff must be only the added or removed records, plus the corpus messages after the change renumbered under new file names with no other field changed, since corpus files are numbered in sequence (#1124)
- `ask_mailbox` or `summarize_thread` prompt or answer-path changes can be compared with the opt-in `make eval-answers` / `make eval-answers-compare` (synthetic corpus only, calls the configured `INFERENCE_*` and `JUDGE_*` providers, never in CI; see `mcp-server/tests/eval/README.md`)
- before opening PRs that touch TLS, auth, logging, subprocess execution, or credential handling, run `bandit -r src/` and resolve any findings rated medium or higher (a CI job in `.github/workflows/security.yml` enforces this at medium+ severity for both services)

Run tests with:

```bash
cd indexer    && uv run pytest
cd mcp-server && uv run pytest
make test-mbsync
make test-mbsync-tls
make test-mbsync-layout
make test-compose
make baseline
make typecheck
```

The `make` targets that run `uv` export `UV_CACHE_DIR` as `.uv-cache/`
under the checkout (ignored by git, the service `.dockerignore` files
and Semgrep), so parallel checkouts or worktrees never share one cache
(#896); each fills its own once. Set `UV_CACHE_DIR` explicitly to
override it.

## Documentation Expectations

Update docs when changing:

- architecture
- setup and first-time setup flow
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
- replace the current Bridge runtime assumptions (the official app on
  the host, reached over implicit TLS with a required fingerprint)
- disable or weaken TLS verification in any service (`CERT_NONE`, `check_hostname = False`)
- suppress or remove credential redaction from any log or error path
- add hand-written parsing of attacker-controlled input (MIME, headers,
  document formats) where the standard library or an existing
  dependency already parses it, even to fix a review finding

## Out of Scope for Root AGENTS.md

The following should live elsewhere instead of this file:

- backlog items and the implementation queue: GitHub issues
- roadmap, future-project list and owner decisions: `PLAN.md`
- one-time recovery procedures
- long troubleshooting walkthroughs

Suggested companion files:

- `docs/ops-notes.md`
- `docs/troubleshooting.md`
- `PLAN.md`

## Repository Map

```text
mbsync/        Mail sync container
indexer/       Parser, threader, embeddings, SQLite writer
mcp-server/    MCP server and tool layer
docs/          Architecture, setup, troubleshooting, and tool documentation
```

## Bottom Line

Preserve privacy, preserve architecture, preserve secret safety, and make the smallest safe change.

When unsure, choose the more conservative implementation.

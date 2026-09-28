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

**The mailbox is an immutable personal knowledge corpus.** The system
transforms it into a searchable, explainable, temporally aware
knowledge base while preserving a direct path from every conclusion
back to the original source.

Two invariants govern all work:

1. **Never force the user to trust an AI conclusion when they can
   verify it directly against the underlying source.**
2. **Raw sources are authoritative and immutable; every
   interpretation is a versioned, reproducible, disposable derived
   artifact.** Deleting every index, vector, summary, or derived
   claim must always leave the system rebuildable from the source
   corpus.

Sequencing principle: **do not build a durable knowledge layer until
the corpus is provably complete, the retrieval contract is truthful,
and every synthesis traces back to source evidence.** Corpus
correctness → truthful contract → swappable pipeline → measurement →
knowledge layer.

This direction was adopted 2026-09-26 after two independent
clean-room code reviews (Claude, ChatGPT) converged on the same
findings, and it **supersedes** the former scope line that froze the
MCP API surface. Adding `query_messages`, structured outputs, and
message-first-class retrieval objects is now in scope; see Phase 1.

"Immutable" means source content is never mutated beneath derived
knowledge — not that users can never delete. Deletion/retention
semantics are an explicit roadmap item (Phase 4).

## Current State

The stack runs four containers:

- **ProtonBridge** — Docker, headless, IMAP/SMTP on `bridge-net` only.
- **mbsync** — Docker, pulls into Maildir, `chmod go+r` after each
  sync, TOFU cert pinning with explicit rotation flag.
- **indexer** — Docker, parses Maildir, threads, embeds via any
  OpenAI-compatible `/v1/embeddings` provider (operator-supplied),
  writes SQLite. Schema v21 (v20 squashed baseline plus
  `ingestion_state`): 4096-dim L2-unit-norm
  vectors, `NOT NULL` `message_chunks.message_date`,
  `indexing_jobs.last_error_class`, per-message `messages` +
  `message_participants`. Initial scan and steady-state both
  drain a durable `indexing_jobs` queue through one two-phase batched
  path (Phase 1 commits thread membership with a three-case
  seed-vector chain; Phase 2b batch-embeds; Phase 2c commits chunks /
  attachments / final thread vector per message transactionally).
- **mcp-server** — Docker, hybrid five-lane search (thread FTS, chunk
  FTS, attachment FTS, thread vec, chunk vec → RRF, optional Cohere
  rerank), exhaustive `query_messages` enumeration, and intelligence
  tools. `INFERENCE_MODE=anthropic` (default,
  `claude-sonnet-4-6`) or `openai`; SSE / streamable-http / dual
  transports; localhost:3000 only.

Inference and embedding endpoints are operator-supplied — the project
ships no model-serving components. Host-side servers keep retrieval
traffic on the box; remote providers cross that boundary. Both
postures documented, neither provisioned.

## Roadmap

### Phase 0 — Corpus integrity and security invariants

**Exit criterion:** every source that exists in the synchronized
Maildir eventually reaches one visible terminal ingestion state, and
transient infrastructure failures never cause permanent source
omission.

1. ~~**Watcher-before-drain.**~~ Done 2026-09-26 (see Recently
   Completed).
2. ~~**Periodic Maildir reconciliation.**~~ Done 2026-09-26 (see
   Recently Completed).
3. ~~**Failure taxonomy.**~~ Done 2026-09-26 (see Recently
   Completed).
4. ~~**Batch failure isolation.**~~ Done 2026-09-26.
5. ~~**Outage circuit breaker.**~~ Done 2026-09-26.
6. ~~**`requeue-dead` command.**~~ Done 2026-09-26.
7. ~~**Robust untrusted-content serialization.**~~ Done 2026-09-27.
8. ~~**Logging privacy.**~~ Done 2026-09-27.
9. ~~**Fix `MCP_PORT` wiring.**~~ Done 2026-09-27.
10. ~~**Pin Bridge source by commit SHA.**~~ Done 2026-09-27.
11. ~~Small carryover (health threshold, fenced JSON).~~ Done 2026-09-27.

### Phase 1 — Truthful MCP contract

**Exit criterion:** an unfamiliar LLM can query the corpus without
guessing about semantics, completeness, or identity.

1. ~~**`query_messages`**~~ — Done 2026-09-27 (see Recently
   Completed).
2. ~~**Structured MCP output**~~ Done 2026-09-28 (see Recently
   Completed).
3. ~~**Message-first-class.**~~ Done 2026-09-28 (see Recently
   Completed). Received date deferred (see Deferred).
4. ~~**Honest `get_mailbox_status`**~~ Done 2026-09-28 (see Recently
   Completed).
5. **Bearer-token auth on the MCP endpoint** (promoted from the old
   "evaluate" backlog item). Localhost topology alone is not a trust
   boundary against other local processes.
6. ~~**Delete the dead action/IMAP surface.**~~ Done 2026-09-28 (see
   Recently Completed).
7. **Source integrity exposure.** Formalize
   `source_id / sha256 / source_type / original locator / ingested_at
   / size` in retrieval and evidence responses — `indexed_files`
   already captures content hash and identity; this is exposure, not
   new capture. Evidence resolves answer → evidence → chunk →
   source_id → immutable raw object.
   *Progress (PR #175): each message's source locator, size, and
   SHA-256 are now stored per message and kept current across
   renames. Remaining: exposing them in retrieval / evidence
   responses.*
8. **Doc-drift sweep**: architecture.md search section describes
   three retrieval lanes (code has five) and a pre-chunks-era table
   list; README overclaims ("Agentic", "Real-time", "any compliant
   provider"); extract setup.md's troubleshooting half into
   `docs/troubleshooting.md`; fold in the surviving Bridge doc nits
   (Gluon-cache backup caveat, upgrade-check failure guidance,
   bridge-v4 vault-path warning).

### Phase 1.5 — Minimal regression baseline

Before the Phase 2 refactor, not after:

- deterministic synthetic mailbox (also satisfies the no-real-PII
  fixture constraint)
- 20–30 golden retrieval/evidence questions with expected-thread /
  Recall@K assertions, including a few attachment and evidence cases

Phase 2 must demonstrate behavior preservation against this baseline.

### Phase 2 — Swappable embedding / vector generations

**Exit criterion:** changing embedding models never requires altering
the canonical source corpus or its schema — vector storage is a
disposable, regenerable index.

1. **`vector_generations` registry**: generation_id, provider, model,
   revision, dimensions, tokenizer, chunk_config_hash, created_at,
   status. Dimension read from metadata, never a constant; pass the
   `dimensions` request param where the provider supports it.
2. **Per-generation vec tables** (`vec_chunks_gNN` — sqlite-vec bakes
   dimension into DDL, so per-generation tables are structurally
   required). Blue/green lifecycle: build → validate (against the
   Phase 1.5 baseline) → activate atomically → retain old generation
   → rollback if needed.
3. **Stage-aware pipeline manifest.** One active generation
   operationally, but the identifier is not opaque: a canonical
   manifest records parser / normalizer / chunker / embedding
   identity + config hashes, and
   `pipeline_config_hash = sha256(canonical_manifest)`. This
   preserves the ability to decide later whether a change means
   re-embed only, rechunk + re-embed, or reparse + rechunk +
   re-embed — without building parser/chunk generation coexistence
   machinery now.
4. **Chunk `kind` tags** (body / quote / signature / forwarded /
   calendar) as chunker metadata, with the invariant that **semantic
   segmentation happens before chunking** — a chunk never spans
   kinds. `quoting.py` already segments pre-chunk; this codifies it
   and lets the embedder skip/deprioritize by kind. No
   `content_blocks` table until a non-chunking consumer needs one.

### Phase 3 — Measurement and product vertical slice

**Exit criterion:** we can objectively measure whether the system
answers real knowledge questions, and identify why failures occur.

1. **Agent-level evals** on the synthetic mailbox: tool-selection
   accuracy, argument accuracy, retrieval recall, citation accuracy,
   pagination completeness, unnecessary-call counts. Extends the
   existing `eval-queries.md` / `scripts/eval_run.py` approach.
2. **Latency instrumentation before performance redesign.** Stage
   timers through the query path (query_embedding / per-lane FTS+KNN /
   fusion / rerank / evidence_fetch / inference / total). `ask_mailbox`
   already exceeds 60s client timeouts on a populated mailbox — but
   measure before touching KNN architecture; if inference dominates,
   vector work won't fix the user problem. Then set request-level
   deadlines. (Project history endorses this: the 400s search hang
   was three wrong theories until the query plan was measured.)
3. **Experimental ephemeral `brief_issue`.** Chronology, actors,
   positions, decisions, open questions, conflicting evidence — every
   assertion cited, **nothing persisted**. This is the proving ground
   for what a durable ontology should eventually contain; its
   failures drive Phase 4.
4. **Adversarial injection suite** (hostile fixtures in the synthetic
   mailbox, asserting the Phase 0 serialization holds under real
   tool flows).
5. Unparked by the eval harness:
   - **Thread-vector weighting** — attachment chunks currently
     dominate the thread-vector mean (a 50-chunk PDF on a 5-chunk
     thread is ~91% of the coarse vector). Options: keep
     mean-of-all / weight body vs attachment / cap per-source
     contribution. Decide from eval data distinguishing
     "asked about the PDF" vs "asked about the email" queries;
     touches `get_thread_chunk_embeddings` and the reconciler's
     survivor-mean path.
   - **Rerank value experiment** — `RERANK_MODE=cohere` vs `none` on
     the golden set.

### Phase 4 — Deterministic knowledge scaffolding

1. **Entity resolution, phase 1 (deterministic):** address
   canonicalization, display-name clustering, domain→organization
   mapping; `entities` / `entity_aliases` relational tables.
   LLM-*suggested* merges are gated on human confirmation — never
   auto-merge on model say-so.
2. **Source authority metadata:** `source_type` / `authority_class`
   as explicit, filterable metadata with provenance. Deterministic
   where possible (sender domain → counsel/management/vendor;
   document type for governing docs), classifier-assigned with
   confidence otherwise. **Never silently folded into ranking
   weights** — authority is contextual; reasoning distinguishes "a
   vendor represented X" from "the executed agreement states X".
3. **Richer temporal retrieval:** capture and expose
   occurred_at/sent_at consistently; bitemporal claim modeling waits
   for Phase 5.
4. **Deletion/retention semantics.** Define the product policy:
   mirror mode (upstream delete → corpus delete; the opt-in
   reconciler is the existing seed), archive mode (retain locally),
   user-controlled retention. Provenance must define behavior when a
   citation's source is reaped (evidence row retained, source marked
   unavailable, chain never silently broken).

### Phase 5 — Knowledge reasoning

1. Hardened `brief_issue` (informed by Phase 3 usage).
2. Support / contradict / qualify / supersede analysis as a
   query-time tool ("here is a conclusion for the Board packet —
   find evidence that supports, contradicts, qualifies, or
   supersedes it").
3. Temporal position/change reasoning ("position as of date X" vs
   "current position").
4. **Only then** evaluate persisted claims/events — and only under
   these rules:
   - every cited span is **verbatim-verified against chunk text at
     write time**; fabricated quotes are rejected mechanically
   - derived claims are **never re-indexed as retrieval content**
     (no self-confirmation loop; retrieval touches sources only)
   - claims carry generation + model identity and are
     **bulk-disposable** by generation
   - claims are **always rendered with their source quotes**
   - derived knowledge may help locate or organize evidence, but it
     **never satisfies an evidentiary requirement by itself** — the
     chain must resolve to a source object
   Experiments before that may use a quarantined `candidate_claims`
   store: non-searchable, non-authoritative, disposable, excluded
   from downstream answers.

Rationale for the deferral: persisting LLM-extracted claims converts
prompt injection from a per-query annoyance into durable
knowledge-base poisoning (a crafted email minting a `decision` row
every future brief cites). The mailbox contains adversarial-capable
input by definition.

## Maintenance backlog (small, ongoing)

- consolidate `BRIDGE_VERSION` to a single source of truth
  (`.env.example`); parameterize the Go toolchain as an `ARG`
- `timeout-minutes` + path filters on `.github/workflows/docker.yml`
- Trivy scan of the Bridge Go module graph in `security.yml`
- pin `actions/checkout` to a commit SHA in `bridge.yml`; pinned
  `setup-go` in the patch-drift job
- fix the `\t\t` BSD-sed portability bug in `bridge/patch-source.sh`
- mbsync: move `BRIDGE_USER` to a file-backed secret; add log
  rotation + memory/CPU limits; evaluate runtime package pinning
- resource limits for the remaining Compose services
  (`protonmail-bridge` first — it holds live Proton credentials)
- loud one-shot startup warning when `INFERENCE_MODE` sends retrieved
  excerpts to a remote provider
- `get_message`: returns a message's full body and headers with no
  bound, so one huge message (a pasted log, 12,000 References) is one
  huge response; decide on body continuation (offset paging) or a
  documented cap — fits alongside Phase 1 item 2's structured output
- mcp-server: remove the dead `Database.get_thread_message_ids` (no
  callers outside its tests)
- IDs are unbounded: a root Message-ID becomes the thread ID with no
  length check, and IDs cannot be cut in responses without breaking
  chaining. Decide on a parse-time length limit (Message-IDs are
  ≤998 characters per RFC 5322 line length) or a hashed thread ID
- AGENTS.md commit-hygiene secret check: `grep '^\+'` fails under
  ugrep (a common `grep` alias); use the portable `grep '^[+]'`

## Not doing (decided 2026-09-26)

Recorded so items are auditable rather than silently dropped. Each
can be revisited with an explicit owner decision.

- **Mail-changing action tools** (`send_email`, `move_message`,
  `mark_read`, `flag_message`, `create_draft`, `reply_to_thread`).
  Read-only is part of the trust model, not a temporary deficiency:
  "it can understand your history, but it cannot send or delete
  anything." The never-registered code was deleted 2026-09-28.
- **Live-IMAP retrieval fallback for mcp-server.** A stale index
  answers "the index is not current" — it never silently switches
  data sources. (This was the last consumer argument for the deleted
  `imap.py`.)
- **Per-session inference-mode toggle.** Complexity with no pull;
  per-deployment is enough.
- **Guarded live Bridge integration CI.** Requires a dedicated paid
  Proton test account and hardens the ingestion edge rather than the
  product. The synthetic mailbox (Phase 1.5/3) buys more quality per
  hour. The old design notes live in git history if ever revived.
- **The bulk of the former Bridge ops list** (~25 items: BATS tests
  for entrypoint.sh, custom seccomp profile, OCI image labels,
  smoke-test ownership / `bash --version` / version-string checks,
  `# syntax` parser directive, assorted doc micro-items). Bridge is
  the project's existential *liability* — three sed patches on a
  vendor's source, on their release schedule — so it gets
  maintenance, not investment. The items that survived are in the
  Maintenance backlog; the doc-shaped ones folded into Phase 1's doc
  sweep. Boundary principle: the knowledge system knows nothing
  about Proton Bridge — the stable contract is
  Bridge → mbsync → **Maildir** (product boundary) → indexer, which
  leaves room for Maildir/mbox import or other mail connectors later
  without touching the knowledge architecture.
- **mypy → pyright migration** — unchanged policy: wait for a real
  trigger (mypy slowness, a missed bug, cross-project friction), not
  a pre-emptive sweep.

## Deferred (not dead — revisit on a real trigger)

- audio/video transcription via Whisper (model storage + compute
  cost; needs a clear use case)
- near-duplicate handling; saved queries / persistent monitors;
  waiting-on-me / unanswered-thread views — cheap *after*
  `query_messages` + knowledge scaffolding exist; premature now
- `content_blocks` as a persisted table (pull-not-push; see Phase 2
  item 4)
- parser/chunk generation coexistence machinery (the pipeline
  manifest preserves stage identity meanwhile)
- extracting the Bridge container into a standalone repo (only
  relevant if generic-IMAP decoupling is pursued)
- attachment download support (needs the read-only action-path
  decision it was always gated on)
- per-message received date: the sender controls `Date:`, so a
  trustworthy timeline needs the receiving server's timestamp (top
  `Received:` header; Maildir mtime is sync time, not delivery). Needs
  a schema bump plus a re-parse of every `.eml`, so land it with the
  Phase 2 reindex or when Phase 4/5 temporal reasoning needs it. First
  verify Bridge-delivered messages keep `Received:` headers (sent mail
  likely has none)

## Operational baseline (unchanged)

- Python services use per-service `uv` projects with pinned
  `pyproject.toml` + `uv.lock`. Both meet a 90% coverage floor in CI.
- Bridge built from upstream Proton source via `make build-nogui`; a
  patch-drift check + smoke test gate version bumps.
- All long-running services run as non-root with `cap_drop: ["ALL"]`,
  `no-new-privileges`, read-only root filesystems, `pids_limit`, and
  `init: true`.
- Bridge password lives in `.secrets/bridge_pass.txt` (Docker
  Compose secret), never `.env`. `make first-run` uses
  `logging: driver: none` to keep credentials out of Docker logs.
- Deletion reconciliation is opt-in (`INDEXER_DELETION_ENABLED=true`)
  with a grace window, mass-delete brake, and atomic
  reap-or-rollback.
- A durable `indexing_jobs` queue retries transient failures with
  exponential backoff and dead-letters persistent ones for operator
  visibility.

## Known limitations

- initial sync may take a long time on large mailboxes
- the schema is effectively locked to 4096-dim
  Qwen3-Embedding-8B-shaped models (hardcoded dim, no stored model
  identity, vendored tokenizer) — Phase 2
- audio/video attachment transcription is not supported; PDF / DOCX /
  XLSX / HTML / TXT / images are extracted, chunked, and searchable
- `list_threads(filter_type=...)` rejects unsupported values cleanly;
  unread/flagged state remains unindexed
- deletion reconciliation is opt-in and not yet validated under
  long-running real-world conditions; `INDEXER_UNLINK_ON_REAP=true`
  only removes the `.eml` when Maildir is mounted read-write
- coverage scope: `indexer/src` omits `src/main.py` (which has grown
  to hold the whole two-phase pipeline — re-scope when touching it);
  `mcp-server` coverage is `src/lib` only

## Blockers and Risks

### Initial Proton sync duration
Large mailboxes may take hours before useful indexing begins.
Do not assume indexing bugs until Bridge internal sync has completed.

### Bridge TLS and cert behavior
Bridge cert behavior is tied to `vault.enc` and patched SAN handling.
Do not modify this casually.

### Schema sensitivity
Changes to SQLite schema, embedding dimensions, or thread model can
invalidate existing assumptions and stored data. Phase 2 exists to
confine embedding-model changes to disposable vector generations.

### Knowledge-layer poisoning
Any future persisted derived knowledge inherits prompt-injection risk
from attacker-controlled email. The Phase 5 rules are the control;
do not ship persisted claims without them.

## Resolved decisions

1. **Read-only policy surface (resolved 2026-09-26):** the MCP server
   is fully read-only — no action tools, no SMTP path, no draft/move/
   flag operations. The unregistered action/IMAP code was deleted
   (2026-09-28) rather than maintained as hypothetical capability.
2. **Live Bridge integration lane (resolved 2026-09-26):** not doing
   (see Not doing).

## Open decisions

1. Default deletion/retention mode when Phase 4 lands (mirror vs
   archive as the shipped default).
2. Whether `brief_issue` debuts as an MCP tool or a host-side script
   during its Phase 3 experimental period.

## Recently Completed

### 2026-09-28 — Honest `get_mailbox_status` (Phase 1 item 4)

`get_mailbox_status` replaces `get_index_status` and `get_sync_status`
(and `make status` uses its helper). It returns `current` with one
reason per failed condition: mbsync completed a sync within three
intervals (5-minute floor), the indexer reported within 10 minutes,
and nothing is pending or retrying. Dead messages are reported but do
not block `current`. It also returns the last sync time, indexer
liveness, queue counts (pending / retrying / dead), and the existing
counts and date range, all from one read snapshot. mcp-server still
reads only SQLite: after each successful sync mbsync writes
`.mbsync-last-sync.json` at the Maildir root (after the permissions
fix-up, so that sync's deliveries are already queued), and the indexer
copies it with its own timestamp into the one-row `ingestion_state`
table at most every 30 s, from the main loop and from each drain pass,
so it keeps reporting through the initial index. Schema v21, first
post-squash migration `0021_ingestion_state.sql`. mbsync now rejects a
non-integer `SYNC_INTERVAL` at startup. Known gap: a missed filesystem
event is invisible to `current` until the periodic rescan enqueues it.

### 2026-09-28 — Structured MCP output (Phase 1 item 2)

The eleven search / retrieval / evidence / status tools declare a
Pydantic output model (`src/tools/outputs.py`) as their `outputSchema`
and return it as `structuredContent` beside the unchanged prose, so IDs
(thread → message → attachment) and paging state (`next_offset`,
`next_cursor`, `total_matches`) are typed fields. Structured output
keeps the prose's bounds on sender-controlled headers, with full counts
alongside. Failures are now raised and reach clients as `isError`
results instead of success results carrying error prose. Previously
FastMCP had wrapped every `list[TextContent]` return as
`{"result": [...]}` structured content, a second copy of the prose;
the intelligence tools still do. A contract test drives every tool
through a real `FastMCP` and validates each result against its
published schema. `pydantic` is now a declared dependency (same pinned
version `mcp` already resolved). No schema change. Review round 1: the
structured `query_messages` rows newly exposed In-Reply-To and
References with no length cut (a 2 MB response at `limit=1`); every
header value in its prose and structured output is now cut at 500
characters, as in `get_thread`, which also closes the backlog item for
its unbounded subjects and display names. Review round 2 (Codex
security review): `get_thread` repeated the sender-controlled thread ID
(the root Message-ID) on every message row, and `search_emails` /
`list_threads` / `search_attachments` listed 10 uncut participants or
senders where the prose shows 2–3. Message rows now carry `thread_id`
only where they can span threads (`query_messages`, `get_message`), and
the shared builders cut by default, with `get_message` the only
full-value caller.

### 2026-09-28 — Dead action/IMAP surface deleted (Phase 1 item 6)

Removed the never-registered mail-changing tools (`tools/actions.py`),
the live Bridge IMAP/SMTP client (`lib/imap.py`), their tests and
`FakeIMAP` fixture, and the `aioimaplib` dependency. `MCP_READ_ONLY`
is gone from `main.py`, compose, `.env.example`, and
`validate-env.sh`: it only chose a log line, and a flag that suggests
the server can be made writable contradicts the read-only decision.
`get_sync_status` lost its never-enabled Bridge reachability probe
(`bridge_enabled`); mcp-server is not on `bridge-net`, so the probe
could never have worked. mcp-tools.md moves the action tools to a
design-only appendix and renumbers System to Group 4; README, setup,
architecture, and AGENTS.md now describe the server as read-only
rather than "read-only by default".

### 2026-09-28 — Message-first-class retrieval (Phase 1 item 3)

`get_message` renders the message's own headers from `messages` +
`message_participants` — subject, every From / To / Cc entry, UTC send
date, folder, In-Reply-To, References, attachment flag — instead of
parent-thread participants and date range. `get_thread` renders the
thread's messages oldest first (`sent_at`, then `message_id`), each
with its own headers (recipients capped at 10 per role) and its body
reconstructed from its own body chunks in one grouped query; the
accumulated `body_text`, which repeats quoted replies, is shown only
when no message body is indexed yet. Received date is not captured by
the parser or schema; deferred (see Deferred). No schema change.
Review round 1: `get_thread` is bounded (message pages of 10, max 50,
with a next-`offset` line; bodies cut at 4,000 characters with an
omitted-count marker, and chunks past the cut are never read — the
first version loaded every chunk of every message, where the old
`body_text` was capped at 4,000 tokens); bodies are rebuilt by
`char_start` offset so the chunker's deliberate overlap is not shown
twice (also a pre-existing `get_message` bug); and both tools read
thread, messages, participants, and bodies in one read transaction.
Review round 2: header content bypassed that bound (12,000 folded
References rendered 550K characters); `get_thread` now lists at most 10
References and thread participants and cuts every header value at 500
characters with a marker, leaving full headers to `get_message`.

### 2026-09-27 — `query_messages` (Phase 1 item 1)

New MCP tool enumerating every message that matches all given
predicates — sender / recipient (To or Cc) / participant, subject
substring, body `text` (every word, FTS with stemming, words may span
chunks; attachments and stripped quotes excluded), folder, inclusive
`sent_at` bounds, attachment flag — with an exact `total_matches`,
`returned` range, `has_more`, and a cursor. Newest first, keyset-paged
on `(sent_at, message_id)` with a row-value predicate that seeks
`idx_messages_sent` (the OR expansion sorted every earlier row: 34 ms
vs 0.06 ms at 200K messages); cursors are bound to a digest of their
filters and rejected, not silently restarted, when reused elsewhere.
Count, page, and participants come from one read snapshot. A full
address matches canonically through the `(address, role)` index;
anything else is a Unicode case-insensitive substring of address or
display name (a `mcp_lower` SQL function, since SQLite's `lower()` is
ASCII-only), and the response names the mode used. `find_contact` now
aggregates `message_participants` instead of parsing every thread's
participant JSON, reporting every display name a contact was written
with; its `senders_only` mode (used by `search_emails(from_name=...)`)
keeps aggregating `threads.senders`, the exact set that search's sender
filter checks, so resolution can never pick an address the filter rejects. `search_emails` /
`list_threads` descriptions route "all" / "how many" questions to the
new tool. Review round 1: rejected-input errors are no longer logged
with the input they quote; empty pages state `returned: 0` /
`has_more: false`. `text` is split into words by FTS5's own unicode61
tokenizer (a throwaway in-memory table read through `fts5vocab`), so
query words always match the index's boundaries: three rounds of
hand-rolled splitting each drifted (combining marks, NFC-composing
Greek and Hangul, underscores, private-use characters), and every drift
silently changed exhaustive counts. No schema change.

### 2026-09-27 — Phase 1 foundation: per-message records

Schema v20 (baseline, folded while no deployed database exists):
`messages` (one row per indexed message: headers, `sent_at`, folder,
source `filepath` / `size_bytes` / `content_hash`) and
`message_participants` (normalized From / To / Cc, indexed by canonical
address). Written in `upsert_thread`'s transaction; removal cascades
from `message_thread_map`, so reaps and thread deletes need no new
code. Review round 1 fixed two pre-existing gaps it exposed: the parser
re-quotes display names when they contain RFC 5322 specials (a
`"Doe, Jane" <addr>` recipient previously lost its quotes and failed
canonicalization, dropping it from participants — thread-level lists
included), and the watcher's rename fast path records the new folder on
cross-folder moves. Round 2: the quoting uses an encoding-free formatter,
not `formataddr` (which raised on non-ASCII addresses, failing the whole
message, and RFC 2047-encoded Unicode names); raw 8-bit address headers
are decoded before splitting; and the folder is written inside
`update_filepath`'s transaction so a failed move rolls back whole and
stays recoverable by the Maildir walk. Round 3: a malformed encoded-word display
name falls back to its raw text instead of dead-lettering the message,
and From is parsed structurally into `from_addrs` (every author), so an
encoded sender name with a comma or a multi-author From no longer loses
the sender. A 27-case table-driven address corpus (parse -> index, end to
end) then found one more pre-existing loss: Python 3.14's strict
`getaddresses` rejects a whole header containing an empty list element
(`a@x, , b@x` or a trailing comma), dropping every recipient; the parser
now recovers such headers by removing the empty elements and
strict-parsing again. Round 4: display names decode only their RFC 2047
encoded-word tokens (linear scan, adjacent words joined, names over 998
chars kept raw), leaving existing Unicode untouched; a token that fails
or decodes to invalid Unicode (lone surrogate, NUL charset label) keeps
its raw text. Rounds 5-6 removed a `strict=False` lenient fallback that
had briefly replaced that recovery: it raised `RecursionError` on deeply
unmatched comment parentheses and ran in quadratic time on large rejected
headers; all parsing is strict again. Round 7: decoded names containing
control characters (a CR let `Mallory@...\r` be read as the address)
keep their raw text; `_format_address` enforces an identity invariant (if
the serialized string parses to a different address, the name is
dropped); and blank-element cleanup is a linear, escape-aware scan that
never touches quoted strings, comments, or domain literals (the regex
version rewrote `"a, ,b"@x` into a different mailbox). Round 8 replaced the
patchwork with one design: encoded-words are swapped for opaque
placeholders before structural parsing (their contents can never become
syntax — recursion, quadratic groups, and a fabricated
`bob@example.com_` sender all traced to that), the list is split at top
level in one linear escape-aware pass (groups flattened, empty elements
dropped), each element is parsed strictly on its own under size budgets
and fails safe, and decoding is bounded per encoded-word rather than per
name. `messages(filepath)` is indexed for the rename path. Round 9 closed the
last reparse hole: a restored encoded-word could re-create a
nested-paren bomb in the address and blow up the unguarded identity
re-parse (dead-lettering the message). Restore/decode/format now run
inside the per-element failure boundary, and every emitted address must
re-parse to itself (`parseaddr` fixed point, ≤998 chars) or be
discarded. `canonical_addr` and mcp-server's `find_contact` entry parse
are also guarded — hostile strings that reach them via the `from_addr`
fallback or already-indexed participants degrade to "no address" instead
of aborting threading or breaking every contact lookup.
Prerequisite for Phase 1 items 1 (`query_messages`), 3
(message-first-class retrieval), and 7 (source integrity exposure).

### 2026-09-27 — Phase 0 hardening (items 7–11); Phase 0 complete

- **Untrusted-content serialization (7).** One helper,
  `_untrusted_email_block`, builds every `<untrusted_email>` block and
  escapes any delimiter-shaped text in the content (case- and
  spacing-insensitive), so a hostile subject, participant, or body can
  no longer close the fence early. Injection fixtures push a hostile
  thread through all three intelligence tools and assert exactly one
  real closing tag with the injected text inside it.
- **Logging privacy (8).** All ten mcp-server tool handlers log through
  `log_tool_call`: content-free parameters plus the *names* of withheld
  ones (query, addresses, folders, IDs, schema never reach logs); an
  allowlisted field's value is logged only if it passes that field's
  check (enum member, integer, boolean, ISO date), since LLM-supplied
  arguments arrive unvalidated (review round 1). The
  find_contact error no longer echoes `from_name`. Indexer stage errors
  persist `Type: message` via `_stage_error`, never `repr()` — a
  `UnicodeDecodeError` repr embeds the decoded email bytes.
- **`MCP_PORT` (9).** Compose maps `${MCP_PORT}` to `${MCP_PORT}`; a
  non-default port previously pointed at a container port nothing
  listened on.
- **Bridge commit pin (10).** Proton's release tags are lightweight, so
  there is no tag signature to verify; `BRIDGE_COMMIT` pins the commit
  and the clone step fails unless `BRIDGE_VERSION` resolves to it. The
  weekly bump workflow resolves and moves the commit with the version
  across all three pin sites; `validate-env.sh` checks its format.
  `bridge-patch-drift.sh` verifies the same pin right after its clone —
  it runs before the image build in `make bridge-upgrade-check`, and its
  patch helper compiles and `go test`s upstream code on the host
  (review round 1).
- **Carryovers (11).** Extraction unwraps ```json-fenced model output
  instead of silently skipping the thread. Indexer health threshold
  90 s → 600 s, and the heartbeat is refreshed before each health probe
  and each isolated message, bounding the silent window to one embed
  request's retry cycle (~6.5 min).

### 2026-09-26 — Failure taxonomy, isolation, outage breaker, requeue-dead (Phase 0 items 3–6)

Phase 0 exit criterion met: transient infrastructure failures can no
longer cause permanent source omission. Every queue failure now
records `last_error_class` (`retryable` / `permanent_source_failure` /
`operator_action_required`; schema v19). When a
batch embed fails, a one-string health probe separates an outage from
a bad input: an outage defers every in-flight job without spending
attempts and opens a circuit breaker (30 s doubling to 10 min, shared
by the initial drain and the main loop); a healthy probe re-embeds
each message alone, so good batchmates index in the same pass and
only the bad input is charged (5xx on that input → retries; outright
rejection → immediate `permanent_source_failure`). A passing probe
only describes one tiny request, so every failure during isolation is
attributed by `classify_embed_failure` (separate from the HTTP retry
predicate): transport / 408 / 429 and 401 / 403 / 404 defer the
remaining messages and open the breaker; only 400 / 413 / 422 are
terminal; 5xx re-probes before charging an attempt (review round 1
found a mid-isolation 401 or persistent 429 could dead-letter valid
mail). A rejected multi-input request is re-sent one input per request
before anything is charged, so a provider request-size limit below
`EMBED_BATCH_SIZE` can't dead-letter valid mail either (review round 2). 429 and 408 now
count as transient in `_is_transient_embed_error`, so rate limits
back off instead of reading as misconfiguration. The same change squashed
migration history through v19 into `_apply_initial_schema` (no
deployed database existed): migrations `0013`–`0018` and the v14
destructive-migration guard (`INDEXER_MIGRATION_V14_FORCE`) were
deleted, `SCHEMA_BASELINE_VERSION = 19` marks the new floor, and older
databases fail closed with rebuild instructions. The runner stays for
future migrations, starting at `0020`. With no pre-v18 rows possible,
`message_chunks.message_date` became `NOT NULL`, the indexer's NULL
backfill was removed, and mcp-server's `COALESCE(message_date,
chunked_at)` legacy fallback was dropped. `make requeue-dead
[CLASS=...]` (`src/requeue_dead.py`) replaces the raw-SQL rescue.
Pausing during an outage also pauses Phase 1, so new mail is not
keyword-searchable until the embedder returns (deliberate: avoids
hammering a down provider).

### 2026-09-26 — Ingestion completeness (Phase 0 items 1–2)

The watchdog observer now starts before the initial drain, so mail
delivered during a long initial index is enqueued instead of
invisible until the next restart. The Maildir walk was extracted
into `_enqueue_unindexed_messages` and now also runs periodically
(on `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS`, reason `rescan`) as the
eventual-completeness backstop for missed events. The walk skips
indexed, dead-lettered, **and already-queued** files — the last is a
behavior change for the startup scan too: a restart no longer resets
an in-flight retry cascade to zero attempts (previously a crash-
looping container could retry a failing file forever without it ever
reaching `dead`). Messages the parser rejects outright (no
Message-ID) are now dead-lettered with `unindexable: no Message-ID`
instead of having their row deleted: they were never marked indexed,
so the periodic walk would otherwise re-parse them every interval
forever, and they previously ended in no visible state at all.
With deletion reconciliation enabled, every enqueue path (startup
scan, periodic rescan, watchdog new-delivery branches) skips
`T`-flagged files; otherwise a reaped message — whose `.eml` stays
on disk under `INDEXER_UNLINK_ON_REAP=false` — was resurrected into
search by the rescan (also by restarts and by mbsync flag renames,
both pre-existing). Caught in review; covered by
`TestReapedMessagesStayDeleted` (reap → rescan → drain, restart,
flag rename, upstream undelete). Tests: `TestEnqueueUnindexedMessages`,
`TestMainStartupAndLoop` (first `main()` wiring coverage; the
ordering test was mutation-checked against the old ordering).

### 2026-09-26 — Direction adoption + roadmap rewrite

Two independent clean-room code reviews (Claude, ChatGPT) converged:
retrieval engine and hardening are strong; corpus-integrity defects
(watchdog blind window, batch dead-lettering), MCP contract gaps
(no enumeration primitive, prose-only output), and embedding-model
lock-in are the priority. This PLAN.md was rewritten around the
knowledge-corpus objective. Stale backlog entries swept in the same
pass: "consider an attachments-search MCP tool", "attachment-aware
retrieval with provenance", and "dual retrieval units" (all shipped
as `search_attachments` / `get_evidence` / the chunk lane on
2026-05-15); "reference `make init-secrets` in README/setup" and
"replace `echo -n` with `printf` in setup.md" (already done in docs).

### 2026-05-15 — get_evidence + search_attachments

Owner-directed retrieval pair on the existing thread + chunk +
attachment tables, no schema change. `get_evidence` returns ranked
evidence chunks with thread/message/attachment provenance, char
offsets, and dates — no LLM synthesis; `thread_id` scopes to one
thread. `search_attachments` locates attachments via two FTS lanes
(filename/MIME + extracted text) with structured filters.
`get_message` reconstructs per-message bodies from the chunk store.
`search_emails` gained a `participant` filter. RRF fusion records
per-lane provenance (`lane_ranks`) as additive observability.

### 2026-05-13 — RAG quality: quote stripping + retrieval followups

Inline-reply quote stripping rewritten two-pass (`On … wrote:` reply
headers no longer hard-cut inline answers), extended to five
non-English attribution shapes, Gmail wrapped headers, and Outlook
bare `From:/Sent:` blocks (`test_quoting.py`, 573 indexer tests
passing). Codex-flagged retrieval gaps fixed: evidence ranking now
carries attachment provenance and floats attachment chunks when the
thread won via attachment-FTS; `summarize_thread` merges accumulated
body with a recent-chunk tail so long threads keep their newest
replies; the retrieval eval requires a real embedder instead of
silently degrading to keyword-only; `semantic_search` fuses the
chunk-vec lane. One finding deliberately parked pending eval data:
thread-vector weighting across body vs attachment chunks (now Phase
3 item 5).

### 2026-05-08 — Operator-supplied providers + pipeline unification

`mlx-service/` / `mlx-lm-server/` removed — the project ships no
model-serving components; operators supply an OpenAI-compatible
embedder and choose Anthropic-compatible or OpenAI-compatible
inference (`INFERENCE_MODE`), reranking opt-in (`RERANK_MODE`).
Same period (PRs #95–#100): OpenAI-wire embedder client everywhere;
cross-message embed batching (~25k single-message round-trips →
~500); three-case seed-vector priority chain hardened against
Phase 2 failures; single unified batched pipeline; L2-unit-norm
storage invariant with v17 backfill; token-based body cap using the
bundled Qwen3 tokenizer; Ollama/OWUI scaffolding removed.

### 2026-05-04 — Hybrid-search index fix (schema v15) and WAL fixes

Missing `fts_rowid` indexes caused `SCAN <base>` plans on FTS5 JOINs
(~1.1B row comparisons per query; 5–7 min searches). v15 migration
added the indexes + `ANALYZE`: `hybrid_search` ~400s → ~0.5s.
Separately, mcp-server's shared read-only connection pinned the WAL
read mark and blocked checkpoints (159 MB WAL observed); production
reads now open per-access connections. `RERANK_CANDIDATES` tuned to
20. Misdiagnosis history preserved in project memory
(`project_fts5_join_missing_index.md`, `project_mcp_wal_pinning.md`).

### Earlier — repository simplification

Schema v1→v12 migration debt collapsed into one
`_apply_initial_schema` (~1,200 lines deleted); `backfill.py`
removed; Maildir handoff simplified to post-sync `chmod go+r` with
UID separation and `:ro` mount preserved; `senders → participants`
fallback removed.

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Treat this file as the current execution plan, not as permission to
  ignore architectural constraints.
- The Current Objective section supersedes any older scope statement
  that froze the MCP API surface; Phases 0–5 are the priority order.
- When a task is completed, move it to `Recently Completed` or remove
  it.
- Keep this file concise and current.

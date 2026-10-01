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
  writes SQLite. Schema v22 (v21 squashed baseline plus migration
  `0022`, the `message_thread_map` lookup indexes): 4096-dim L2-unit-norm
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
5. **MCP endpoint auth — pinned 2026-09-28** pending the deployment
   decision (see Open decisions). Localhost topology alone is not a
   trust boundary against other local processes, but the right design
   depends on where the server runs.
6. ~~**Delete the dead action/IMAP surface.**~~ Done 2026-09-28 (see
   Recently Completed).
7. ~~**Source integrity exposure.**~~ Done 2026-09-28 (see Recently
   Completed).
8. ~~**Doc-drift sweep.**~~ Done 2026-09-28 (see Recently Completed).

### Phase 1.5 — Minimal regression baseline

Done 2026-09-28 (see Recently Completed): `make baseline`, CI job
`retrieval baseline`. Phase 2 must demonstrate behavior preservation
against it: refactor PRs (items 1–3) show no snapshot diff; item 4
shows a reviewed snapshot diff with golden checks still passing.

### Phase 2 — Swappable embedding / vector generations

**Exit criterion:** changing embedding models never requires altering
the source corpus (the Maildir) or the versioned schema — no numbered
migration and no `SCHEMA_VERSION` bump. Everything derived — vectors
first, but the chunk index too — is disposable and regenerable: a
context-compatible model switch regenerates vectors only, creating
and later dropping runtime-managed `gNN` tables that are exempt from
`SCHEMA_VERSION` (the exception recorded under item 2); an
incompatible one regenerates chunks and vectors through the reindex
bundle path. Neither touches a source file or a migration.

1. **`vector_generations` registry** — generation_id, provider,
   resolved endpoint, model, revision, dimensions, tokenizer, context
   window, chunk_config_hash, created_at, status (building /
   caught-up / active / retained / retired). Dimension read from
   metadata, never a constant; pass the `dimensions` request param
   where the provider supports it.
2. **Per-generation vec tables and the blue/green lifecycle** —
   `vec_chunks_gNN` and `vec_threads_gNN` (sqlite-vec bakes dimension
   into DDL), built for a candidate, validated, switched atomically,
   retained for rollback, retired. Seven review rounds on this plan
   (2026-09-30, PR #307) turned that sentence into a protocol with a
   state machine, and each round's findings were consequences of the
   previous round's additions — the sign that it needs a design
   document, not a longer bullet. **Deliverable before any
   implementation:** `docs/design/vector-generations.md`, reviewed on
   its own PR, satisfying every requirement below; the reviews that
   produced them are on #307.
   - **Identity and selection.** The registry records a generation's
     resolved endpoint (the SDK's `client.base_url` after
     construction, credential-sanitized — not the configured value,
     which is empty when the SDK inherits `OPENAI_BASE_URL`), model,
     dimension and any exposed revision, **plus a non-secret
     configuration label**, because two credentials at one gateway can
     route to different deployments behind identical public fields.
     None of those fields fingerprints the model's *output*: a
     host-side server can reload a different same-dimensional model
     under the same name, and a provider can move an unversioned
     alias. So the registry also stores a **calibration vector** — the
     embedding of a fixed synthetic text taken when the generation is
     created — and both services re-embed that text at startup and
     periodically; a distance beyond tolerance means the deployment
     behind the generation has changed, and the service refuses to
     write to or query it until a new generation is built.
     **At most two generations are live at once** (active plus one of
     building or retained), and the registry refuses to start a build
     while a retained generation exists, since only two configuration
     sets are available. Both services receive both sets (`EMBED_*`
     and `EMBED_NEXT_*`, each with its own Docker secret); the
     registry's active generation names the live one; each service selects the
     client whose label and identity match, at startup and on every
     semantic query, and **fails closed unless every live generation**
     (active, building, retained) **has a matching client** — not
     merely one of them, or the indexer could not dual-write. Activation
     is a registry change plus a restart or reload, never a
     configuration edit.
   - **Vector-only or rechunk.** A model switch reuses the stored
     chunks only when the candidate tokenizer counts every document
     input — every stored chunk and every chunkless thread's
     display-subject fallback text — within the candidate's input
     window, verified by counting; otherwise it is a rechunk through
     the reindex bundle below. Queries are the other input: today a
     tool's query string reaches `embed_query` unbounded, so the
     switch also fixes a supported query length, enforced on the query
     side from the active generation's window, or a query that fit the
     old model fails after activation and disables the semantic path.
   - **Validation gate.** The Phase 1.5 baseline runs on a hashed test
     embedder and proves wiring only. Activation requires #283's
     evidence-recall eval against the old and candidate generations
     with query vectors from their respective real embedders.
   - **One generation per query.** Read the active ID, embed the query
     **outside any transaction** (a read mark held across a provider
     call blocks WAL truncation — the May bug), then open a snapshot,
     re-read the ID, and run both dense lanes in it; re-embed once and
     retry if the ID changed, else fail closed.
   - **Synchronized with ingestion.** Dual-writing into every live
     generation is enabled and durably queued **before** the build
     watermark is recorded (or ingestion pauses across an atomic
     catch-up-and-enable), so nothing falls between backfill and
     dual-write; and backfill is **ordered behind concurrent
     mutations** — a monotonic sequence with conditional writes, or a
     replay of every post-snapshot mutation before the candidate is
     marked caught-up — so a stale backfill read of a chunk that
     ingestion has since replaced or deleted can never overwrite the
     newer state or leave an orphan vector. Every new or changed chunk, every **deletion** (reaps
     and reprocessing), and every survivor-thread mean recomputation
     apply to all live generations in one transaction — orphan vectors
     would otherwise consume the candidate window. The lifecycle is
     active A + candidate B → retained A + active B; the retained
     generation stays written for its rollback window and is retired
     only when that closes. Thread vectors are rebuilt with the
     candidate model, using the display-subject fallback for chunkless
     threads so none drops out on activation.
   - **Schema and secrets: two recorded exceptions.** The `gNN` tables
     are created and dropped at run time, which the schema rule (every
     change bumps `SCHEMA_VERSION` and ships a migration) does not
     allow: the registry and lifecycle arrive by one numbered
     migration; the tables it manages are runtime-managed derived
     storage, registered there, outside `SCHEMA_VERSION`, failing
     closed at startup on registry/table disagreement. And the second
     Docker secret for the embed layer breaks the one-secret-per-layer
     credential contract in AGENTS.md's Architecture Summary. Both are
     owner-approved exceptions (2026-09-30) that the design PR records
     in AGENTS.md before any wiring lands.
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
5. **Spike: retire the hand-rolled address parsing behind the
   existing bounds** (added 2026-09-30 after PR #260; reduced on
   review from a parser migration). `parser.py` uses the legacy
   `compat32` parser, and its header helpers work around it:
   `_split_address_list` / `_parse_addrs` / `_format_address`,
   `_decode_encoded_word_runs`, `str()` around `Header` values, and
   `_is_attachment`. The modern API (`email.policy.default`,
   `EmailMessage`, `email.headerregistry`) was proposed as the
   replacement, and review established what it cannot replace, each
   point verified on 3.14:
   - the **initial parse stays `compat32`**: the default policy parses
     structured headers during `message_from_bytes`, and a
     `Content-Type` `name=` holding a few hundred nested comments
     (about 1 KB) raises `RecursionError` there, before any guard;
   - the **encoded-word scanner stays**: the default policy's decoding
     is quadratic on malformed `=?…?` runs (0.28 s at 8,000 prefixes,
     4.1 s at 32,000);
   - the **address pre-screen stays**: reading `.addresses` recurses
     on about a thousand nested comments, mishandles malformed group
     syntax (a bogus empty `<>` address), and a large list enters
     `headerregistry` before a wrapper can look at it;
   - the **attachment classifier stays**: a filename-bearing `text/*`
     part with no `Content-Disposition` is an attachment here but
     `is_attachment()` says no and `get_body()` returns it, while
     `iter_attachments()` yields the second of two bare `text/plain`
     body candidates;
   - **`_safe_decode` stays**: `get_content()` raises `LookupError` on
     an unknown body charset.
   What is left is one targeted change: keep `_split_address_list` —
   it *is* the pre-screen, the linear structural split that keeps
   large lists, nested comments and group syntax out of any
   whole-header stdlib parse, and the raw-header cap alone does not
   replace it — and parse each element it yields with
   `email.headerregistry` instead of `_parse_addrs`, possibly retiring
   `_format_address` too. The work
   is a differential, not a rewrite: every parser fixture and every
   shape in the encoding parity test (plus the filename-only text part
   and repeated body candidates) run through both the old helpers and
   the new call, comparing addresses, names, body selection,
   attachment classification and identity; a helper is retired only
   where the differential is equivalent and its bounds survive, with
   a work-bound regression for each hostile input above. If the
   differential retires nothing without a new bound, close this item
   and record why. Any retired helper lands with the Phase 2 reindex,
   since a parse change can alter bodies and attachment identity.

**Reindex bundle.** Fixes that change chunk IDs, bodies or message
identity share one rebuild with the first Phase 2 generation, never one
reindex each. One rebuild, not one PR: each fix is its own reviewed PR
per the review rules, and a fix that would change bodies or IDs for
newly ingested mail before the rebuild is gated behind the pipeline
configuration so the live index stays internally consistent until the
single staged rebuild picks all of them up. The fixes: reply subjects in the embedding input and thread body (#303);
chunk overlap past `max_tokens` (#208); Message-ID conflicts kept as
both claimants (#217); a deterministic date source for undated mail
(#297's second half, the deferred received-date item: the top
`Received:` header, then — since sent mail and stripped messages have
none — the Maildir filename's delivery timestamp, which is sync time
but stable for the life of the file, then the previously persisted
date carried forward by the rebuild; `now()` only for a message with
none of the three, on first sight, and persisted once; the first half
— keep the first persisted date on reprocess — is a batch-1 guard);
the whitespace-only plain alternative that suppresses a non-empty HTML
body (#298: one line, but a body change, so it rebuilds with the
bundle); sequential inline text parts in `multipart/mixed` (#295,
decided 2026-09-30 to document for now and revisit when this bundle is
assembled, since the reparse is free then); and repair of chunks
committed with all-zero vectors (#304).

**Sequencing (decided 2026-09-30, per #283).** Phase 2's blue/green
lifecycle validates a new generation against the old, which needs a
measurement that distinguishes evidence recall from hit rate. So the
Phase 1.5 baseline plus #283's evidence-recall eval run before the
first generation is built: a slice of Phase 3 item 1 moves ahead of
Phase 2.

**Two kinds of reindex.** Items 1–2 version the vec tables only; the
chunk index stays keyed by deterministic chunk IDs. A context-compatible
model switch is therefore a **table switch inside the live file**, and
the lifecycle is proved on that first, on the unchanged corpus. The
reindex bundle above changes chunk IDs, so it is a **staged rebuild
with a file swap**, which the design document specifies as well; the
plan records only what it must satisfy (each point from a review round
on #307):
- no in-place rebuild: a running MCP server must never see half-rebuilt
  FTS data or vectors that no longer join, so the new image builds
  `mail.next.db` from the source corpus alongside the live file, and
  validation and the eval slice run against the staged file;
- the staged file is complete at cutover: mbsync and the live indexer
  are quiesced, a **final watermark is taken after that quiescence**,
  and the candidate indexer drains through it before the swap — the
  build-time watermark is not enough, since mail added, changed or
  reaped after it would be lost or resurrected; and an empty runnable
  queue is not completeness, because the queue parks persistent
  failures as `dead`, so cutover validation reconciles every source
  file against the staged database and rejects unresolved or
  dead-lettered work unless the operator has approved each as a
  terminal skip;
- **both** databases have their writers stopped and their WALs
  successfully checkpointed before either main file is renamed (the
  staged file is in WAL mode too, and a rename would orphan
  `mail.next.db-wal`); the MCP server is stopped and its in-flight
  requests drained before that checkpoint, and restarted only once the
  new file is installed and validated — it opens `mail.db` read-only
  per request, so a request could otherwise keep serving the old inode
  after the swap or open the path while neither file carries the live
  name;
- the swap is crash-consistent: the retained file is kept by a hard
  link taken first, the staged file then replaces the live name in a
  **single atomic rename**, and the directory is `fsync`ed — never a
  remove-then-rename pair that leaves an interval with no `mail.db` —
  and startup recognises and resolves a half-finished cutover (a
  marker written before the swap and cleared after) rather than
  finding an indeterminate state; the ordering is durable at every
  step — marker and retained link `fsync`ed before the rename, the
  directory synced after it, and the marker's removal synced too — so
  a crash between the rename and the sync cannot leave the new live
  entry without its rollback target or marker;
- rollback is the reverse swap onto the previous image tag, and it is
  only valid if the retained file is either kept synchronized with
  ingestion for the rollback window or caught up under the same
  quiescence — with reconciliation and the queue fully drained —
  before the MCP server is started on it; the indexer touches its
  health file before `initial_index`, so "start the old image" alone
  would serve a stale database while catch-up runs;
- the live database is **never migrated in place**: a bundled schema
  change (#217) is applied only to the staged file by the rebuild
  image, and the live indexer runs the previous image until cutover —
  a behaviour gate alone cannot defer the migration runner, and a
  retained file already on the forward-only schema would make the
  reverse swap unusable;
- a rebuild of the previous `pipeline_config_hash` is *not* a rollback:
  the bundle carries a forward-only migration (#217), so the previous
  image fails closed on the new schema and the new image lacks the old
  algorithms; the manifest records which stages changed, not a way to
  recreate code, and chunk/FTS coexistence machinery stays deferred;
- the bundle's cost is measured on a representative **staged full
  rebuild** (reparse, extraction unless the cache is safely seeded,
  FTS, thread and message rows, embedding); the vector-only
  generation's timing is only an embedding lower bound. The gate also
  covers **peak storage**: two complete databases, their vector tables
  and WALs, and the extraction cache must fit with headroom, since a
  full data volume also fails the live indexer's commits, so a
  conservative free-space preflight runs before staging begins.

### Phase 3 — Measurement and product vertical slice

**Exit criterion:** we can objectively measure whether the system
answers real knowledge questions, and identify why failures occur.

1. **Agent-level evals** on the synthetic mailbox: tool-selection
   accuracy, argument accuracy, retrieval recall, citation accuracy,
   pagination completeness, unnecessary-call counts. Extends the
   retrieval-only harness in `mcp-server/tests/eval/` (Recall@10 /
   MRR, opt-in; see its README for the invocation) to the agent level; the old
   `scripts/eval_run.py` batch runner was removed with Open WebUI.
   Tracked by **#283**, whose substantive addition is separating
   jointly-required-evidence recall from any-hit rate (today's
   "Recall@10" is the latter); its first slice precedes Phase 2 (see
   Phase 2, Sequencing).
2. **Latency instrumentation before performance redesign.** Stage
   timers through the query path (query_embedding / per-lane FTS+KNN /
   fusion / rerank / evidence_fetch / inference / total). `ask_mailbox`
   exceeded 60s client timeouts on a populated mailbox before the v15
   index fix and has not been re-measured since — measure before
   touching KNN architecture; if inference dominates,
   vector work won't fix the user problem. Then set request-level
   deadlines. (Project history endorses this: the 400s search hang
   was three wrong theories until the query plan was measured.)
   Tracked by **#287** (adds content-safe telemetry, cold/warm
   benchmarks, cancellation of thread-offloaded work).
3. **Experimental ephemeral `brief_issue`.** Chronology, actors,
   positions, decisions, open questions, conflicting evidence — every
   assertion cited, **nothing persisted**. This is the proving ground
   for what a durable ontology should eventually contain; its
   failures drive Phase 4. Tracked by **#291** (chronology,
   corrections, contradictions, "as of" questions; "newest is not
   authoritative").
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
   Tracked by **#288** (thread-vector policy, with versioning and a
   recompute spec) and **#289** (rerank, including candidate-text
   representation; recommendation only, default stays off).
6. **Prompt context budget with coverage disclosure** (#286 is
   item 7; this is **#285**): replace the fixed 2,000-character
   per-thread fill and 3-chunks-per-candidate slice with a
   whole-prompt token budget, an evidence-selection policy, dedup,
   and a statement of what was left out. Nothing in the plan covered
   this; #214/#215 fixed only `summarize_thread`'s tail and
   attachment identity.
7. **Semantic recall under selective filters** (**#286**): the vector
   lanes run unfiltered and sender/date/folder filters apply
   afterwards, so a selective filter can empty the candidate window.
   Measure the loss, then choose a bounded remedy (eligible-subset
   scoring, filter pushdown, or candidate expansion).
8. **A validated citation contract for today's answers** (**#284**):
   stable evidence IDs and per-message identity in `ask_mailbox`
   prompts, a structured claim→citation map checked against the
   evidence, bounded repair. The plan verified quotes only for
   Phase 5's persisted claims; ephemeral answers need it first.
   Depends on #217's identity decision.

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

## Open review findings (next session starts here)

Codex filed code-review findings as GitHub issues on 2026-09-28. Many
break the Phase 0 exit criterion (a source must reach one visible
terminal state) or the Phase 1 truthful-contract criterion, so they
come before Phase 2 work. The first batch (#202–#223) is done: 18 of 22
closed in PRs #229, #236, #241, #246, #247, #248, #249 (see Recently
Completed).

How the first batch was worked, and what to repeat:

1. **Verify before fixing.** Parallel agents, one per code area, checked
   every claim against current `main`. Every issue was real, but severity
   and likelihood were often misjudged (#203 and #204 far more likely
   than stated; #217 overrated; #202 overstated for a single sheet).
2. **Batch by area.** Small PRs grouped by code area, with one test-first
   commit per issue and a `Fixes #N` line per issue so a merge closes
   it. Behaviour-changing or schema-adjacent fixes get their own PR.
3. **Expect Codex rounds.** Every PR took 1–5 rounds, and later rounds
   found gaps in the fixes themselves. Record each round in the PR body
   and PLAN, and resolve threads only once fixed or deferred by the owner.
4. **Guards in the loop, mechanisms out of it** (learned on #260,
   fourteen rounds). A finding that needs a check, cap or fallback is fixed in
   the round. One that needs a new mechanism — new parsing of untrusted
   input above all — is a stop-and-ask, with "document the limitation"
   as the default; three rounds on one mechanism means re-scope. Before
   fixing a "wrong" derived value (a hash, a size), ask what consumes
   it. Before rewriting a walk, pin the old behaviour with tests.

### Carried over from the first batch

- **#217 Message-ID conflicts** — split out of #246 after two review
  rounds showed it needs a design. The earlier attempt is on the pushed
  branch `fix/indexer-message-id-conflicts` (head `d479363`, based on a
  pre-#246 `main`, so rebase before reuse); PR #246's description lists
  every finding it must address:
  - fail closed when the recorded original is unreadable
  - compare full attachment metadata, not only content hashes
  - replace rather than merge on takeover: remove the old message and
    rebuild its thread first
  - re-check a conflict only when the original disappears, not on every
    walk
  - verify source identity before a known Message-ID keeps its thread
    (the #246 round-4 P3)
  - owner decision pending: which claimant wins; the first-arrival
    spoofing risk (the #246 round-1 P3)

  Decided 2026-09-30: **keep both claimants** — the thread stays keyed
  by Message-ID, each message row is keyed by Message-ID plus content
  hash, and a conflict is exposed by `get_message` and status rather
  than resolved by an arrival-order rule (either order is spoofable).
  That needs a **stable claimant identifier in the MCP contract**
  before the schema: today the chain is `get_thread` →
  `messages[].message_id` → `get_message`, and `get_message` takes a
  bare Message-ID, so two claimants would be indistinguishable to a
  caller. Specify the identifier (Message-ID plus a short content-hash
  discriminator, or a derived opaque ID), propagate it through thread,
  message, search and evidence results and the retrieval parameters,
  and keep the bare Message-ID working for the unambiguous case. The
  identifier replaces the bare Message-ID in every per-message key,
  not only `messages` and the MCP contract: the chunker's
  `message_pk` (so the deterministic `sha256(message_pk || index ||
  text)` shape is kept but two claimants' chunks cannot collide),
  attachment occurrence and chunk IDs, and the deletion paths that
  drop chunks and attachments by Message-ID — otherwise reprocessing
  or reaping one claimant overwrites or deletes the other's evidence.
  #260's serialized-form attachment hash gives the "compare full
  attachment metadata" finding a deterministic identity. A schema
  change, so it lands with the Phase 2 migration and reindex. That
  reindex also repairs databases already damaged by #204, #205, #230
  and #232.
- **#208 chunk overlap exceeds `max_tokens`** — harmless at default
  settings; it changes chunk IDs, so it lands in the Phase 2 reindex
  bundle (see Phase 2), not on its own.
- ~~**#209 single-part MIME attachment decoded as body**~~ (done with
  #230 in item 8 below), ~~**#210 stale
  unsupported-extraction cache entry**~~ (done in item 10 below) — low frequency, about 10 and 5
  lines; fix them when next in `parser.py` / `attachment_indexing.py`,
  or together as one small PR.

### Second batch (#224–#245) — verified 2026-09-28

Triaged against `d30e500` by four parallel agents; every issue is real
and most reproduced with synthetic input. PRs in the order to land
them (one test-first commit per issue, `Fixes #N` per issue):

**Status 2026-10-01 (late) — next session starts here.** Second batch
items 1–12 are merged; 13–14 and #217 are decided (below and in
Resolved decisions). The deprecation cleanup is finished (#343, #345,
#346, #348; baseline is v21). Third batch merged so far: #293 (#335),
the privacy trio #311/#325/#326 (#337), #327/#328 (#341), #301 (#336),
#278/#267 (#342), #339/#340 (#347), #304 batch-1 half (#349), #297
first half (#350), #306 (#351), #302 (#352, schema v22), #319 (#353),
#332 (#356), #321 (#357), #318 (#358), #310/#329/#315 (#359), #314
(#360), #361 (#414), #320/#334 (#417), and the whole #257 sweep (#364,
#363, #401, #412, #411, closed by this PR). The 2026-10-01 cleanup
batch is done: the 36 stale-text issues #365–#400 and the #395
reconciler fix merged as #402–#410 (decisions: `RERANK_TOP_N` removed,
the eval's unused query fields dropped, its fallback notice printed).

In review or queued (three PRs in flight; Codex-clean PRs are merged
without waiting, per the owner):

- Merged since: #416 date filters (#312, #330, #333) and #418 name
  matching (#313, #324, #331; #316 query side).
- In review: #419 mbsync #271/#280, #420 #308/#309. Queued: the
  `fastmcp` 4.0.10 migration (owner's choice over `mcp` 1.30.0;
  closes #317 with an explicit session idle timeout and keeps
  Host/Origin checks on both transports).

Filed 2026-10-01: #362 (`idna` RFC 2231 filename raises
`UnicodeError`; needs a fallback-filename decision) and #415 (search
`folders` filters still use the thread's representative folder).

Open owner decision: **#267** one-shot rotation. #342 shipped the
documented limitation (recreate with `BRIDGE_CERT_PIN_ROTATE=false`);
the alternatives are a flag that holds the expected new fingerprint, or
a consumed marker in `/state`.

Before `make up`: the operator's `.env` and `.secrets` use current
names, but the embedding provider (`EMBED_MODEL`, `EMBED_BASE_URL`,
`.secrets/embed_api_key.txt`) still needs configuring. A dev database
at v20 must be rebuilt from Maildir; v21 opens and migrates to v22.

Next, in order (each item is a batch-1 guard unless noted; reproduce
first, one test-first commit per issue, keep three PRs in flight):

1. ~~Rest of the MCP "errors reported as success" cluster: #318,
   #321, #332, then #310 + #329 + #315 together, then #314~~ (done:
   #356–#360).
2. ~~The cleanup batch above (C1–C8, then #395)~~ (done: #402–#410).
3. ~~#257 sweep, split by boundary~~ (done: parser #364, `last_error`
   #363, attachments #401, MCP provider errors #412 with a dedicated
   `ProviderResponseError` and caller text classified like the log,
   MCP SQLite logs #411; AGENTS.md updated in this closing PR).
4. MCP: ~~event-loop hygiene #320, #334~~ (done: #417), ~~date
   filters #312/#330/#333~~ (done: #416), ~~name matching
   #313/#324/#331~~ (done: #418; #316's remainder needs the Phase 2
   reindex). In review: #308/#309 (#420). Queued: #317 through the
   `fastmcp` 4.0.10 migration, then #415 (search `folders` filters).
5. mbsync: #271, #280 (in review, #419). Parser: ~~#361~~ (done: #414); #362
   needs a fallback-filename decision first.
6. Bridge, needing Docker for `make bridge-upgrade-check` and the
   first Bridge shell test harness: the entrypoint PR (#242, #266,
   #270), the smoke-test fix (#269 with #268's minimal fix, both in
   `scripts/bridge-smoke.sh`), and the updater-gate PR (#245). Land
   them before the eval slice: existing vaults make unpinned update
   fetches until #245.
7. Then third-batch items 2–4 below (extractor bumps, mbsync design,
   the Phase 2 reindex bundle), the Phase 1.5/#283 eval slice, and
   Phase 2.

How this session worked, worth repeating: fixes were prepared by
background agents, one per issue in its own `git worktree` off
`origin/main`, committing locally only; each diff was reviewed before
its PR opened, and rebased on `main` first (several hit stale test
helpers or neighbouring PLAN strikes). Codex inline findings are
matched to a round by `original_commit_id`, not by timestamp.

1. **Done (#251).** **#238** invalid date filters leak withheld input into logs — five
   handlers logged the `ValueError` quoting the value. Fixed with a
   dedicated `InvalidFilterError` the handlers log by field name only.
2. **Done (#252).** **#239** (P1) reply-attribution regexes are quadratic, and wider than
   filed: all six languages, and `wrote:` mid-line too. A 1 MB line
   blocks the worker for minutes. Fixed: skip lines over 300 chars in
   `_is_reply_header`.
3. **Done (#255, 5 Codex rounds).** **#228 + #226** (#228 P1) DOCX walker rewrite: iterate serialized cells once
   (no `gridSpan` expansion), skip vMerge continuations, recurse into
   nested and header/footer tables via `iter_inner_content()`. No
   character budget needed: work is now linear in the XML, and lxml's
   256-element depth limit bounds the recursion. Also lands the cache
   versioning (`EXTRACTOR_VERSIONS`, `docx@2`): an older version is a
   cache miss and startup re-queues the affected messages once. Review
   rounds settled the refresh semantics: any occurrence of a stale row
   re-runs the module that wrote it (`extract(module_override=...)`),
   the sweep re-queues every message carrying the bytes except
   dead-lettered ones (those keep their stale chunks until an operator
   runs `make requeue-dead`), and a plan
   without usable text clears the attachment's chunk slice.
4. **Done (#253).** **#224** malformed 200 provider responses leak mailbox text (embedder
   index errors reach logs and `indexing_jobs.last_error`; reranker
   `int()`/`float()` errors reach logs). Fixed-text diagnostics only.
5. **Done (#254).** **#232** non-finite embeddings: sqlite-vec stores NaN; the row gets
   a NULL distance (thread lane raised on `float(None)`, chunk lanes
   scored it as a perfect match). Rejected at the embedder,
   `l2_normalize` and the MCP query embed; MCP skips non-finite
   distances. No startup repair sweep: in a realistic index these rows
   sort last, reaching results only when `k` nears the row count, and
   detecting them costs a full vector scan (15–30 s at 20k threads).
   Already-stored rows are repaired by the Phase 2 reindex.
6. **Done (#258).** **#233 + #225** MCP robustness: guard `parseaddr` in the mcp-server's
   `canonical_addr` (the indexer copy already does); validate reranker
   indices (unique, in range) before mutating any candidate.
7. **Done (#259).** **#243** header clipping sweep (subject, participant names,
   attachment filename/MIME, reranker `_candidate_text`). Separate from
   the `get_message` / ID-length backlog items below.
8. **Done (#260).** **#230 + #209** parser MIME traversal: nothing inside an
   attachment is a body candidate, attachments inside attachments are
   still recorded, and a single-part or attachment-labelled root is
   classified. An attached email is identified by the hash of its
   serialized form (deterministic, the same across transfer encodings),
   not of its raw bytes; every one used to hash to `sha256(b"")`. Exact
   raw-byte identity was tried and reverted (see Deferred), since
   nothing extracts `message/rfc822`.
9. **Done (#262).** **#231 + #234** multipage TIFF (honour the ignored `max_ocr_pages`),
   UTF-16 BOM / NUL detection. Bump the `image` and `text` modules in
   `EXTRACTOR_VERSIONS` (from item 3; owner approved 2026-09-29) so
   their stale cache rows re-extract. **Land #237 (item 10) first:**
   without a per-batch cache, the startup re-queue re-runs OCR once per
   message carrying the same stale payload in a batch (PR #255 round 2).
10. **Done (#261).** **#237 + #210** extraction cache semantics: per-batch cache keyed
    by content hash, and re-run an `unsupported` row when an extractor
    now resolves.
11. **Done (#263).** **#244** reaping leaves `indexing_jobs`, and the rename fix moves
    pending jobs onto the `T`-flagged path, so an embedder outage past
    the grace period resurrects deleted mail. Delete the job in both
    removal paths; skip trashed files at drain time.
12. **Done (#264).** **#227 + #240** mbsync: functions called under `if` run without
    errexit, so a failed `chmod` / fingerprint write still reports
    success. Check each step; write the pin via temp file + `mv`.
13. **#242** (decided 2026-09-30) no non-interactive way to tell an
    empty vault from a logged-in one. Set `BRIDGE_FORCE_CLI=true` in
    `docker-compose.first-run.yml`. Lands with #266 (bootstrap runs
    key-gen before the vault check) and #270 (the documented `su`
    shortcut cannot work), which restructure the same `LOGGED_IN`
    branch; the first Bridge entrypoint PR also creates a shell test
    harness like `mbsync/tests/entrypoint_test.sh`.
14. **#245** (decided 2026-09-30) impact is lower than filed: with no
    launcher a downloaded update is staged in `/data` but never
    executed; the exposure is unpinned fetches and code on disk. Fix is
    a fourth patch hunk forcing the `updates.go` gate off (three-layer
    rule applies), the only fix that holds for every vault. AGENTS.md's
    "silently bypass" wording was corrected in #256.

### Third batch (#266–#334) — triaged 2026-09-30

69 issues filed from a whole-repository review. Triaged by four
parallel agents reading (not reproducing) one area each; every claim
was judged plausible against the source, and severity is miscalibrated
in the usual direction (#301 and #306 need rare conditions; #293 and
#294 are cheap to trigger). **This queue is provisional:** rule 1
above applies to every item — reproduce the claim on the current head
in the PR that fixes it, and drop or re-scope an item whose claim does
not hold. Decisions 6–11 in Resolved decisions were taken on the same
basis: each states a direction conditional on its finding reproducing,
and a decision whose finding fails to reproduce is void, not binding.
Order of work, chosen to minimise reindexes:

1. **No-reindex guards, small PRs by area.** Indexer: ~~the quadratic
   subject normalizer (#293)~~ (done: offset scan, one slice); ~~the first half of #297 (keep the first
   persisted date on reprocess, so an undated message is never
   re-dated before the Phase 2 rebuild)~~ (done: a fallback date
   defers to the stored `sent_at` on reprocess and reap rebuild); ~~the all-zero embedding guard (#304, fixed
   message, no values logged)~~ (done: rejected at the indexer embedder
   and the MCP query embed); ~~tombstone revalidation on restore
   (#301)~~ (done: a tombstone is refused for a path the message no
   longer maps to); ~~`message_thread_map` lookup indexes as migration `0022`
   (#302, index-only)~~ (done: filepath and thread_id indexes); ~~the unbounded recovery parameter list
   (#306)~~ (done: recovery lookups bind IDs in batches of 500);
   and ~~the #257 sweep (classify parse-stage and provider exceptions
   at their boundary, `caplog` marker tests)~~ (done: #364, #363, #401,
   #412, #411, #413). MCP: ~~the two quadratic
   regexes (#327, #328)~~ (done: one way to match each whitespace run);
   ~~the privacy trio — redirects that would
   forward prompts and API keys (#325), inherited endpoint userinfo
   in the startup log (#326), the read-only URI bypass (#311)~~
   (done: same-origin request hook on the Anthropic backend, userinfo
   check on the resolved endpoint, percent-encoded URI path); the
   "errors reported as success" cluster — ~~intelligence tools
   returning `Error:` prose as `isError=false` (#319, the unfinished
   half of Phase 1 item 2; `docs/mcp-tools.md` already promises
   otherwise)~~ (done: failures and unknown threads are raised as
   `ToolError`), ~~semantic search on missing vec tables (#318)~~
   (done: semantic mode errors when neither vector lane answers),
   ~~malformed provider content rendered as a summary (#321)~~ (done:
   non-text or blank content is an error in both backends), ~~future
   timestamps marking the index current (#332)~~ (done: a stamp over
   2 minutes ahead is a not-current reason), ~~schema-violating
   extraction records (#310), overwritten `_date`/`_source_thread`
   fields (#329), the missing search instruction in extraction
   prompts (#315)~~ (done: records failing a bounded required-field
   and JSON-type check are dropped and reported, a schema declaring a
   provenance name is refused, and the query reaches the prompt as the
   request), and ~~typo'd thread IDs summarizing an unrelated
   thread by domain-token overlap (#314: narrow the fallback rather
   than parse IDs)~~ (done: an input containing `@` never reaches
   the subject fallback); ~~the name-matching cluster (#313, #324,
   #331)~~ (done: #418) and its FTS analogue (#316, query side done in
   #418; the rest needs the Phase 2 reindex); ~~date filters (#312,
   #330); degraded-lane honesty (#333)~~ (done: #416); event-loop
   hygiene (~~#320, #334~~ done in #417; #317 via the `fastmcp`
   migration); folder discovery hiding reply-only folders (#308); the
   attachment-lane duplicate before MIME filters (#309); ~~docs (#322,
   #323)~~ (done: #403, #404). mbsync:
   ~~the empty pin re-TOFU (#278)~~ (done: only an absent pin is a
   first boot) and ~~the rotation flag surviving restarts (#267,
   docs)~~ (done: documented recreate-with-false; one-shot
   authorization not built), the unbounded connect probe (#271), and
   signal forwarding to the sync child (#280). Bridge, both decided as
   items 13–14 above and needing the first Bridge shell test harness:
   the smoke-test one-liner (#269) with #268's minimal fix, the
   entrypoint PR (#242 `BRIDGE_FORCE_CLI`, #266 bootstrap order, #270
   the impossible `su` shortcut) and the updater-gate PR (#245, with
   its enabled-vault test) — before the eval slice, since existing
   vaults make unpinned update requests until #245 lands.
2. **Extractor version bumps, one PR per module** so each cache
   refresh happens once: `xlsx` (#294's shared-string budget — a
   behaviour change for the same bytes, so it lands with the bump
   rather than as a guard, or cached rows would keep the old result —
   #296 empty cells, #305 stale dimensions; add `xlsx` to
   `EXTRACTOR_VERSIONS`),
   `docx` 2→3 (#299 first-page and even-page headers), `pdf`
   (#292 page-level OCR selection — add `pdf`), with #300 (enabling
   OCR re-queues skipped images) alongside since it shares the
   OCR-disabled sentinel.
3. **Design work before code**, both mbsync: the retained near-side
   state family (#275, #276, #279, #281) and the sync-supervision
   pair (#277, #282; their small siblings #271 and #280 are batch-1
   guards above and are not repeated here); see Resolved decisions 9
   and 10 for the chosen direction and the one measurement still
   needed.
4. **The Phase 2 reindex bundle** (see Phase 2): #303, #208, #217,
   #297's second half, #298 (a one-line selection fix, but it changes
   persisted bodies, so it lands with a rebuild rather than making
   results depend on processing history), #295 if revisited, and the
   zero-chunk repair from #304.

Follow-ups filed 2026-09-30 from the privacy trio, both batch-1 guards
(done, both reproduced): #339 (indexer logs an SDK-inherited embed URL
without the userinfo check, counterpart of #326) and #340 (OpenAI SDK
clients re-send request bodies on cross-origin redirects, counterpart
of #325; reuse #337's same-origin hook).

Closed as duplicates of plan lines: #272 and #273 (Maintenance
backlog), #290 (Phase 2 items 1–3). Roadmap issues #283–#291 are
linked from the Phase 3 items they track.

## Maintenance backlog (small, ongoing)

- consolidate `BRIDGE_VERSION` to a single source of truth
  (`.env.example`); parameterize the Go toolchain as an `ARG`
- `timeout-minutes` + path filters on `.github/workflows/docker.yml`
- Bridge build: `go mod download` has no retry, so one blip at
  `proxy.golang.org` (seen 2026-09-30: an HTTP/2 `INTERNAL_ERROR` on a
  single module) fails the whole `docker compose build` check. Add a
  bounded retry around the download (`go mod verify` stays
  unconditional) or a module cache in the workflow
- Trivy scan of the Bridge Go module graph in `security.yml` (#272
  closed as its duplicate; needs an exception policy for upstream
  Proton dependencies we cannot patch)
- pin `actions/checkout` to a commit SHA in `bridge.yml`; pinned
  `setup-go` in the patch-drift job
- fix the `\t\t` BSD-sed portability bug in `bridge/patch-source.sh`
- mbsync: move `BRIDGE_USER` to a file-backed secret; add log
  rotation + memory/CPU limits; evaluate runtime package pinning
- resource limits for the remaining Compose services
  (`protonmail-bridge` first — it holds live Proton credentials; #273
  closed as its duplicate: a measured, operator-overridable memory
  budget, tested against the initial Gluon sync)
- loud one-shot startup warning when `INFERENCE_MODE` sends retrieved
  excerpts to a remote provider
- `get_message`: returns a message's full body and headers with no
  bound, so one huge message (a pasted log, 12,000 References) is one
  huge response; decide on body continuation (offset paging) or a
  documented cap — fits alongside Phase 1 item 2's structured output
- ~~mcp-server: remove the dead `Database.get_thread_message_ids`~~
  (done: already gone from the code)
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
- replacing the stdlib MIME parser (decided 2026-09-30 on PR #260):
  Python's `email` package never exposes a part's raw byte offsets and
  parses a transfer-encoded `message/rfc822` (forbidden by RFC 2046,
  but sent) before decoding it, so an attached email's identity is the
  hash of its re-serialized form (line endings and folding normalized),
  not of its raw bytes. Encodings agree on that form for every shape a
  25-case parity test covers, after the transport text was rebuilt
  from the parser's raw tuples; the one residual is an inner email
  declaring `multipart/*` with no usable boundary (malformed), which
  compat32 parses differently nested and standalone. A hand-rolled
  boundary slicer was tried and reverted after four Codex rounds of
  real findings (quadratic and memory-heavy on hostile input, bare-CR
  endings, depth caps). Every maintained Python parser
  wraps the stdlib; `flanker` is unmaintained (last release 2019). The
  one real candidate is Stalwart's Rust `mail-parser` (RFC-conformant,
  no dependencies, exposes part offsets, built for hostile mail) via a
  homegrown pyo3 binding and a Rust build stage in the indexer image —
  an architecture change with a full reindex. Revisit if hostile-mail
  robustness becomes a goal in its own right (Phase 3's adversarial
  suite is the natural trigger), not for a bug fix

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
- coverage scope: both services measure `src/` with `src/main.py`
  omitted. The indexer's `main.py` has grown to hold the whole
  two-phase pipeline, which `tests/test_main.py` exercises but the
  coverage figure does not count — re-scope when touching it

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
3. **#242 first-run retry (2026-09-30):** `BRIDGE_FORCE_CLI=true` in
   the first-run overlay, landing with #266 and #270.
4. **#245 auto-update in existing vaults (2026-09-30):** a fourth
   Bridge patch hunk forcing the `updates.go` gate off, under the
   three-layer rule — with one addition to the pattern: the existing
   layers prove only the new-vault default, so the `go test` for this
   hunk must exercise the patched gate with an **enabled** existing
   setting (`AutoUpdate: true`) and prove the version fetch and staging
   path is not entered, and the smoke test asserts the same from the
   log on a vault seeded that way where seeding is feasible.
5. **#217 Message-ID conflicts (2026-09-30):** keep both claimants;
   expose, do not resolve by arrival order; a stable claimant
   identifier goes through the MCP contract first. Phase 2 reindex
   bundle.
6. **#292 mixed digital/scanned PDFs (2026-09-30):** page-level OCR
   selection with a `pdf` extractor version bump, together with #300.
7. **#295 sequential inline text parts (2026-09-30):** document the
   limitation now (queued as cleanup batch C7, since the note belongs
   in `docs/architecture.md`); revisit with the Phase 2 reindex
   bundle.
8. **#297 undated mail (2026-09-30):** keep the first persisted date on
   reprocess now; with the Phase 2 reindex, a deterministic chain —
   top `Received:`, else the Maildir filename timestamp, else the
   previously persisted date — so a rebuild never re-dates a message
   (see the Phase 2 reindex bundle).
9. **#276 and family, retained near-side mbsync state (2026-09-30):**
   tolerate a far-side box that cannot be opened (warn, keep syncing
   the rest) rather than far-only patterns or `Remove Near`, which
   deletes local mail. #275 and #281 change the on-disk layout and wait
   for a real report.
10. **#277/#282 health during a long first sync (2026-09-30):**
    separate liveness (process alive, progress observed) from
    freshness (the success stamp), so a first sync is "healthy, not
    yet current"; then set #282's stall deadline above the observed
    initial backfill — that duration is the one measurement still
    needed, and #277 lands before #282.
11. **#268 smoke-test scope (2026-09-30):** the minimal fix
    (distinguish the intentional post-marker kill from a fatal exit)
    with #269; the requested IMAP/STARTTLS/SAN/restart checks are the
    live-Bridge lane declined in Not doing.

## Open decisions

1. Default deletion/retention mode when Phase 4 lands (mirror vs
   archive as the shipped default).
2. Whether `brief_issue` debuts as an MCP tool or a host-side script
   during its Phase 3 experimental period.
3. MCP endpoint auth (Phase 1 item 5, pinned 2026-09-28). The design
   follows the deployment target:
   - local only, deployable outside this repo → static bearer token
     stored as a Docker secret (`.secrets/mcp_auth_token.txt`, mode
     600) and read via `_read_secret`; the env-var fallback stays a
     non-container local-dev convenience only, never the Compose
     path. Constant-time compare, fail closed on an empty token
   - own devices over a private network → gate outside the app
     (Tailscale, reverse proxy, Cloudflare Access), static token as
     optional defence in depth
   - hosted clients (claude.ai / ChatGPT connectors) → OAuth 2.1
     resource server validating tokens from an external IdP
   Both non-local options send mailbox content off the host (and
   Cloudflare adds a third-party transit hop), so either needs an
   owner decision and a privacy-posture update in AGENTS.md first.
   Either one also has to add the approved external Host (and HTTPS
   Origin) to the `TransportSecuritySettings` allowlist in
   `mcp-server/src/main.py`. The allowlist currently accepts only
   localhost, loopback and `mcp-server`, so proxied requests fail
   before they reach auth. Add each approved name narrowly and keep
   DNS-rebinding protection on. The hosted option must also set
   `AuthSettings.resource_server_url` to the externally visible MCP
   URL, because the SDK registers the RFC 9728 protected-resource
   metadata route and the `resource_metadata` challenge parameter
   only when that URL is set. Without them connectors cannot discover
   the authorization server. The proxy must expose that well-known
   route.
   Any in-app option plugs into the MCP SDK's `TokenVerifier` /
   `AuthSettings` hooks: `sse_app()` and `streamable_http_app()` add
   the bearer-auth middleware themselves (so `server.run()` and
   `dual` both get it), and `custom_route` endpoints such as
   `/health` stay unauthenticated. `AuthSettings` requires an
   `issuer_url` even when only a verifier is used.

## Recently Completed

### 2026-09-30 — Second review batch, items 6–12 (#258–#264)

Seven PRs, one per item, each with a test-first commit per issue.
MCP: a hostile stored sender no longer aborts sender-filtered searches
and an invalid rerank ranking falls back to RRF order (#233, #225, PR
#258); every tool and prompt cuts sender-controlled headers (#243, PR
#259). Indexer: attachment boundaries in MIME traversal, with attached
emails identified by their serialized form and transfer encodings
made to agree on it (#230, #209, PR #260, fifteen Codex rounds — see
the review-method notes above and the parser-policy candidate in
Phase 2); extraction results shared within a batch and `unsupported`
rows re-checked per occurrence (#237, #210, PR #261); every TIFF page
OCR'd and UTF-16 text decoded, with `image` and `text` extractor
versions bumped (#231, #234, PR #262); queued work can no longer
resurrect reaped mail, with tombstones re-checked inside the reap
transaction (#244, PR #263, six rounds). mbsync fails closed when the
cert pin cannot be saved or the Maildir permission repair fails, with a
shell test harness and CI job (#240, #227, PR #264). Lessons from the
review loop went into AGENTS.md and here (PRs #265, #307).

### 2026-09-29 — Second review batch, items 1–5 (#251–#255)

Triage of #224–#245 (four parallel agents) confirmed every issue; see
the ordered list under Open review findings. Merged: rejected date
filters logged by field name only (#238, PR #251); reply-header regexes
skip lines over 300 chars (#239, PR #252); malformed provider responses
no longer quote their values in logs or `last_error` (#224, PR #253);
NaN/inf embeddings rejected at the embedder, `l2_normalize` and the MCP
query embed, and skipped on read (#232, PR #254); DOCX walker reads each
cell once and walks nested and header/footer tables (#228, #226, PR
#255). #255 also added `EXTRACTOR_VERSIONS` cache versioning and a
startup re-queue; five Codex rounds shaped its refresh semantics
(recorded in item 3). AGENTS.md gained an "Untrusted Mail Content"
section and a PR review section (#256); remaining logging gaps are
#257. Item 6 (PR #258): the MCP `canonical_addr` degrades an
unparseable stored sender to "no address" instead of aborting the
search (#233), and `_apply_rerank` falls back to RRF order on an
out-of-range or repeated rerank index (#225).

### 2026-09-28 — Right text reaches the model (#214, #215, #223)

`summarize_thread` spends its recent-chunk tail budget newest-first and
renders oldest-first (#214): one ordinary chunk filled the budget, so the
newest reply was the first thing dropped. Evidence for a thread surfaced
by an attachment filename match leads with that attachment's chunks,
then other attachments, then body (#215); only the thread was
remembered, so other attachments could fill the three slots. Both vector
lanes clamp KNN `k` to sqlite-vec's 4096 (#223): a large
`RERANK_CANDIDATES` with a filter asked for 8000 and silently lost the
chunk lane. No schema change; `make baseline` unchanged. Review round 1
(Codex): matched attachments are ranked by BM25, since the index holds
MIME types and query words are OR'd ("proposal-quote pdf" matched every
PDF); and a chunk cut to fit the tail keeps its beginning, so an
oversized newest reply keeps the answer it opens with. Round 2: the
budget goes to messages newest-first but reads each message from its
first chunk, so a long newest email's last chunk cannot crowd out its
opening.

### 2026-09-28 — MCP results no longer overclaim (#219, #220, #222)

Three places where a tool's answer claimed more than it had. Neither
inference backend checked why generation stopped (#222): output cut off
at `max_tokens` now raises `InferenceTruncatedError` with the partial
text, `extract_from_emails` counts truncated and non-JSON threads apart
from a valid `null` and says how many could not be extracted (instead
of "No structured data … found"), and `ask_mailbox` / `summarize_thread`
mark a cut-off answer. `search_attachments`' text lane ranks each
attachment by its best chunk before its LIMIT (#220), so one long
document no longer hides other matches (a MATERIALIZED CTE, since
`bm25()` is not allowed in a grouped query). `get_evidence` rejects
`thread_id` combined with thread-selecting filters (#219) instead of
silently ignoring them. No schema change; `make baseline` unchanged.
Review round 1 (Codex): valid JSON of another shape (a string, a
number, a list of non-objects) now counts as a failed extraction;
blank optional filters (`from_addr=""`) are absent on the thread-scoped
path, as on the mailbox-wide one; and an OpenAI `content_filter` or
Anthropic `refusal` stop is an error rather than a finished answer.
Review round 2: the attachment text lane applies its filters before
grouping (a filtered search no longer aggregates every matching chunk
in the mailbox), and an Anthropic `model_context_window_exceeded` stop
counts as truncation. Round 3: a mixed array's objects are kept but
the thread counts as incompletely extracted.

### 2026-09-28 — Subject fallback needs a shared correspondent pair (#205)

The headerless subject fallback accepted any one shared address, and
the mailbox owner is a recipient of nearly every message, so two
vendors' "Invoice" mails merged into one thread. It now requires the
incoming sender and one of its other recipients to both be thread
participants. That also stops the owner's same-subject notes to
different people from merging. Monthly mail between the same pair
(invoices from one vendor) still chains while each falls within 60 days
of the last; not in #205's scope. `make baseline` unchanged: the
synthetic corpus has no shared-recipient-only case. Threads already
merged split only on reindex. No schema change. Review round 1
(Codex): a multi-author From counted only its first author, in the
check and in thread participants, so a co-author's follow-up split off;
every author now counts.

### 2026-09-28 — Thread reprocessing (#204)

Reprocessing an already-indexed message no longer re-resolves its
thread from headers (#204): a reply indexed before its parent, when
reprocessed, moved to the parent's thread in the map while its chunks
and the old thread row still claimed it. The startup rename sweep now
runs before the initial walk, so files mbsync renamed while the indexer
was down are no longer reprocessed as new mail at all. Databases
already split by #204 keep the inconsistency until the Phase 2 reindex.
The Message-ID conflict fix (#217) moved to its own PR after review
showed takeover needs a real design. No schema change; `make baseline`
unchanged. Review round 3 (Codex): moving the sweep onto the startup
path exposed that it rescanned a folder for every stale path (quadratic
in files renamed while the indexer was down); each sweep now lists a
directory at most once.

### 2026-09-28 — Ingestion completeness fixes (#203, #206, #207, #212, #213)

Five Codex findings where valid mail could silently lose indexing, or
the indexer could fail to start. A Maildir rename now moves the file's
`indexing_jobs` row with it (#203): a reply whose Phase 2 was deferred
by an embedder outage lost its job on a flag rename and was left
without chunks and with no pending or dead row. `PermissionError` at
parse is deferred every 60 s without spending an attempt for a day
after enqueue (#212): mbsync relaxes permissions only after a whole
sync, so a long sync dead-lettered valid mail. The deletion reaper
picks survivors by message ID rather than a tombstone snapshot's path
(#213), so a flag rename mid-reap can no longer leave a deleted
message's text in its thread, and it scrubs embedding errors in its log
(#206). The initial schema and its version stamp are created in one
transaction (#207), so an interrupted first start no longer blocks every
later one. Known limit (#203): a rename during that message's own step
leaves the moved row one extra attempt and one run alone. No schema
change; `make baseline` unchanged.

### 2026-09-28 — Interrupted messages dead-letter (#235)

A message that crashed or hung the single worker never reached
`mark_failed`, so it was re-claimed at the same attempt count after
every restart, forever (and a hang never restarted at all: Compose does
not restart unhealthy containers). The one message whose parse or
chunk/extraction step is running now carries one attempt while it runs
(`begin_attempt`), refunded when the step returns or its outcome is
recorded; a process that dies mid-step leaves only that message
charged, never its batchmates, and exhausted rows are dead-lettered
with `last_stage = 'interrupted'`. A stall guard thread exits the
indexer when one step runs past `INDEXER_MESSAGE_TIMEOUT_SECONDS`
(default 3600, `0` disables) so the restart policy recovers it. The
refund is deliberately not in a `finally`: `MemoryError` /
`RecursionError` re-raised by the extractors must keep the charge. No
schema change. Review round 1 (Codex): the charge and refund were
read-then-write, so a concurrent re-enqueue from the watchdog thread
could be overwritten with a stale count or dead status (now conditional
SQL under the connection lock); the stall guard could exit on a stale
in-flight reading after the step had already refunded (it now decides
and exits holding a lock the refund takes); and an OOM from a whole
batch's footprint was blamed on the row it landed on, replayed in the
same order after each restart until a valid message was dead-lettered
(a row left marked `interrupted` now runs alone first). Review round 2
(Codex): a kill during the bulk embed or vector commit came after every
charge was refunded, so the same batch could crash forever (several
survivors are now marked `interrupted` without a charge before the
embed; a lone survivor stays charged through it); and the 3600 s limit
bounded a whole message, below three legitimately slow scanned PDFs
(each attachment now restarts the guard's clock). Review round 3
(Codex): a lone survivor is watched through the bulk embed, where a
large message's many embed requests only refreshed the heartbeat, so a
healthy message on a slow embedder could be killed; each completed
embed request now also restarts the guard's clock.

### 2026-09-28 — Bounded indexer work on hostile input (#202, #211, #216, #218, #221)

Five Codex findings where one crafted message could stall the single
indexing worker or silently blank later bodies. The chunker's sentence
regex is linear on long punctuation runs; Subject and raw-From
encoded-words decode in one linear pass (plain text next to an
encoded-word no longer gains doubled spaces); each HTML body and
attachment gets a fresh `HTML2Text`, since a shared one carried an
unclosed `<style>` into later documents; XLSX extraction stops at
20M visited cells, padding included; PDF OCR lowers its DPI so the
largest page fits 10M pixels (failing closed when page sizes are
unreadable), and `INDEXER_OCR_TIMEOUT_SECONDS` also bounds the Poppler
render. Remaining gap: pdf2image does not pass that timeout to its
`pdfinfo` page-count call. Separately, a job that hangs or kills the
worker is re-claimed without spending an attempt, so it never reaches
`dead`; that fix is a queue-semantics change of its own. No schema
change; `make baseline` unchanged. Review round 1 (Codex): the pixel
budget counted page area, not the rounded whole-pixel sides Poppler
allocates, so a sliver page kept 200 dpi at 1 x 40M pixels; and
whitespace was dropped next to a malformed encoded-word look-alike
(`ok =?utf-8?x?bad?=` fused), now only inside runs of valid words
(display names too).

### 2026-09-28 — Retrieval regression baseline (Phase 1.5)

A 20-thread, 37-message synthetic mailbox
(`indexer/tests/baseline/corpus.py`, reserved `.example` domains, text
and HTML attachments, a forward, cross-folder replies, distractor
threads) is indexed by the real `initial_index` with a deterministic
hashed embedder (words + character trigrams → 4096 dims), then queried
through mcp-server's real `hybrid_search` (no reranker) and
`query_messages`. Two layers:

- **Golden checks** (`mcp-server/tests/baseline/golden.json`): 29
  search questions with a per-question max rank, evidence-substring
  checks for attachment and multi-message cases, and four
  vector-only questions (misspellings / re-split compounds that porter
  stemming does not map back, asserted to be found with no `*_fts`
  lane) plus an MRR floor; 7 enumeration questions with exact message
  sets.
- **Rank snapshot** (`snapshot.json`): top-10 order per question, exact
  score ties ordered by thread ID; any change fails until regenerated
  with `make baseline UPDATE=1`.

Verified both layers bite: disabling the vector lanes fails the
vector-only questions, MRR and the snapshot; changing RRF `k` fails only
the snapshot. The two services cannot share a process (both are a
top-level `src` package), so the indexer step also writes the query
vectors and mcp-server never re-implements the embedder. The hashed
embedder measures plumbing, not semantic quality — that stays with
Phase 3's evals. No schema change, no new dependencies.

### 2026-09-28 — Source integrity exposure (Phase 1 item 7)

Every message row (`get_thread`, `get_message`, `query_messages`),
evidence chunk (`get_evidence`), and attachment hit
(`search_attachments`) now carries `source_file`: `source_type`
(`maildir_message`), `locator` (the raw file's `/maildir/...` path),
`sha256` and `size_bytes` of the raw file, and `indexed_at`, read from
`messages` in the same query as the result. An attachment's source is
the message file that carries it. `get_message`'s prose names the
source file too. The chain is answer → evidence chunk → `message_id` →
`source_file` → raw bytes. Two departures from the planned field list:
no separate `source_id`, since the SHA-256 already is the content
identity and a second name for it would add nothing; and `indexed_at`
rather than `ingested_at`, because the indexer rewrites the record
whenever the thread is re-indexed, so it is not a first-ingest time.
No schema change.

### 2026-09-28 — Doc-drift sweep (Phase 1 item 8)

`architecture.md`'s search section now describes the five retrieval
lanes the code runs, in two RRF stages: three FTS5 lanes
(`thread_fts`, `chunk_fts`, `attachment_fts`) fuse into the keyword
list, which then fuses with `thread_vec` and `chunk_vec`. Its data-flow
table list covers the chunk, message, attachment, queue, and ingestion
tables. README drops "Agentic" and "Real-time" and states the 4096-dim
embedder requirement instead of "any compliant provider". The
Troubleshooting half of `setup.md` moved to `docs/troubleshooting.md`.
Bridge doc nits: the `bridge-data` volume row now separates auth
material (`config` + `gnupg` + `pass`, backed up together) from the
Gluon cache (optional but hours to rebuild); `setup.md` says what to do
when `make bridge-upgrade-check` fails and what to expect after an
upgrade (Gluon re-sync, the `bridge-v3` vault path, cert pin rotation).

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
`.mbsync-last-sync.json` at the Maildir root, renamed into place from a
temporary file named after that sync. The indexer acknowledges a sync
only once its deliveries are queued (when its watcher handles that
rename, which comes after the sync's delivery events, or after a
Maildir walk that started later), and records the latest acknowledged
sync with its own liveness in the one-row `ingestion_state` table on
each health heartbeat, at most every 30 s. Schema v21, first
post-squash migration `0021_ingestion_state.sql`. mbsync now rejects a
non-integer `SYNC_INTERVAL` at startup. Known gap: a missed filesystem
event is invisible to `current` until the periodic rescan enqueues it.
Review round 1 (Codex): the indexer copied whatever stamp was on disk,
which can run ahead of the watcher's queue (now acknowledged per
rename event / walk); liveness was refreshed once per drain pass, so a
long OCR batch could exceed the 10-minute threshold (now on every
health heartbeat); jobs deferred during an embedder outage keep
`attempts = 0` and were counted as pending (now retrying, by failure
class); two test `type: ignore`s replaced with `monkeypatch`; dead
messages were described as not searchable, but one that fails after
Phase 1 keeps its keyword-searchable thread text (now "incompletely
indexed").
Review round 2 (Claude): mcp-server restarted on this build before the
indexer ran migration 0021 failed the whole status tool on the missing
table; it now reports "the indexer has not reported" instead.

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

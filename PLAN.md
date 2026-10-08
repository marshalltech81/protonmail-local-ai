# PLAN.md

## Purpose

This file records **direction, roadmap and decisions**: what the
system is for, the order of the roadmap phases and why, the owner's
rulings, what is not done or deferred, and the known limitations.

Current work lives in **GitHub issues**, grouped by milestone (one per
remaining phase, plus operations) and labelled `P0`–`P3` for priority
and `decision` for questions waiting on the owner. Work logs, review
rounds and PR chronology stay in issues, PRs and git history. A PR
edits this file only when a decision, a roadmap item's status or
scope, or a known limitation changes. Permanent constraints belong in
`AGENTS.md`; design and operational detail in `docs/`.

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
MCP API surface.

"Immutable" means source content is never mutated beneath derived
knowledge — not that users can never delete. Deletion/retention
semantics are a roadmap item (Phase 4 item 4).

## Current State

Live since the first deployment on 2026-10-03. The index was rebuilt
from Maildir on 2026-10-05 with the initial scan oldest first (#754),
OCR off until #698 is decided.

The stack runs three containers beside the Proton Mail Bridge app:

- **Proton Mail Bridge** — the official app on the host (#497; the only
  mode since the Bridge container was removed, #716), IMAP on the
  host's loopback over implicit TLS, reached through
  `host.docker.internal`. Tested on macOS; Linux is not supported.
- **mbsync** — on Debian trixie with isync 1.5.1 (#833); pull-only
  into Maildir, `chmod go+r` after each sync;
  no trust on first use: the certificate must match the
  operator-supplied `BRIDGE_CERT_FINGERPRINT` on every start, then a
  persistent pin.
- **indexer** — parses Maildir, threads, embeds through any
  OpenAI-compatible `/v1/embeddings` provider, writes SQLite (schema
  v1 since the per-extractor extraction cache, #928; numbered
  migrations for any change since the first deployment; 4096-dim L2-normalized vectors; per-message records
  keyed by claimant ID).
  Initial scan and steady state drain one durable `indexing_jobs`
  queue through a two-phase batched path; indexed mail is reparsed in
  place through the same queue when a parser change needs it (#1078),
  with fresh mail going ahead of the backlog (#1142).
- **mcp-server** — hybrid five-lane search (thread FTS, chunk FTS,
  attachment FTS, thread vec, chunk vec → RRF, optional Cohere
  rerank), exhaustive `query_messages`, and intelligence tools
  (`INFERENCE_MODE=none` by default, #750). Streamable HTTP at `/mcp`
  only, behind a static bearer token, on localhost:3000.

Inference and embedding endpoints are operator-supplied; the project
ships no model-serving components. Host-side servers keep retrieval
traffic on the box; remote providers cross that boundary. Both
postures are documented, neither is provisioned.

## Roadmap

Phases run in order; a later phase starts only when the earlier one's
exit criterion holds, except where a decision below moves a slice
ahead. Remaining work is linked by issue; each phase has a milestone.

### Phase 0 — Corpus integrity and security invariants

**Status: done (2026-09-27).** Follow-ups: milestone *Corpus and
contract follow-ups*.

**Exit criterion:** every source that exists in the synchronized
Maildir eventually reaches one visible terminal ingestion state, and
transient infrastructure failures never cause permanent source
omission.

Items, all done: 1 watcher-before-drain; 2 periodic Maildir
reconciliation; 3 failure taxonomy; 4 batch failure isolation;
5 outage circuit breaker; 6 `requeue-dead`; 7 robust
untrusted-content serialization; 8 logging privacy; 9 `MCP_PORT`
wiring; 10 Bridge source pinned by commit (obsolete since #716);
11 health threshold and fenced JSON.

Follow-ups on corpus completeness and correctness:

- threading: #752 (replies indexed before their root stay split;
  count split threads on the rebuilt index first), #756 (subject
  fallback chains recurring same-subject mail; counts posted
  2026-10-07: 694 of 5,979 multi-message threads have a subject-joined
  message, 3 span more than 180 days; owner picks an option)
- attachment coverage: #691 (optional decoders; fontTools waits on
  py-pdf/pypdf#4156, and the CFF-font PDF gap is pinned by a strict
  xfail), #695, #923 (formats with no extractor), #947
  (PPTX charts and SmartArt; the `.pptm`, `.ppsx`, `.potx`, `.ppsm`
  and `.potm` variants are routed since #1043 and #1068)
- bounded work: #781
- index-side Unicode normalization left from #316: #782

### Phase 1 — Truthful MCP contract

**Status: done (2026-10-02).**

**Exit criterion:** an unfamiliar LLM can query the corpus without
guessing about semantics, completeness, or identity.

1. `query_messages` — done.
2. Structured MCP output — done.
3. Message-first-class retrieval — done.
4. Honest `get_mailbox_status` — done.
5. MCP endpoint auth — done (#573): `/mcp` requires the static bearer
   token (Resolved decisions 13).
6. Dead action/IMAP surface deleted — done.
7. Source integrity exposure — done.
8. Doc-drift sweep — done.

Follow-up: #755 (filters select whole threads, so a passage from a
message outside the filter can be cited). Decided: label passages
(Resolved decisions 29). Implemented (#861): `in_scope` / `context`
labels in `ask_mailbox` and `get_evidence`, the prompt's scope line
naming the filters (#779), the context-only citation check and the
context label on the `get_message` / `get_thread` thread-text
fallback. Measured over three runs per side against the post-#837
baseline (same judge): judge correctness 99 to 108 of 114 case-runs,
groundedness 94 to 103, deterministic passes 92 to 96; the
narrow-filter cases rise from 13 to 20 of 21 on correctness. In the
decoy cases the answer now gives the in-scope value and names the
decoy as context; the deterministic `must_not_include` check still
fails them, since it sees only the value's presence (#894). The labels
now also cover `extract_from_emails` and the experimental tools (#895;
#915).

### Phase 1.5 — Minimal regression baseline

**Status: done (2026-09-28).** `make baseline` and the CI job
`retrieval baseline`. Phase 2 must demonstrate behavior preservation
against it: refactor PRs show no snapshot diff; behaviour changes show
a reviewed snapshot diff with golden checks still passing.

### Phase 2 — Swappable embedding / vector generations

**Status: in progress (first slice only).** Milestone *Phase 2*.

**Exit criterion:** changing embedding models never requires altering
the source corpus (the Maildir) or the versioned schema — no numbered
migration and no `SCHEMA_VERSION` bump. Everything derived is
disposable and regenerable: a context-compatible model switch
regenerates vectors only (runtime-managed `gNN` tables exempt from
`SCHEMA_VERSION`, item 2); an incompatible one regenerates chunks and
vectors through the staged rebuild below.

**First milestone narrowed (2026-10-05, external review; #719).**
Before building the live-generation lifecycle, measure rebuild
duration, resources and acceptable downtime, and ship a validated
staged rebuild with a controlled cutover, recording dimension and
tokenizer explicitly. Pursue live generations only if measurements
show the maintenance window is unacceptable, without weakening their
consistency requirements. Deferred while the embedding model is
unchanged (owner, 2026-10-05).

1. **`vector_generations` registry** — provider, resolved endpoint,
   model, revision, dimensions, tokenizer, context window,
   chunk_config_hash, status (building / caught-up / active /
   retained / retired). Dimension read from metadata, never a
   constant. **First slice done** (#650): the embedder identity record
   and calibration vector, checked at startup by both services.
   Remaining: #719 (detected size, tokenizer), #648 (periodic
   calibration re-check), #661 (keep SQLite-only tools up when the
   check cannot run).
2. **Per-generation vec tables and the blue/green lifecycle** — only if
   #719's measurements call for it (design notes on #719). Seven review
   rounds on PR #307 showed it needs a reviewed design document,
   `docs/design/vector-generations.md`, before any implementation. It
   must cover: generation identity (resolved endpoint, model,
   dimension, revision, a non-secret configuration label, and a
   calibration vector re-checked at startup and periodically, refusing
   reads and writes on drift); at most two live generations, both
   services holding both configuration sets (`EMBED_*`,
   `EMBED_NEXT_*`) and failing closed unless every live generation has
   a matching client, activation by registry change plus restart;
   reusing stored chunks only when the candidate tokenizer fits every
   stored input, by counting, with a supported query length; #283's
   evidence-recall eval as the activation gate; one generation per
   query (embed outside any transaction, re-read the ID in the
   snapshot, retry once, else fail closed); dual-writes enabled before
   the build watermark, backfill ordered behind concurrent mutations,
   and every chunk change, deletion and survivor mean applied to all
   live generations in one transaction. Two owner-approved exceptions
   (2026-09-30), recorded in AGENTS.md by the design PR: runtime `gNN`
   tables outside `SCHEMA_VERSION`, and a second embed Docker secret.
3. **Stage-aware pipeline manifest** — a canonical manifest of parser /
   normalizer / chunker / embedding identity and config hashes,
   `pipeline_config_hash = sha256(canonical_manifest)`, so a change can
   be classified as re-embed, rechunk or reparse without building
   coexistence machinery (#786).
4. **Chunk `kind` tags** (body / quote / signature / forwarded /
   calendar), with semantic segmentation before chunking. **Tags
   stored** (#659, in v0); using them in retrieval is #660.
5. **Spike: retire hand-rolled address parsing behind the existing
   bounds** (#787). Review established on 3.14 that the
   initial `compat32` parse, the encoded-word scanner, the address
   pre-screen (`_split_address_list`), the attachment classifier and
   `_safe_decode` all stay. What is left: parse each element
   `_split_address_list` yields with `email.headerregistry` instead of
   `_parse_addrs`, possibly retiring `_format_address`, as a
   differential over every parser fixture and encoding-parity shape. A
   helper is retired only where the differential is equivalent and its
   bounds survive; if nothing retires without a new bound, close it
   and record why. A retired helper lands with a rebuild.

**Rebuild bundle.** Fixes that change chunk IDs, bodies or message
identity share one rebuild rather than one each, each still its own
reviewed PR, gated behind the pipeline configuration if it must land
before the rebuild. Everything bundled so far landed directly before
go-live (#208, #550, #297, #298, #295, #303, #217; #304 unneeded after
#349). Open: #782.

**Sequencing (2026-09-30, #283).** The blue/green lifecycle validates
a generation against the old one, which needs evidence recall, not hit
rate; so the Phase 1.5 baseline plus #283's evidence-recall slice
(done, #452) precede the first generation.

**Two kinds of reindex.** A context-compatible model switch is a
table switch inside the live file (items 1–2). A chunk-ID change is a
**staged rebuild with a file swap**: the new image builds
`mail.next.db` from the source corpus beside the live file and is
validated there; at cutover mbsync and the indexer are quiesced, a
final watermark is drained, and every source file is reconciled
(unresolved or dead-lettered work rejected unless the operator approves
each skip); both writers stop and both WALs are checkpointed, and the
MCP server is drained, before any rename; the swap is crash-consistent
(hard link to retain the old file, one atomic rename, directory
`fsync`, a durable marker startup resolves); rollback is the reverse
swap onto the previous image, valid only if the retained file is kept
in sync; the live database is never migrated in place, and rebuilding
an old `pipeline_config_hash` is not a rollback; the gate measures a
representative full rebuild including peak storage, with a free-space
preflight.

### Phase 3 — Measurement and product vertical slice

**Status: in progress.** Milestone *Phase 3*.

**Exit criterion:** we can objectively measure whether the system
answers real knowledge questions, and identify why failures occur.

1. **Agent-level evals** on the synthetic mailbox — tool selection,
   arguments, retrieval and evidence recall (apart from hit rate),
   citation accuracy, pagination, unnecessary calls. **Partly done**
   (#452, #494, #560; answer evaluation #658). The `ask_mailbox`
   answer contract (#811, #817) raised the synthetic answer eval from
   17/34 to 28–29/34 deterministic passes with no regressions. The
   exhaustive-workflow tool guidance (#800, #816) awaits a live
   before/after run (#803).

   The answer eval now has 38 cases: evidence-scope decoys for #755,
   and `disclose_missing` grading on the server's coverage note (#820,
   #822). Its judge can run on the Claude or Codex subscription CLIs
   (#806, #810). One run varies by about ±1–2 cases, so comparisons use
   three runs per side, per case. The judge sees each passage's header
   (sender, date, scope label; #837, rubric 5); the current baseline is
   32, 30 and 30 of 38 deterministic passes before #755's labels and 32
   of 38 in each run after them.

   `make eval-answers` prints its planned answer and judge calls
   before it runs, caps them with `EVAL_MAX_CALLS` and stops at the
   first billing or usage-limit error (#839, #866).

   Remaining: #283 (pass thresholds, live-client trace replay, latency
   and cost), #655, #656 (the `summarize_thread` adapter is done;
   `extract_from_emails` is #1137, the experimental tools remain), #657, #818, #826, #834, #894 (decoy cases
   fail `must_not_include` on a correct answer), #897 (one judge error
   marks a run incomplete). Real-mail failures to explain: #774, #776. Evals use the synthetic corpus only; questions from real
   mail would send mail to the model provider and wait on an owner
   decision (held 2026-10-02, #785). Answer quality
   stays a manual grade.
2. **Latency instrumentation before performance redesign.** Stage
   timings done (#458). Remaining: benchmarks, request-level budgets
   and cancellation on real mail (#287). Measure before touching KNN
   architecture: if inference dominates, vector work will not help.
3. **Experimental ephemeral `brief_issue`** — chronology, actors,
   positions, decisions, open questions, conflicting evidence, every
   assertion cited, nothing persisted; behind
   `MCP_EXPERIMENTAL_TOOLS=true` (Resolved decisions 12). **Done
   (experimental)** (#466, #493). Accuracy and abstention scoring:
   #291.
4. **Adversarial injection suite** — hostile fixtures asserting the
   Phase 0 serialization holds under real tool flows. **Done** (#448,
   #534).
5. **Thread-vector weighting and rerank value** — attachment chunks
   dominate the thread-vector mean; reranking is unmeasured. **Not
   started**; needs a real embedder and reranker: #288, #289, #766.
6. **Prompt context budget with coverage disclosure.** **Done** (#445,
   #496). Retune from measurements: #487 (live answers dropped 8–22
   passages to the budget).
7. **Semantic recall under selective filters.** **Done** (#440, #470)
   with candidate expansion; eligible-subset scoring is next if evals
   still show loss.
8. **A validated citation contract for today's answers** — stable
   evidence IDs, claim→citation checks, quote verification, one
   repair, in every citing tool. **Partly done** (#457, #495, #559,
   #565). Remaining: semantic support (a model judge), #284; long
   correction, conflict and multi-thread answers still uncited after
   the repair (#819); the truncation notice gives the wrong setting for
   a context-window stop (#890).

Also in this milestone: #764 (default inference model).

### Phase 4 — Deterministic knowledge scaffolding

**Status: mostly done; item 5 in progress.** Milestone *Phase 4*. Items 1 and 2 were built
before go-live, so their tables are in the v0 schema; any later
Phase 4 schema change needs a numbered migration.

1. **Entity resolution, phase 1 (deterministic):** address
   canonicalization, display-name clustering, domain→organization;
   never merged by display name alone, no model-suggested merges
   (Resolved decisions 12). **Done** (#459, #527).
2. **Source authority metadata:** `source_type` / `authority_class`
   from an operator rules file only (Resolved decisions 12), filterable,
   **never silently folded into ranking weights**. **Done** (#459;
   Spam guard #474). Remaining: verdict gating on Proton's
   authentication headers, #463 (decided, Resolved decisions 19).
3. **Richer temporal retrieval:** `sent_at`, `occurred_at` (top
   `Received:`, confirmed as Proton's on live mail) and the effective
   time `COALESCE(occurred_at, sent_at)` for every filter, span and
   ordering (`docs/architecture.md`, "Message time"). **Done** (#593,
   #597, #599). Amended 2026-10-07 (Resolved decisions 35): the
   effective time stays the default basis; `date_basis` lets a caller
   choose `sent`, `occurred` or `internal` (#1150, #1092), and a
   missing `Date:` stays unknown instead of being dated at indexing
   (#1080). Bitemporal claims wait for Phase 5.
4. **Deletion/retention semantics.** Mirror is the default, archive
   (`INDEXER_DELETION_ENABLED=false`) the opt-in, Trash indexed but
   hidden from default search. A reaped source reads as removed for 30
   days, then not found, and its text is never kept (Resolved
   decisions 14). **Mostly done** (#451, #475, #562, #564, #583).
   Remaining: user-controlled retention (#784), local
   deletion of deleted mail's files (#728), and validating reaping on
   live mail (#783).
5. **Deterministic query language** (2026-10-07, Resolved decisions
   35).
   `query_messages` is already an exact enumerator with good address
   semantics; what it lacks is composition, an explicit clock and a
   way to say "could not tell". One leaf compiler serves the three
   filter implementations (#1084), then: `replied` and size (#1085),
   which ships the `indeterminate` count for unknown sizes, read from
   the row with no new storage; the selectable clock, `date_basis`
   (#1150, depending on #1080 for `sent`), counting its own NULL
   clocks the same way so no basis ever drops unknown rows silently;
   per-message
   content evaluability generalizing that count (#1086, before any
   negation); the bounded `all` / `any` / `negate` form with a leaf
   cap that also counts `any` groups (empty groups rejected) and
   three-valued evaluation; the form is fixed at two levels, so there
   is no nesting to bound (#1087, delivers #992 option 2; option 1,
   the documented multi-lane recipe, is done);
   explicit address-mode and `body_words` leaves (#1088); grouped
   aggregation as its own tool over the same engine (#823); leaves
   that wait on the evidence model: `header_exists` /
   `header_contains` (#1089), Bcc (#1090), `attachment_text_words`
   (#1091), `date_basis=internal` (#1092); and a capability report in
   `get_mailbox_status` derived from the registered leaves (#1093).
   Omitted by decision: IMAP UID, KEYWORD (labels are outside the
   corpus by the sync patterns, a separate corpus decision), DRAFT
   and DELETED (#955 becomes a leaf if wanted). #824, #955 and #1056
   become leaves on the compiler. **In progress:** the predicate
   module (#1084) and `replied` and size with their `indeterminate`
   count (#1085) are done; the rest is open.

### Phase 5 — Knowledge reasoning

**Status: not started, except item 2.** Milestone *Phase 5*.

1. Hardened `brief_issue`, informed by Phase 3 usage on real mail.
   **Not started** (#788).
2. Support / contradict / qualify / supersede analysis as a
   query-time tool, experimental like `brief_issue`. **Built
   (experimental)** as `check_conclusion` (#467), with quote
   verification (#558). Remaining: semantic support, #284.
3. Temporal position/change reasoning ("position as of date X" vs
   "current position"). **Not started** (#789).
4. **Only then** evaluate persisted claims/events — and only under
   these rules (**not started, by design**; no issue until 1–3 land):
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

### Operations and hardening

Not a phase: the running deployment's resource, throughput, privacy
and supply-chain work. Milestone *Operations and hardening*: #488,
#697, #698, #777, #778, #767 (a CycloneDX SBOM per image is done; the
dependency license check is open), #769. Done: #765 (at-rest protection
documented as a setup requirement, #851), #768 (`make status`
shows each provider as LOCAL or REMOTE, #830), #780 (#829), dependency
and base-image digest refresh (#828), mbsync on Debian trixie
(#833), and, 2026-10-08: `make trivy-images` for the image scans with
a stale-image warning (#1065, #1103), fewer Maven Central contacts
(the Trivy dependency scan caches the Maven repository, #1069, and
pull-request image builds and the indexer test job restore the `.ppt`
reader's build stage from the CI cache, #1070, #1105; runs on `main`
and cold local builds still need Maven Central), a check that both
workflows pin the same BuildKit image (#1122), and defaults for
optional Compose settings (#1074). Open decisions: the runtime base images (#835), and bounding
review rounds for test and eval-harness PRs (#838). Until the owner
decides #835, the AGENTS.md constraint "Do not switch runtime images to
Alpine" stands, and no image moves to Alpine or distroless. #835 records
the options, distroless included, for that decision only.

**Observability.** AGENTS.md now requires degraded behaviour to be
visible in the logs (#880): every fallback, cap, skip, retry or partial
result logs at INFO or above, recoveries are logged, and a tool call
that degraded says so on its own line. Done: token-limit hits,
degraded-retrieval markers and rate-limited `/mcp` rejection logging
(#865, #877, #878; #883), a completion line for every MCP tool (#886;
#892), and mbsync sync-success and folder-name-safe repair logging
(#879; #881), and rate-limited logging of attachment extraction outcomes, the OCR
page cap and the parser's body and address caps (#871, #872; #884).
Done 2026-10-07: embedder retries and recoveries, the queue heartbeat,
maintenance summaries and storage size (#873–#875; #904), the startup
identity line (#887; #893), header truncation (#902; #913), every
extractor truncation cap with a completeness test (#903; #917), the
multipage TIFF OCR cap (#885; #916), one shared rate-limited logger per
service (#889; #933), reconciler errors logged by type only (#934;
#940), a per-checkout uv cache (#896; #912) and Trivy scans of the
built images, gated on fixable HIGH/CRITICAL findings (#977; #1015,
after #1006/#1007 refreshed the base digests and removed pip from the
runtime images). Open: the OCR cap on cache hits (#891), stale
extractor-version counts in status (#979, decision), the unscanned
jlink runtime (#1008) and a merge gate on a Codex review of the head
commit (#978). Done 2026-10-08: the indexer exits when the Maildir
watcher thread dies, so the restart restores it, and counts the
directories its walks cannot read (#870). Open decisions: telling a
stall from a backlog in status (#876; parked trashed files currently
show as "retrying"), and correlation IDs (#888).
Every MCP tool declares safety annotations (#899; #900; Resolved
decisions 31).

### Evidence model

Not a phase: cross-cutting work to capture source semantics losslessly
at the Maildir boundary, so deterministic queries are grounded in
preserved evidence rather than reconstructed from it (owner and Claude
correspondence, 2026-10-07; Resolved decisions 35). Milestone
*Evidence model*. Principle: capture source semantics losslessly at
ingestion, preserve immutable evidence locally, derive search
structures from that evidence. The product boundary stays Bridge →
mbsync → Maildir → indexer; mbsync is not replaced (Deferred).

1. **Parser seam** — `parse_email_bytes(raw, source_metadata)` with
   Maildir as the adapter; no behaviour change (#1077). **Done.**
2. **Reparse class** — an in-place full reparse through the job queue
   for changes that keep chunk IDs, the executable form of #786's
   "reparse" (#1078). **Done:** `reason = 'reparse'` jobs and
   `make reparse`, with fresh mail ahead of the backlog (#1142); no
   column has used it yet. Each schema change below is its own
   PR with its own migration and `SCHEMA_VERSION` bump (AGENTS.md:
   schema-adjacent fixes get their own PR); a new column reads as
   unknown, and the capability report (#1093) says "supported after
   backfill", until a reparse fills it. Changes that land close
   together may share one reparse run, never one migration.
3. **All headers** — a `message_headers` table keeping duplicates and
   order under one aggregate budget (fields, bytes, largest value in
   one guard); values never reach logs (#1079). Keyed by claimant ID,
   and added to the AGENTS.md per-message-row list by the same PR. The
   same migration stores whether the budget was hit, so a header
   predicate on a capped message is indeterminate, not false. Narrows #825 to
   normalization; prerequisite for #463 options 2 and 3.
4. **Unknown dates stay unknown** — nullable `sent_at` with a status;
   `effective_at` remains the ordering fallback, documented as not
   evidence (#1080).
5. **Arrival time** — experiment: `CopyArrivalDate yes` in the mbsync
   template, a layout test proving the mtime equals Bridge's
   INTERNALDATE, `internal_at` with an `unavailable` status for files
   that predate the option (#1081). `CopyArrivalDate yes` is on, and
   isync's INTERNALDATE handling is checked over IMAP against a test
   server (#1132); which date the Bridge app reports is unverified
   (#1138), and `internal_at` waits on it. Recovering it for existing mail
   would be a cold re-pull, an explicit operation, not part of this.
6. **Content-hash identity** — whether messages without a usable
   Message-ID are indexed instead of dead-lettered (#1082, decision,
   open). Adopting it changes the AGENTS.md claimant-ID and
   thread-membership constraints, so the decision must define the
   synthetic message and thread identity (such a message never becomes
   a thread other messages resolve to by ID, and two of them never
   merge by an empty ID) and update AGENTS.md in the same PR. Until
   then the claimant-ID invariant stands unchanged.
7. **Occurrence model** — one `messages` row is one occurrence while
   Proton folders are exclusive and the virtual folders are excluded;
   the `,U=` in a Maildir file name is isync's near-side UID, never
   read; `.mbsyncstate` is never parsed (#1083, decision). Two
   byte-identical files claiming one Message-ID share a claimant ID
   and today collapse into one row that tracks a single path, so
   removing that copy can reap mail whose other copy survives (#1102,
   P1); "one row per occurrence" does not hold for them until #1102 is
   fixed.

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
  data sources.
- **Per-session inference-mode toggle.** Complexity with no pull;
  per-deployment is enough.
- **Guarded live Bridge integration CI.** Requires a dedicated paid
  Proton test account and hardens the ingestion edge rather than the
  product. The synthetic mailbox buys more quality per hour.
- **Investment in Bridge operations.** Bridge is the project's
  existential *liability*, so it gets maintenance, not investment;
  since #716 it is the vendor's app on the host. Boundary principle:
  the knowledge system knows nothing about Proton Bridge — the stable
  contract is Bridge → mbsync → **Maildir** (product boundary) →
  indexer, which leaves room for Maildir/mbox import or other mail
  connectors later.
- **mypy → pyright migration** — wait for a real trigger (mypy
  slowness, a missed bug, cross-project friction), not a pre-emptive
  sweep.

## Deferred (not dead — revisit on a real trigger)

- audio/video transcription via Whisper (model storage + compute
  cost; trigger: a clear use case)
- near-duplicate handling; saved queries / persistent monitors;
  waiting-on-me / unanswered-thread views (trigger: knowledge
  scaffolding in daily use)
- `content_blocks` as a persisted table (trigger: a non-chunking
  consumer needs it; Phase 2 item 4)
- parser/chunk generation coexistence machinery (the pipeline
  manifest preserves stage identity meanwhile)
- attachment download support (trigger: the read-only action-path
  decision it was always gated on)
- replacing the stdlib MIME parser (decided 2026-09-30 on PR #260):
  Python's `email` package never exposes raw part offsets, so an
  attached email's identity is the hash of its re-serialized form; a
  hand-rolled boundary slicer was reverted after four review rounds.
  The one real candidate is Stalwart's Rust `mail-parser` via pyo3 —
  an architecture change with a full reindex. Trigger: hostile-mail
  robustness becomes a goal in its own right, not a bug fix
- hosted or remote MCP clients (ChatGPT connectors, other devices):
  need the design in Resolved decisions 13 and a privacy-posture
  update in AGENTS.md first. Trigger: the owner wants such a client
- a cheaper chunking ceiling (#684 made splitting linear; a 12 MB body
  still takes 10–29 s): a per-message text budget or a tokenize-once
  chunker. Trigger: a real stall on live mail
- newest-first initial indexing as an option. Trigger: #752 lands
- reporting child folders skipped under isync's reserved names
  (needs an extra IMAP listing). Trigger: a real report
- replacing mbsync with a read-only IMAP acquisition layer (decided
  2026-10-07, Resolved decisions 35): Maildir already carries folder,
  flags and (with `CopyArrivalDate`) the arrival time, and the
  evidence-model work needs none of Bridge's UIDs; a Python IMAP
  client against Bridge's Gluon server would re-open the TLS and
  fingerprint path, add a fourth container and a new untrusted-input
  surface, and reverse the Maildir product boundary. Bridge `UID
  SEARCH` is a differential test target for simple predicates, run by
  hand and read-only, never an oracle for BODY/TEXT semantics.
  Trigger: an IDLE-latency requirement, or a sync defect isync cannot
  handle

## Operational baseline

- Python services use per-service `uv` projects with pinned
  `pyproject.toml` + `uv.lock`. Both meet a 90% coverage floor in CI,
  `src/main.py` included (#757).
- Bridge is the official Proton Mail Bridge app on the host, which
  updates itself (#716); mbsync requires its certificate fingerprint on
  every start.
- All long-running services run as non-root with `cap_drop: ["ALL"]`,
  `no-new-privileges`, read-only root filesystems, `pids_limit`,
  `init: true`, and Docker log rotation (#501).
- Bridge password lives in `.secrets/bridge_pass.txt` (Docker
  Compose secret), never `.env`.
- Deletion reconciliation (mirror) is on by default
  (`INDEXER_DELETION_ENABLED=false` selects archive) with a grace
  window, mass-delete brake, and atomic reap-or-rollback.
- A durable `indexing_jobs` queue retries transient failures with
  exponential backoff and dead-letters persistent ones for operator
  visibility.

## Known limitations

- initial sync may take a long time on large mailboxes
- attachment OCR assumes English (#490); OCR is off on the live
  deployment until #698
- Linux hosts are not supported out of the box: `host.docker.internal`
  (`host-gateway`) reaches the docker bridge address, not the host
  loopback the Bridge app binds (#716), and the services cannot read
  their mode-600 `.secrets/*.txt` (#652, closed as Linux-only); Windows
  Docker Desktop is untested
- the schema is effectively locked to 4096-dim
  Qwen3-Embedding-8B-shaped models (hardcoded dim, vendored
  tokenizer); the embedder's identity and a calibration vector are
  checked at startup (#650), so switching models or moving the embed
  endpoint needs a rebuild (#719); an embedder outage at startup stops
  the server (#661)
- audio/video and calendar attachments are not extracted (#695); PDF /
  DOCX / XLSX / PPTX / legacy DOC and XLS (#935) / legacy PPT (Apache
  POI in a Java process, #957) / HTML / TXT / images are
- pdf2image runs a second, untimed `pdfinfo` inside each render; the
  timed page count before it covers the realistic stall (#868; no
  pdf2image release changes this, checked 2026-10-07, so the choice
  between accepting it and a direct `pdftoppm` call is the owner's)
- `list_threads(filter_type=...)` rejects unsupported values cleanly;
  read/flagged/replied state is indexed (#649) but draft/forwarded is not
- deletion reconciliation (mirror) is not yet validated under
  long-running real-world conditions; the indexer never deletes Maildir
  files, so a reaped message's `.eml` stays on disk (#728)
- a reap overwrites freed pages (`secure_delete`, #642), and the next
  maintenance pass removes the reaped FTS5 terms with a stepped merge
  (the incremental form of `optimize`) before its checkpoint (#666,
  #670). There is no fixed deletion time. The merge is capped per pass,
  so a table it does not finish stays pending and its deleted terms
  stay in the file across further passes until a pass completes it. A
  failed merge step, a busy checkpoint, or a restart that interrupts a
  merge in progress also delays removal. A restart before the pass does
  not: the indexer scrubs every table at startup. Below SQLite, deleted text can outlive both in
  filesystem free blocks, snapshots and backups (`docs/architecture.md`).
- keyword search does not match precomposed letters with two
  diacritics or composed vs decomposed Hangul across forms (#316's
  index-side remainder, pinned by xfail tests; #782)
- a child folder named after one of isync's own files is not synced
  and nothing reports it (`docs/architecture.md`)
- chunking a very large crafted body still takes tens of seconds
  (#684; Deferred)
- tool descriptions ask an agent to tell the user before bulk or
  out-of-scope reads but cannot enforce it; a filtered read can still
  return passages and parent-thread context from messages outside the
  filter, labelled `context` (#755); the model is told to answer from
  in-scope passages, which it follows on the synthetic decoys but
  cannot be forced to

## Blockers and Risks

### Initial Proton sync duration
Large mailboxes may take hours before useful indexing begins.
Do not assume indexing bugs until Bridge internal sync has completed.

### Bridge TLS and cert behavior
The Bridge app's certificate is issued for `127.0.0.1` only; mbsync
checks it against that name through a tunnel and requires
`BRIDGE_CERT_FINGERPRINT`. Do not modify this casually.

### Schema sensitivity
Changes to SQLite schema, embedding dimensions, or thread model can
invalidate existing assumptions and stored data. Phase 2 exists to
confine embedding-model changes to disposable vector generations.

### Knowledge-layer poisoning
Any future persisted derived knowledge inherits prompt-injection risk
from attacker-controlled email. The Phase 5 rules are the control;
do not ship persisted claims without them.

## Resolved decisions

Numbers are stable; code, tests and docs cite them. Entries about the
removed Bridge container are kept as history.

1. **Read-only MCP server (2026-09-26):** no action tools, SMTP path or
   draft/move/flag operations; the unregistered action/IMAP code was
   deleted rather than kept as hypothetical capability.
2. **Live Bridge integration lane (2026-09-26):** not doing (see Not
   doing).
3. **#242 first-run retry (2026-09-30):** `BRIDGE_FORCE_CLI=true` in
   the first-run overlay. Obsolete since #716.
4. **#245 Bridge auto-update in existing vaults (2026-09-30):** a
   patch hunk forcing the update gate off, tested with the setting
   enabled. Obsolete since #716.
5. **#217 Message-ID conflicts (2026-09-30):** keep every claimant and
   expose conflicts; never pick a winner by arrival order, which is
   spoofable. Landed in v0 (2026-10-01) as the claimant ID.
6. **#292 mixed digital/scanned PDFs (2026-09-30):** page-level OCR
   selection with a `pdf` extractor version bump, with #300.
7. **#295 sequential inline text parts (2026-09-30):** document, then
   revisit with the reindex. Superseded 2026-10-01: fixed directly
   (#444) while no live index existed.
8. **#297 undated mail (2026-09-30):** keep the first persisted date
   on reprocess. Its date chain was superseded by 14 (`occurred_at`).
   **Superseded by 35 once #1080 lands:** a missing or unparseable
   `Date:` is stored as unknown (`sent_at` NULL with a status), and
   the first indexing time survives only as `first_indexed_at`, a
   non-evidentiary fallback for `effective_at` ordering and display.
9. **#276 retained near-side mbsync state (2026-09-30):** warn about a
   far-side box that cannot be opened and keep syncing the rest, never
   `Remove Near`, which deletes local mail. Done (#521); #275/#281 per
   14.
10. **#277/#282 first-sync health (2026-09-30):** separate liveness
    from freshness, so a first sync is "healthy, not yet current", and
    set the stall deadline above the observed backfill. Done (#515,
    #651); `SYNC_DEADLINE_SECONDS` is tuned per mailbox.
11. **#268 Bridge smoke-test scope (2026-09-30):** the minimal fix;
    deeper live checks are the lane in Not doing. Obsolete since #716.
12. **Result quality and Phases 3–5 (2026-10-01):**
    - until the first deployment, schema changes fold into v0 with no
      migration (ended 2026-10-03);
    - retention ships as **mirror**; archive is the opt-in;
    - `brief_issue` and Phase 5's support/contradict analysis are
      **experimental tools**, off unless `MCP_EXPERIMENTAL_TOOLS=true`;
    - #217's claimant ID is the Message-ID plus a short content-hash
      suffix; the bare Message-ID works while only one message claims it;
    - source authority comes only from an operator rules file: a model
      classifier would send mail to the inference provider;
    - entity resolution is deterministic only.
13. **Open decisions of 2026-10-02:**
    - **MCP endpoint auth:** a local static bearer token from a Docker
      secret, compared in constant time, failing closed when empty, so
      `/mcp` answers 401 before any session and `/health` stays open
      (design in AGENTS.md, `mcp-server/`; done, #573). Trust condition:
      "processes running as the operator are trusted". Not chosen, each
      needing its own owner decision and an AGENTS.md privacy update
      because mail would leave the host: a private-network gateway and
      hosted clients (an OAuth 2.1 resource server). Either must add the
      external Host/Origin narrowly to the transport-security allowlist;
      the hosted one must also set the protected-resource URL
      (`AuthSettings.resource_server_url` / fastmcp `RemoteAuthProvider`).
    - **#432:** a streaming pre-pass counting raw xlsx `<c>` nodes, with
      an `xlsx` version bump (done, #572).
    - **#498:** Streamable HTTP at `/mcp` is the only transport (done,
      #563).
    - **#497:** the Bridge app on the host through a certificate-valid
      path, never a TLS bypass (done, #571; the only mode since #716).
    - **#537:** `get_evidence` gains `max_threads` (done, #559).
    - **#533, #526, #524:** deferred; fixed later (#667, #653, #654).
    - **#494:** keep strict argument matching and first-call tool
      selection; the live-trace recorder comes after go-live (#283).
    - **#496:** keep the 3-characters-per-token estimate.
    - **#495:** keep the CJK terminators and word counts.
    - **Compose and shell scanning:** build it (done, #566).
    - **#287 request cancellation:** designed only after real-mail
      measurements.
14. **Evening walkthrough of 2026-10-02:**
    - **`occurred_at`** is the top `Received:` header's date, NULL when
      absent or unparseable, never `Date:`; date filters and thread
      spans use `COALESCE(occurred_at, sent_at)`.
    - **Date-range evidence:** any passage of a thread whose span
      overlaps the range, each with its own dates (#574 superseded);
      passage dates are read from `messages` (#575).
    - **#562:** purge unreferenced `attachment_extractions` rows in the
      reap transaction.
    - **#580:** check hardening on the merged config of every overlay
      combination (covers #577).
    - **MCP clients:** Claude Code, Codex, Claude Desktop (a repo-owned
      `fastmcp` stdio adapter instead of `mcp-remote`) and ChatGPT
      desktop, whose connectors need the hosted-client design
      (Deferred).
    - **#497 live test:** go-live with a fresh Maildir is the live test
      (passed 2026-10-03).
    - **Reaped-citation invariant:** a 30-day, content-free record (a
      permanent one would list deleted mail's domains); a reaped source
      reads as removed for 30 days, then not found, and its text is
      never kept.
    - **#428:** cap every xlsx part openpyxl loads whole, with a version
      bump.
    - **#275/#281:** collision-free folder mapping before go-live;
      **#279:** a tested UIDVALIDITY recovery procedure before go-live.
    - **#267:** not relevant once the pin is the operator-supplied
      fingerprint; the documented limitation stands.
    - **#489:** `get_message` pages the body by offset, headers capped.
    - **Message-ID length:** 998 characters; longer takes the
      no-`Message-ID` dead-letter path.
    - **`BRIDGE_USER`** stays in `.env`: an identifier, not a credential.
    - **Bridge Go module scan:** report only (obsolete since #716).
    - **#208/#550:** fix before the go-live rebuild, not in the bundle.
    - **P3 policy:** small P3s with an agreed fix and no new mechanism
      may be fixed before go-live (AGENTS.md).
15. **#638 implicit TLS (2026-10-02 night):** mbsync ⇄ Bridge IMAP uses
    implicit TLS, with no STARTTLS or plaintext fallback; an approved,
    owner-gated TLS change.
16. **v0 additions before go-live (2026-10-02 night):** Maildir
    read/flagged/replied state (#649), chunk `kind` tags (#659) and the
    embedder identity record with a calibration vector (#650) went into
    v0 to avoid a later migration; the claimant suffix grew to 16 hex
    digits (#640).
17. **Result quality first (2026-10-01):** work targets returned-result
    quality ahead of edge cases and P3s; with no live index yet, the
    reindex bundle's body fixes landed directly.
18. **Bridge container removed (2026-10-04, #716):** the official app
    on the host is the only setup, folded into `docker-compose.yml` with
    no aliases; macOS tested, Windows untested, Linux unsupported;
    dated history stays as written; `BRIDGE_CERT_FINGERPRINT` is always
    required.
19. **#463 sender authentication (2026-10-04):** a DMARC `fail` in
    Proton's own `Authentication-Results` leaves the sender
    unclassified, and a `sender_authenticated` flag (pass / none / fail
    / internal) is shown with the authority class. A single
    `X-Pm-Origin` with the exact value `internal` counts as
    authenticated (Proton overwrote a forged one in the owner's test).
    The backfill is an application step after the migration, not SQL.
20. **`INDEXER_UNLINK_ON_REAP` removed (2026-10-04, #721):** it could
    not work against the read-only Maildir mount; the indexer never
    deletes Maildir files, and local deletion belongs to mbsync (#728).
21. **Smaller rulings of 2026-10-04:** Proton's virtual `Starred` is
    left out of the sync (#746); #652 and #685 closed as Linux-only;
    the best thread keyword hit is guaranteed a top-three place in
    hybrid RRF (#701, #745); the initial scan indexes **oldest first**
    across folders, with no setting (#754), since newest first split
    replies from their roots (#752).
22. **Explicit provider destination (2026-10-05, #750):** every enabled
    layer's `*_BASE_URL` is a URL or the literal `default`; an API key
    is not consent to the SDK's default endpoint. `INFERENCE_MODE`
    defaults to `none`.
23. **Late review findings (2026-10-05, #751):** a verified
    round-three-or-later finding still blocks the merge when it is
    P0/P1 or introduced by the PR (AGENTS.md).
24. **Phase 2 first milestone narrowed (2026-10-05, #719):** measure,
    then a validated staged rebuild, before any live generations;
    deferred while the embedding model is unchanged.
25. **Work tracking (2026-10-05):** PLAN.md keeps direction, roadmap and
    decisions; work is tracked in GitHub issues with milestones and
    `P0`–`P3` / `decision` labels.
26. **No bulk mail export (2026-10-05):** agents do not copy mail out of
    the Maildir and index volumes in bulk for testing or analysis without
    the owner's consent for that run; analysis goes through the MCP
    tools in memory (AGENTS.md, "Do not export mail in bulk").
27. **Default inference model (2026-10-05, #764, #805):** the default
    `INFERENCE_MODEL` is `claude-sonnet-5-5`. The owner made the change
    without the Sonnet 4.6 comparison the issue proposed. Unset token
    settings take 16000 / 48000 (reply / window) in anthropic mode,
    where thinking counts against the reply limit, and keep 1024 /
    32768 otherwise, so a 32k local model still fits.
28. **Structured outputs for the JSON tools (2026-10-05, #808, #809):**
    in anthropic mode `extract_from_emails`, `brief_issue` and
    `check_conclusion` send their reply schema as an Anthropic
    structured-output format, behind `INFERENCE_STRUCTURED_OUTPUT`
    (default on). A provider rejection is an error naming the setting,
    with no retry without the format. Anthropic caches a schema for up
    to 24 hours, so the extraction schema carries neutral keys
    (`f1`, `f2`, …) and never a caller's field name. Schemas over
    Anthropic's measured limits (more than 8 fields, or a type list
    mixing an array) are sent as plain JSON. openai mode is #807.
29. **Evidence scope under filters (2026-10-05, #755):** retrieval keeps
    selecting whole threads and supplying their context, and each
    evidence passage is labelled `in_scope` (its own message satisfies
    every message-level filter) or `context`. The `ask_mailbox` prompt
    answers from in-scope passages and uses context only to interpret
    them; the citation check flags a filtered answer that cites no
    in-scope passage; the body-less parent-thread fallback of
    `get_message` / `get_thread` is labelled context. Labels come from
    indexed message metadata at query time, with no schema change.
    Restricting evidence was rejected (it drops the corrections an
    answer depends on), as was keeping it unlabelled (out-of-scope
    citations stay invisible). Synthetic decoy eval cases come first.
    Step 1 is done (#822): decoy cases for a sender filter, a date
    filter and a mixed Trash/INBOX thread, plus a three-run baseline.
    Step 2 also puts the active filters in the prompt as a scope line
    (folding in #779) and keeps the hidden-scope case next to its
    scope-stated companion. #837 lands first, so the judge sees the
    passage headers, and the baseline is re-run (owner, 2026-10-06).

30. **mbsync on Debian trixie (2026-10-06, #833):** isync 1.5.1,
    OpenSSL 3.5. The #570 log redaction was re-derived from isync
    1.5.1's message formats. The template uses `TLSType IMAPS`, with the
    same meaning as before. isync 1.5 decodes Bridge's modified UTF-7
    folder names to UTF-8 directory names. An existing install with
    encoded folders migrates by renaming them (never deleting) and
    rebuilding the index (`docs/setup.md`). The live mailbox had none,
    and the owner accepts a reindex if needed.
31. **MCP tool safety annotations (2026-10-06, #899, #900):** every
    tool declares `readOnlyHint: true`, `destructiveHint: false`,
    `openWorldHint: false` and its own title-cased `title`, from one
    shared constant; a test fails on a tool added without a deliberate
    classification. Operational logging does not prevent read-only: the
    logs are the server's telemetry, carry no content-bearing arguments
    or mail (only allowlisted, validated values), and are
    not an effect of the tool. `openWorldHint` stays `false` for the
    tools that call the embed, rerank or inference provider too: the
    domain is the mailbox, and egress is disclosed by the startup
    privacy warnings and `make status`. Deriving the hint from provider
    locality was considered and not chosen. Covers OpenAI's three
    required hints and Anthropic's `readOnlyHint`, `destructiveHint` and
    `title`. Accepted risk: tools that send retrieved mail to a remote
    inference or rerank provider still advertise `readOnlyHint: true`
    (nothing in the mailbox or other state changes), so a client that
    auto-approves read-only tools can send excerpts to that provider
    without a prompt; the egress is disclosed at startup and by
    `make status`.

32. **Attachment formats (2026-10-07, #694, #935, #936, #937, #957):**
    `.pptx` through python-pptx and `.dotx` through python-docx's own
    part registry, with pre-open package budgets. Legacy binary
    formats: `.doc` through `catdoc`; `.xls` through xlrd in a child
    process with its own memory and CPU limits, because python-calamine
    allocates every sheet's full grid at open (1.6–2.7 GB from a 6.6 KB
    file) and xlrd's shared-string loop is unbounded in-process; `.ppt`
    through Apache POI on a trimmed Java runtime (#957; #982),
    because `catppt` reads no slide text from current PowerPoint decks
    and office-oxide was judged too young for code beside the mail
    database. Every external parser runs through one runner (temp file,
    no shell, timeout, output cap, and address-space and CPU limits set
    by its launcher before the parser loads, #995; the OCR path's
    Tesseract and Poppler have a timeout only, #1021). Accepted risk: catdoc's
    unfixed Debian CVEs (none at HIGH/CRITICAL); CI now scans the built
    images (#977; #1015).
33. **Per-extractor extraction cache (2026-10-07, #928):** the
    attachment extraction cache is keyed by content hash and extractor
    module, so one label's result is never served to an occurrence that
    selects another extractor. It is the first change since the first
    deployment that takes a `SCHEMA_VERSION` bump (to 1) and a numbered
    migration (`0001`). Chosen over per-row special cases after #931's
    review showed each workaround creating new cross-label cases. Landed
    in #994 (also closing #986); #931 (permanent failures recorded
    `unsupported`, with the pdf/xlsx version bump kept) was rebuilt on
    it in #971. Snapshot the index before deploying a schema change
    (#1005).
34. **Review and PR rules (2026-10-07, #944, #981):** every real gap in
    a PR's "Not done" gets its own issue before the PR is called ready;
    a gap in code, tests or docs that a PR adds counts as introduced by
    it under the #751 exception.
35. **Deterministic query language and the evidence model
    (2026-10-07):** keep Maildir as the product boundary and isync as
    the sync layer; do not replace mbsync (Deferred). Preserve source
    semantics the indexer already receives but discards (all headers,
    unknown dates, arrival time; content-hash identity is a separate
    open decision, #1082; "Evidence model" above). Build the deterministic layer as one predicate
    compiler with a bounded Boolean form, explicit `date_basis`
    (`effective` stays the default) and three-valued results with an
    `indeterminate` count, established before negation (Phase 4
    item 5). Define `BODY`/`TEXT` equivalents by what the index holds
    (`body_words`, `attachment_text_words`), never by Gluon's
    behaviour. Omit IMAP UID, KEYWORD, DRAFT and DELETED. The
    assessment behind this is in the issues' bodies (#1077–#1093), not
    in the repository.

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Find current work in GitHub issues; this file is direction, not a
  queue. Labels, milestones, blocked-by relations and title shape
  follow AGENTS.md "Issue Conventions".
- The Current Objective supersedes any older scope statement that
  froze the MCP API surface; Phases 0–5 are the priority order.
- Edit this file only when a decision, roadmap status or known
  limitation changes; keep it concise.

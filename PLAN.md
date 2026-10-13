# PLAN.md

## Purpose

This file records **direction, roadmap and decisions**: what the
system is for, the order of the roadmap phases and why, the owner's
rulings, what is not done or deferred, and the known limitations.

Current work lives in **GitHub issues**, grouped by milestone (one per
remaining phase, plus operations) and labelled `P0`–`P3` for priority
and `decision` for questions waiting on the owner. Work logs, review
rounds, measurements and PR chronology stay in issues, PRs and git
history; this file names an issue only where it is a blocker or an
open choice. A PR edits this file only when a decision, a roadmap
item's status or scope, or a known limitation changes. Permanent
constraints belong in `AGENTS.md`; design and operational detail in
`docs/`.

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

## V1 Finish Line

Adopted 2026-10-08 (decision 38). **V1 is a read-only mailbox evidence
system that can enumerate, inspect, aggregate and reason over a
defined population of mail, makes missing evidence and uncertainty
explicit, and can be rebuilt from Maildir.** Issues outside these
outcomes and gates are not prerequisites.

Outcomes:

1. **Every attachment in a defined scope can be listed and read in
   full**, with paging and message and folder filters (#796).
   **In progress:** every stored occurrence can be listed
   (`query_attachments`) and its stored text read (`get_attachment`),
   and per-occurrence text completeness is recorded (#1242, done).
   Open: whether an extraction cap cut the stored text (#1261) and
   occurrences missing from a message whose attachment manifest is
   incomplete (#1282).
2. **A population can be built and counted on the server** through
   bounded Boolean filters, explicit address, body, header and
   attachment predicates, grouped counts and exhaustive thread
   grouping. Incomplete parsing or extraction answers `indeterminate`,
   never a confident "no". **In progress:** per-message completeness
   is done (#1086), explicit leaves through `where` (#1088) and the
   bounded `any` / `negate` form over them (#1087), and grouped counts
   by sender, domain, folder, year, month and authority class
   (`aggregate_messages`), and subject, header and attachment-text
   leaves in `where` (#1079, #1089, #1091), and exhaustive thread
   grouping (`query_threads`). Open: the rest of #823 (selectable clocks,
   further dimensions and identity counts).
3. **Source metadata stays evidence:** bounded ordered headers,
   unknown send dates kept unknown, verified arrival time, participant
   and Bcc semantics, authority from the stored sender only when it is
   safe ("Evidence model").
4. **Retrieval supplies the passage that answers**, not only the right
   thread: the keyword-matched passage, which message matched, and later
   corrections or closures in the thread. **In progress:** the
   keyword-matched passage is kept, ranked by in-thread word rarity
   (#858, #1246); open #987, #974.
5. **Structured extraction runs over a known set and discloses
   coverage:** explicitly selected messages or attachments, with
   omitted or truncated source text reported (#976, #1057).
6. **Reasoning over change and disagreement meets the answer-quality
   bar below.** The bar is the requirement, not a particular checking
   mechanism (Phase 5).

Release gates:

- **Corpus and privacy correctness:** no open P0 or P1 issue. Met on
  2026-10-08 (#1031, #1040 fixed); #1236 is an accepted P2 risk
  (updated 2026-10-11: PR #1355 added its per-message extraction
  budget; it stays open for child confinement, #1354).
- **Measured answer quality:** written pass thresholds for evidence
  recall, supported conclusions, chronology, abstention, exhaustive
  workflows and truncation disclosure, met on the synthetic eval
  corpus. The deterministic baseline tests wiring, not answer quality.
- **Deployed:** the release runs on the live mailbox after an upgrade,
  with any queued reparse drained and status healthy.
- **Operational bounds:** recovery verified, request deadlines, and
  resource limits for the supported deployment.

Not required for V1: swapping the embedding model without a code
change (#719 stays an open decision), sending mail, a web UI, hosted
clients, Linux support, every attachment format, saved monitors and
persisted claims.

## Current State

Live since the first deployment on 2026-10-03; the index was rebuilt
from Maildir on 2026-10-05, OCR off until #698 is decided. Three
containers run beside the Proton Mail Bridge app:

- **Proton Mail Bridge** — the official app on the host (the only
  mode since #716), IMAP on the host's loopback over implicit TLS,
  reached through `host.docker.internal`. Tested on macOS; Linux is
  not supported.
- **mbsync** — isync 1.5.1 on Debian trixie, pull-only into Maildir.
  No trust on first use: the certificate must match the
  operator-supplied `BRIDGE_CERT_FINGERPRINT` on every start, then a
  persistent pin.
- **indexer** — parses Maildir, threads, embeds through any
  OpenAI-compatible `/v1/embeddings` provider and writes SQLite
  (numbered migrations since the first deployment;
  4096-dim vectors; per-message records keyed by claimant ID). One
  durable `indexing_jobs` queue drives the initial scan, steady state
  and in-place reparses (#1078), fresh mail ahead of the backlog.
- **mcp-server** — five-lane hybrid search with RRF and optional
  Cohere rerank, exhaustive `query_messages`, and intelligence tools
  (`INFERENCE_MODE=none` by default). Streamable HTTP at `/mcp` only,
  behind a static bearer token, on localhost:3000.

Inference and embedding endpoints are operator-supplied; the project
ships no model-serving components. Host-side servers keep retrieval
traffic on the box; remote providers cross that boundary. Both
postures are documented, neither is provisioned.

## Roadmap

Phases run in order; a later phase starts only when the earlier one's
exit criterion holds, except where a decision moves a slice ahead.
Each phase has a milestone holding its open issues; item numbers are
stable because code and docs cite them.

### Phase 0 — Corpus integrity and security invariants

**Status: done (2026-09-27).** Follow-ups: milestone *Corpus and
contract follow-ups*.

**Exit criterion:** every source that exists in the synchronized
Maildir eventually reaches one visible terminal ingestion state, and
transient infrastructure failures never cause permanent source
omission.

Open follow-ups that matter most: bounded work on crafted attachments
(#1236), threading (#752 replies indexed before
their root stay split; #756 subject fallback, owner picks an option),
attachment formats without an extractor (#695, #923, #947, #691).

### Phase 1 — Truthful MCP contract

**Status: done (2026-10-02).**

**Exit criterion:** an unfamiliar LLM can query the corpus without
guessing about semantics, completeness, or identity.

Items: 1 `query_messages`; 2 structured MCP output; 3 message-first
retrieval; 4 honest `get_mailbox_status`; 5 MCP endpoint auth
(decision 13); 6 dead action/IMAP surface deleted; 7 source integrity
exposure; 8 doc-drift sweep. Evidence passages under filters are
labelled `in_scope` / `context` (decision 29).

### Phase 1.5 — Minimal regression baseline

**Status: done (2026-09-28).** `make baseline` and the CI job
`retrieval baseline`. Phase 2 must demonstrate behavior preservation
against it: refactor PRs show no snapshot diff; behaviour changes show
a reviewed snapshot diff with golden checks still passing.

### Phase 2 — Swappable embedding / vector generations

**Status: first slice only; deferred while the embedding model is
unchanged (decision 24).** Not required for V1.

**Exit criterion:** changing embedding models never requires altering
the source corpus or the versioned schema (no numbered migration and
no `SCHEMA_VERSION` bump). Everything derived is disposable and
regenerable: a context-compatible model switch regenerates vectors only (runtime `gNN` tables exempt from
`SCHEMA_VERSION`); an incompatible one regenerates chunks and vectors
through the staged rebuild below.

First milestone (#719): measure rebuild duration, resources and
acceptable downtime, then ship a validated staged rebuild with a
controlled cutover, recording dimension and tokenizer explicitly.
Live generations only if the maintenance window proves unacceptable.

1. **`vector_generations` registry** — provider, resolved endpoint,
   model, revision, dimensions, tokenizer, context window,
   chunk_config_hash, status (building / caught-up / active /
   retained / retired). Dimension read from metadata, never a
   constant. **First slice done** (#650): the
   embedder identity record and calibration vector, checked at startup
   by both services. Open: #719, #648, #661.
2. **Per-generation vec tables and the blue/green lifecycle** — only if
   #719's measurements call for it, and only after a reviewed design
   document, `docs/design/vector-generations.md`. It must cover:
   generation identity (resolved endpoint, model, dimension, revision,
   a non-secret configuration label, a calibration vector re-checked
   at startup and periodically, refusing reads and writes on drift); at
   most two live generations, both services holding both configuration
   sets (`EMBED_*`, `EMBED_NEXT_*`) and failing closed unless every
   live generation has a matching client; activation by registry
   change plus restart; reusing stored chunks only when the candidate
   tokenizer fits every stored input, by counting, with a supported
   query length; #283's evidence-recall eval as
   the activation gate; one generation per query (embed outside any
   transaction, re-read the ID in the snapshot, retry once, else fail
   closed); dual-writes enabled before the build watermark, backfill
   ordered behind concurrent mutations, and every chunk change,
   deletion and survivor mean applied to all live generations in one
   transaction. Two owner-approved exceptions (2026-09-30), recorded in
   AGENTS.md by the design PR: runtime `gNN` tables outside
   `SCHEMA_VERSION`, and a second embed Docker secret.
3. **Stage-aware pipeline manifest** — parser / normalizer / chunker /
   embedding identity and config hashes,
   `pipeline_config_hash = sha256(canonical_manifest)`, so a change is
   classified as re-embed, rechunk or reparse (#786).
4. **Chunk `kind` tags** (body / quote / signature / forwarded /
   calendar). **Stored** (#659); using them in retrieval is #660.
5. **Spike: retire hand-rolled address parsing behind the existing
   bounds** (#787). The initial `compat32` parse, the encoded-word
   scanner, the address pre-screen (`_split_address_list`), the
   attachment classifier and `_safe_decode` all stay. What is left:
   parse each element `_split_address_list` yields with
   `email.headerregistry` instead of `_parse_addrs`, possibly retiring
   `_format_address`, as a differential over every parser fixture and
   encoding-parity shape. A helper is retired only where the
   differential is equivalent and its bounds survive; if nothing
   retires without a new bound, close it and record why. A retired
   helper lands with a rebuild.

**Rebuild bundle.** Fixes that change chunk IDs, bodies or message
identity share one rebuild rather than one each, each still its own
reviewed PR, gated behind the pipeline configuration if it must land
before the rebuild. Open: #782.

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
   arguments, evidence recall, citation accuracy, pagination,
   unnecessary calls, and a judged answer eval (`make eval-answers`,
   three runs per side; `mcp-server/tests/eval/README.md`). **Partly
   done.** Next: written pass thresholds (#283, a V1 gate) and the
   re-baseline (#1174). Evals use the synthetic corpus only; questions
   from real mail wait on an owner decision (#785).
2. **Latency instrumentation before performance redesign.** Stage
   timings done. Open: benchmarks, request-level budgets and
   cancellation on real mail (#287). Measure before touching KNN
   architecture.
3. **Experimental ephemeral `brief_issue`** — chronology, actors,
   positions, decisions, open questions, conflicting evidence, every
   assertion cited, nothing persisted; behind
   `MCP_EXPERIMENTAL_TOOLS=true` (decision 12). **Done
   (experimental).** Accuracy and abstention scoring: #291.
4. **Adversarial injection suite** — hostile fixtures asserting the
   Phase 0 serialization holds under real tool flows. **Done.**
5. **Thread-vector weighting and rerank value** — attachment chunks
   dominate the thread-vector mean; reranking is unmeasured. **Not
   started** (#288, #289, #766).
6. **Prompt context budget with coverage disclosure.** **Done.**
   Retune from measurements: #487.
7. **Semantic recall under selective filters.** **Done** with
   candidate expansion; eligible-subset scoring is next if evals still
   show loss.
8. **A validated citation contract** — stable evidence IDs,
   claim→citation checks, quote verification, one repair, in every
   citing tool. **Partly done.** Open: semantic support (#284) and
   long answers left uncited after the repair (#819).

Retrieval gaps on the V1 list: #974, #987 (#858 done).

### Phase 4 — Deterministic knowledge scaffolding

**Status: mostly done; item 5 in progress.** Milestone *Phase 4*. Any
schema change needs a numbered migration.

1. **Entity resolution, phase 1 (deterministic):** address
   canonicalization, display-name clustering, domain→organization;
   never merged by display name alone, no model-suggested merges
   (decision 12). **Done.**
2. **Source authority metadata:** `source_type` / `authority_class`
   from an operator rules file only (decision 12), filterable, **never
   silently folded into ranking weights**; Spam never counts. **Done.**
   Open: verdict gating on Proton's authentication headers (#463,
   decision 19).
3. **Richer temporal retrieval:** `sent_at`, `occurred_at` (top
   `Received:`) and the effective time `COALESCE(occurred_at, sent_at)`
   for every filter, span and ordering (updated 2026-10-11: since #1080
   it is `COALESCE(occurred_at, sent_at, first_indexed_at)`; the date
   bounds of `query_messages` and `query_attachments` never read
   `first_indexed_at`, thread spans and `search_attachments` do,
   #1373) (`docs/architecture.md`,
   "Message time"). **Done** for the effective time; a missing `Date:`
   stays unknown (#1080). Per decision 35 the effective time stays the
   default; `date_basis` chooses `sent` or `occurred` for message and
   attachment listings (#1150). Keyset paging is retained; sent and delivery
   clock pages scan and sort under the current schema. Index optimization
   remains #1123.
   `internal` still waits on #1092 (item 5). Bitemporal claims
   wait for Phase 5.
4. **Deletion/retention semantics.** Mirror is the default, archive
   (`INDEXER_DELETION_ENABLED=false`) the opt-in, Trash indexed but
   hidden from default search. A reaped source reads as removed for 30
   days, then not found, and its text is never kept (decision 14).
   **Mostly done.** Open: user-controlled retention (#784), local
   deletion of deleted mail's files (#728), validating reaping on live
   mail (#783).
5. **Deterministic query language** (decision 35). One leaf compiler
   serves every filter implementation; leaves answer true, false or
   unknown, and `query_messages` counts unknown rows as
   `indeterminate`. **Done:** the compiler (#1084), `replied` and size
   (#1085), the sender-ambiguity flag (#1144) with sender and
   participant leaves answering unknown on it (#1153),
   `search_attachments` sender (#1056), stored display names with
   their completeness (#1140), and per-message completeness of body,
   subject, addresses and attachment manifest (#1086), whose address
   terms `query_attachments` shares, and the explicit address-mode and
   `body_words` leaves with the `where` form (#1088), per-occurrence
   attachment-text completeness (#1242), and the bounded `all` / `any`
   / `negate` form with a node cap that also counts `any` groups (empty
   groups rejected) and three-valued evaluation, fixed at two levels so
   there is no nesting to bound (#1087), and grouped aggregation as its
   own tool over the same engine (`aggregate_messages`, first slice of
   #823), subject_contains, header_exists / header_contains (#1089),
   and attachment_text_words (#1091). The header capability reports
   unassessed and incomplete messages (#1093 header slice). **Open, in order:**
   the rest of #823 (selectable clocks,
   further dimensions and identity counts); the
   index optimization for selected clocks (#1123; #1150 serves
   `date_basis` for both listings, retaining keyset paging with scan-and-sort
   under the current schema and counting missing clocks as indeterminate); leaves
   that wait on the evidence model (#1090 Bcc, #1092 internal date);
   a capability report in
   `get_mailbox_status` (#1093). Omitted by decision: IMAP UID,
   KEYWORD, DRAFT and DELETED.

### Phase 5 — Knowledge reasoning

**Status: not started, except item 2.** Milestone *Phase 5*. V1 needs
the answer-quality bar, not a particular mechanism.

1. Hardened `brief_issue`, informed by Phase 3 usage on real mail
   (#788).
2. Support / contradict / qualify / supersede analysis as a
   query-time tool. **Built (experimental)** as `check_conclusion`,
   with quote verification. Open: semantic support (#284).
3. Temporal position/change reasoning ("position as of date X" vs
   "current position") (#789).
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

Not a phase: the running deployment's resource, throughput, privacy,
observability and supply-chain work. Milestone *Operations and
hardening*. AGENTS.md requires degraded behaviour to be visible in
the logs and every MCP tool to declare safety annotations
(decision 31). Open decisions: the runtime base images (#835; until
then no image moves to Alpine or distroless), bounding review rounds
for test and eval-harness PRs (#838), stale extractor-version counts
in status (#979), telling a stall from a backlog in status (#876),
correlation IDs (#888) and OCR on the live deployment (#698).

### Evidence model

Not a phase: capture source semantics losslessly at the Maildir
boundary, so deterministic queries are grounded in preserved evidence
rather than reconstructed from it (decision 35). Milestone *Evidence
model*. The product boundary stays Bridge → mbsync → Maildir →
indexer; mbsync is not replaced (Deferred).

1. **Parser seam** — `parse_email_bytes(raw, source_metadata)` with
   Maildir as the adapter (#1077). **Done.**
2. **Reparse class** — an in-place full reparse through the job queue
   for changes that keep chunk IDs (#1078). **Done**, with fresh mail
   ahead of the backlog (#1142). Each schema change below is its own
   PR with its own migration and `SCHEMA_VERSION` bump; a new column
   reads as unknown, and the capability report (#1093) says "supported
   after backfill", until a reparse fills it. Changes that land close together may share one
   reparse run, never one migration.
3. **All headers** — a `message_headers` table keeping duplicates and
   order under one aggregate budget (fields, bytes, largest value in
   one guard); values never reach logs (#1079). Keyed by claimant ID,
   and added to the AGENTS.md per-message-row list by the same PR. The
   same migration stores whether the budget was hit, so a header
   predicate on a capped message is indeterminate, not false. **Done**
   (schema v13). Narrows
   #825 to normalization; prerequisite for #463 options 2 and 3.
4. **Unknown dates stay unknown** — nullable `sent_at` with a status;
   `effective_at` remains the ordering fallback, documented as not
   evidence (#1080). **Done** (schema v8); thread spans and
   `search_attachments` still read the fallback (#1373).
5. **Arrival time** — `CopyArrivalDate yes` is on in the mbsync
   template, with a layout test proving the mtime equals the server's
   INTERNALDATE; `internal_at` with an `unavailable` status for files
   that predate the option waits on verifying which date the Bridge
   app reports (#1081, #1138). Recovering it for
   existing mail would be an explicit cold re-pull.
6. **Content-hash identity** — whether messages without a usable
   Message-ID are indexed instead of dead-lettered (#1082, decision).
   Adopting it changes the AGENTS.md claimant-ID and thread-membership
   constraints, so the decision must define the synthetic message and
   thread identity (such a message never becomes a thread other
   messages resolve to by ID, and two of them never merge by an empty
   ID) and update AGENTS.md in the same PR. Until then the claimant-ID
   invariant stands unchanged.
7. **Occurrence model** — each Maildir copy of a message becomes its
   own occurrence (decision 47; #1566, #1567, #1568, #1569). Until
   then one `messages` row holds one folder, path and flag state, so
   byte-identical files claiming one Message-ID share a claimant ID and
   one row; removing one such copy no longer reaps a message whose
   other copy survives (#1102). The `,U=` in a Maildir file name is
   never read; `.mbsyncstate` is never parsed.

## Not doing (decided 2026-09-26)

Recorded so items are auditable rather than silently dropped. Each
can be revisited with an explicit owner decision.

- **Mail-changing action tools** (`send_email`, `move_message`,
  `mark_read`, `flag_message`, `create_draft`, `reply_to_thread`).
  Read-only is part of the trust model, not a temporary deficiency:
  "it can understand your history, but it cannot send or delete
  anything."
- **Live-IMAP retrieval fallback for mcp-server.** A stale index
  answers "the index is not current" — it never silently switches
  data sources.
- **Per-session inference-mode toggle.** Per-deployment is enough.
- **Guarded live Bridge integration CI.** Requires a dedicated paid
  Proton test account and hardens the ingestion edge rather than the
  product.
- **Investment in Bridge operations.** Bridge gets maintenance, not
  investment; it is the vendor's app on the host. The stable contract
  is Bridge → mbsync → **Maildir** (product boundary) → indexer, which
  leaves room for other mail connectors later.
- **Attachment export or download** (decided 2026-10-08, decision 39
  reversed). Original attachment bytes are fetched from the Proton
  inbox; the tools serve extracted text only (`query_attachments`,
  `get_attachment`), with no operator export command and no MCP
  download.
- **mypy → pyright migration** — wait for a real trigger.

## Deferred (not dead — revisit on a real trigger)

- audio/video transcription via Whisper (trigger: a clear use case)
- near-duplicate handling; saved queries / persistent monitors;
  waiting-on-me / unanswered-thread views (trigger: knowledge
  scaffolding in daily use)
- `content_blocks` as a persisted table (trigger: a non-chunking
  consumer needs it)
- parser/chunk generation coexistence machinery (the pipeline
  manifest preserves stage identity meanwhile)
- replacing the stdlib MIME parser (decided 2026-09-30): Python's
  `email` package never exposes raw part offsets; the one real
  candidate is Stalwart's Rust `mail-parser` via pyo3, an architecture
  change with a full reindex. Trigger: hostile-mail robustness becomes
  a goal in its own right
- hosted or remote MCP clients: need the design in decision 13 and a
  privacy-posture update in AGENTS.md first. Trigger: the owner wants
  such a client
- a cheaper chunking ceiling (a 12 MB body still takes 10–29 s).
  Trigger: a real stall on live mail
- newest-first initial indexing as an option. Trigger: #752 lands
- reporting child folders skipped under isync's reserved names.
  Trigger: a real report
- replacing mbsync with a read-only IMAP acquisition layer (decision
  35): Maildir already carries folder, flags and (with
  `CopyArrivalDate`, for files synced since it was enabled, once
  Bridge's reported date is verified, #1138) the arrival time; a
  Python IMAP client would re-open the TLS and fingerprint path, add a
  container and an untrusted-input surface, and reverse the Maildir
  boundary. Bridge `UID SEARCH` is a hand-run differential target for
  simple predicates, never an oracle. Trigger: an IDLE-latency
  requirement, or a sync defect isync cannot handle

## Operational baseline

- Python services use per-service `uv` projects with pinned
  `pyproject.toml` + `uv.lock`, each with a 90% coverage floor in CI.
- Bridge is the official app on the host, which updates itself; mbsync
  requires its certificate fingerprint on every start.
- All long-running services run as non-root with `cap_drop: ["ALL"]`,
  `no-new-privileges`, read-only root filesystems, `pids_limit`,
  `init: true`, and Docker log rotation.
- Bridge password lives in `.secrets/bridge_pass.txt` (Docker
  Compose secret), never `.env`.
- Deletion reconciliation (mirror) is on by default with a grace
  window, mass-delete brake, and atomic reap-or-rollback.
- A durable `indexing_jobs` queue retries transient failures with
  exponential backoff and dead-letters persistent ones for operator
  visibility.

## Known limitations

- initial sync may take a long time on large mailboxes
- attachment OCR assumes English (documented, #490); OCR is off on
  the live deployment until #698
- Linux hosts are not supported out of the box: `host.docker.internal`
  reaches the docker bridge address, not the host loopback the Bridge
  app binds, and the services cannot read their mode-600
  `.secrets/*.txt`; Windows Docker Desktop is untested
- the schema is locked to 4096-dim Qwen3-Embedding-8B-shaped models
  (hardcoded dim, vendored tokenizer), so switching models or moving
  the embed endpoint needs a rebuild (#719); an embedder outage at
  startup stops the server (#661)
- audio/video and calendar attachments are not extracted (#695)
- draft/forwarded state is not indexed
- the indexer never deletes Maildir files, so a reaped message's
  `.eml` stays on disk (#728); mirror reaping is not yet validated
  under long-running real-world conditions (#783)
- reaped text leaves the index file on the next completed maintenance
  merge, not at a fixed time, and can outlive it in filesystem free
  blocks, snapshots and backups (`docs/architecture.md`)
- keyword search does not match precomposed letters with two
  diacritics or composed vs decomposed Hangul across forms (#782)
- a child folder named after one of isync's own files is not synced
  and nothing reports it (`docs/architecture.md`)
- chunking a very large crafted body still takes tens of seconds
  (Deferred)
- tool descriptions ask an agent to tell the user before bulk or
  out-of-scope reads but cannot enforce it; a filtered read can still
  return passages from messages outside the filter, labelled
  `context` (decision 29), which the model is told, but cannot be
  forced, to use only as context

## Blockers and Risks

- **Initial Proton sync duration.** Large mailboxes may take hours
  before useful indexing begins; do not assume indexing bugs until
  Bridge's internal sync has completed.
- **Bridge TLS and cert behavior.** The app's certificate is issued
  for `127.0.0.1` only; mbsync checks it against that name through a
  tunnel and requires `BRIDGE_CERT_FINGERPRINT`. Do not modify this
  casually.
- **Schema sensitivity.** Changes to the schema, embedding dimensions
  or thread model can invalidate stored data; Phase 2 exists to
  confine embedding-model changes to disposable vector generations.
- **Knowledge-layer poisoning.** Any future persisted derived knowledge
  inherits prompt-injection risk from attacker-controlled email; the
  Phase 5 rules are the control.

## Resolved decisions

Numbers are stable; code, tests and docs cite them. Obsolete and
superseded entries keep their number and one line.

1. **Read-only MCP server (2026-09-26):** no action tools, SMTP path or
   draft/move/flag operations; the unregistered action/IMAP code was
   deleted rather than kept as hypothetical capability.
2. **Live Bridge integration lane (2026-09-26):** not doing (see Not
   doing).
3. **#242 first-run retry (2026-09-30):** obsolete since #716.
4. **#245 Bridge auto-update in existing vaults (2026-09-30):**
   obsolete since #716.
5. **#217 Message-ID conflicts (2026-09-30):** keep every claimant and
   expose conflicts; never pick a winner by arrival order, which is
   spoofable. Landed as the claimant ID.
6. **#292 mixed digital/scanned PDFs (2026-09-30):** page-level OCR
   selection with a `pdf` extractor version bump.
7. **#295 sequential inline text parts (2026-09-30):** superseded;
   fixed directly (#444).
8. **#297 undated mail (2026-09-30):** superseded by 14 and, once
   #1080 lands, by 35: a missing or unparseable `Date:` is stored as
   unknown, and the first indexing time survives only as
   `first_indexed_at`, a non-evidentiary ordering fallback.
9. **#276 retained near-side mbsync state (2026-09-30):** warn about a
   far-side box that cannot be opened and keep syncing the rest, never
   `Remove Near`, which deletes local mail.
10. **#277/#282 first-sync health (2026-09-30):** separate liveness
    from freshness, so a first sync is "healthy, not yet current", and
    set the stall deadline above the observed backfill
    (`SYNC_DEADLINE_SECONDS`, tuned per mailbox).
11. **#268 Bridge smoke-test scope (2026-09-30):** obsolete since #716.
12. **Result quality and Phases 3–5 (2026-10-01):**
    - until the first deployment, schema changes fold into v0 with no
      migration (ended 2026-10-03);
    - retention ships as **mirror**; archive is the opt-in;
    - `brief_issue` and Phase 5's support/contradict analysis are
      **experimental tools**, off unless `MCP_EXPERIMENTAL_TOOLS=true`;
    - #217's claimant ID is the Message-ID plus a short content-hash
      suffix;
    - source authority comes only from an operator rules file: a model
      classifier would send mail to the inference provider;
    - entity resolution is deterministic only.
13. **Open decisions of 2026-10-02:**
    - **MCP endpoint auth:** a local static bearer token from a Docker
      secret, compared in constant time, failing closed when empty, so
      `/mcp` answers 401 before any session and `/health` stays open
      (design in AGENTS.md, `mcp-server/`). Trust condition:
      "processes running as the operator are trusted". Not chosen, each
      needing its own owner decision and an AGENTS.md privacy update
      because mail would leave the host: a private-network gateway and
      hosted clients (an OAuth 2.1 resource server). Either must add the
      external Host/Origin narrowly to the transport-security allowlist;
      the hosted one must also set the protected-resource URL
      (`AuthSettings.resource_server_url` / fastmcp `RemoteAuthProvider`).
    - **#432:** a streaming pre-pass counting raw xlsx `<c>` nodes, with
      an `xlsx` version bump.
    - **#498:** Streamable HTTP at `/mcp` is the only transport.
    - **#497:** the Bridge app on the host through a certificate-valid
      path, never a TLS bypass.
    - **#537:** `get_evidence` gains `max_threads`.
    - **#494:** keep strict argument matching and first-call tool
      selection; the live-trace recorder comes after go-live (#283).
    - **#496:** keep the 3-characters-per-token estimate.
    - **#495:** keep the CJK terminators and word counts.
    - **Compose and shell scanning:** build it.
    - **#287 request cancellation:** designed only after real-mail
      measurements.
14. **Evening walkthrough of 2026-10-02:**
    - **`occurred_at`** is the top `Received:` header's date, NULL when
      absent or unparseable, never `Date:`; date filters and thread
      spans use `COALESCE(occurred_at, sent_at)` (updated 2026-10-11:
      since #1080, migration 0008, the effective time is
      `COALESCE(occurred_at, sent_at, first_indexed_at)`; the last
      orders an undated message, places it in its thread's span and
      is read by `search_attachments`' bound, #1373, while
      `query_messages` and `query_attachments` count it as
      indeterminate).
    - **Date-range evidence:** any passage of a thread whose span
      overlaps the range, each with its own dates; passage dates are
      read from `messages`.
    - **#562:** purge unreferenced `attachment_extractions` rows in the
      reap transaction.
    - **#580:** check hardening on the merged config of every overlay
      combination.
    - **MCP clients:** Claude Code, Codex, Claude Desktop (a repo-owned
      `fastmcp` stdio adapter instead of `mcp-remote`) and ChatGPT
      desktop, whose connectors need the hosted-client design
      (Deferred).
    - **Reaped-citation invariant:** a 30-day, content-free record (a
      permanent one would list deleted mail's domains); a reaped source
      reads as removed for 30 days, then not found, and its text is
      never kept.
    - **#428:** cap every xlsx part openpyxl loads whole, with a version
      bump.
    - **#275/#281:** collision-free folder mapping; **#279:** a tested
      UIDVALIDITY recovery procedure.
    - **#489:** `get_message` pages the body by offset, headers capped.
    - **Message-ID length:** 998 characters; longer takes the
      no-`Message-ID` dead-letter path.
    - **`BRIDGE_USER`** stays in `.env`: an identifier, not a credential.
    - **P3 policy:** small P3s with an agreed fix and no new mechanism
      may be fixed before go-live (AGENTS.md). (2026-10-09: superseded
      by priority order, P0 first, then P1/P2, then P3s in turn; small
      P3s may still be fixed ahead of P1/P2 work. AGENTS.md.)
15. **#638 implicit TLS (2026-10-02 night):** mbsync ⇄ Bridge IMAP uses
    implicit TLS, with no STARTTLS or plaintext fallback; an approved,
    owner-gated TLS change.
16. **v0 additions before go-live (2026-10-02 night):** Maildir
    read/flagged/replied state, chunk `kind` tags and the embedder
    identity record went into v0; the claimant suffix grew to 16 hex
    digits.
17. **Result quality first (2026-10-01):** work targets returned-result
    quality ahead of edge cases and P3s. (2026-10-09: run-scoped focus;
    under AGENTS.md "Accuracy first", an edge case inside an issue's
    scope is fixed, and only unrequested ones are filed.)
18. **Bridge container removed (2026-10-04, #716):** the official app
    on the host is the only setup; macOS tested, Windows untested,
    Linux unsupported; `BRIDGE_CERT_FINGERPRINT` is always required.
19. **#463 sender authentication (2026-10-04):** a DMARC `fail` in
    Proton's own `Authentication-Results` leaves the sender
    unclassified, and a `sender_authenticated` flag (pass / none / fail
    / internal) is shown with the authority class. A single
    `X-Pm-Origin` with the exact value `internal` counts as
    authenticated. The backfill is an application step after the
    migration, not SQL.
20. **`INDEXER_UNLINK_ON_REAP` removed (2026-10-04, #721):** the
    indexer never deletes Maildir files; local deletion belongs to
    mbsync (#728).
21. **Smaller rulings of 2026-10-04:** Proton's virtual `Starred` is
    left out of the sync; the best thread keyword hit is guaranteed a
    top-three place in hybrid RRF; the initial scan indexes **oldest
    first** across folders, with no setting, since newest first split
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
    the owner's consent for that run (AGENTS.md, "Do not export mail in
    bulk").
27. **Default inference model (2026-10-05, #764, #805):** the default
    `INFERENCE_MODEL` is `claude-sonnet-5-5`. Unset token settings take
    16000 / 48000 (reply / window) in anthropic mode, where thinking
    counts against the reply limit, and keep 1024 / 32768 otherwise, so
    a 32k local model still fits.
28. **Structured outputs for the JSON tools (2026-10-05, #808, #809):**
    in anthropic mode `extract_from_emails`, `brief_issue` and
    `check_conclusion` send their reply schema as an Anthropic
    structured-output format, behind `INFERENCE_STRUCTURED_OUTPUT`
    (default on). A provider rejection is an error naming the setting,
    with no retry without the format. The extraction schema carries
    neutral keys (`f1`, `f2`, …), never a caller's field name, because
    Anthropic caches a schema for up to 24 hours. Schemas over
    Anthropic's measured limits are sent as plain JSON. openai mode is
    #807.
29. **Evidence scope under filters (2026-10-05, #755):** retrieval keeps
    selecting whole threads and supplying their context, and each
    evidence passage is labelled `in_scope` (its own message satisfies
    every message-level filter) or `context`. The `ask_mailbox` prompt
    answers from in-scope passages, names the active filters, and uses
    context only to interpret them; the citation check flags a filtered
    answer that cites no in-scope passage. Labels come from indexed
    message metadata at query time. Restricting evidence was rejected
    (it drops the corrections an answer depends on), as was keeping it
    unlabelled.
30. **mbsync on Debian trixie (2026-10-06, #833):** isync 1.5.1,
    OpenSSL 3.5, `TLSType IMAPS`. isync 1.5 decodes Bridge's modified
    UTF-7 folder names to UTF-8 directory names; an existing install
    with encoded folders migrates by renaming them (never deleting) and
    rebuilding the index (`docs/setup.md`).
31. **MCP tool safety annotations (2026-10-06, #899, #900):** every
    tool declares `readOnlyHint: true`, `destructiveHint: false`,
    `openWorldHint: false` and its own title, from one shared constant;
    a test fails on a tool added without a deliberate classification.
    Operational logging does not prevent read-only. `openWorldHint`
    stays `false` for tools that call a provider: the domain is the
    mailbox, and egress is disclosed by the startup privacy warnings and
    `make status`. Accepted risk: a client that auto-approves read-only
    tools can send excerpts to a remote inference or rerank provider
    without a prompt.
32. **Attachment formats (2026-10-07, #694, #935, #936, #937, #957):**
    `.pptx` through python-pptx and `.dotx` through python-docx's own
    part registry, with pre-open package budgets. `.doc` through
    `catdoc`; `.xls` through xlrd in a child process with its own
    memory and CPU limits, because python-calamine allocates every
    sheet's full grid at open and xlrd's shared-string loop is unbounded
    in-process; `.ppt` through Apache POI on a trimmed Java runtime,
    because `catppt` reads no slide text from current decks. Every
    external parser runs through one runner (temp file, no shell,
    timeout, output cap, and address-space and CPU limits set before
    the parser loads; the OCR path has a timeout only, #1021; updated
    2026-10-11: image OCR, #1292, and PDF parse, render and OCR, #1293
    and PR #1453, now run in the limited extractor child, closing
    #1021). Accepted
    risk: catdoc's unfixed Debian CVEs (none at HIGH/CRITICAL).
33. **Per-extractor extraction cache (2026-10-07, #928):** the
    attachment extraction cache is keyed by content hash and extractor
    module, so one label's result is never served to an occurrence that
    selects another extractor. The first change after the first
    deployment to take a `SCHEMA_VERSION` bump and a numbered migration.
    Snapshot the index before deploying a schema change (#1005).
34. **Review and PR rules (2026-10-07, #944, #981):** every real gap in
    a PR's "Not done" gets its own issue before the PR is called ready;
    a gap in code, tests or docs that a PR adds counts as introduced by
    it under the #751 exception.
35. **Deterministic query language and the evidence model
    (2026-10-07):** keep Maildir as the product boundary and isync as
    the sync layer; do not replace mbsync (Deferred). Preserve source
    semantics the indexer already receives but discards (all headers,
    unknown dates, arrival time; content-hash identity is a separate
    open decision, #1082). Build the deterministic layer as one
    predicate compiler with a bounded Boolean form, explicit
    `date_basis` (`effective` stays the default) and three-valued
    results with an `indeterminate` count, established before negation
    (Phase 4 item 5). Define `BODY`/`TEXT` equivalents by what the index
    holds (`body_words`, `attachment_text_words`), never by Gluon's
    behaviour. Omit IMAP UID, KEYWORD, DRAFT and DELETED. The
    assessment behind this is in the issues' bodies (#1077–#1093).
36. **Predicate leaves, reparse and sender evidence (2026-10-08):**
    parser-only backfills run as an in-place reparse through the job
    queue, queued by the migration that needs it (#1078), with fresh
    mail interleaved ahead of the backlog (#1142). Explicit leaves
    take a typed `where: {all: [...]}` form whose #1087 shape is fixed
    up front; the flat parameters keep their meaning forever (#1088).
    Each `where` leaf reports true / false / indeterminate counts of
    its own value over the messages the whole expression does not
    reject (matches plus indeterminate), and its matched addresses as
    the addresses that leaf's own SQL selects on the messages the
    expression returns; under OR and NOT (#1087) the same rules hold,
    and a negated leaf reports counts only.
    Bcc counts as a recipient and answers "can't tell" on received
    mail (#1090). Every display name is stored (#1140). Repeated To and
    Cc headers merge; a repeated From marks the sender ambiguous and
    the message loses source authority (#1144, #463). A leaf never
    answers a confident "no" on incomplete or not-yet-reparsed data.
    The choices behind each are in the issues' decision comments.
37. **Review-round reduction (2026-10-08, #1162):** a fix that adds a
    state or moves a validation boundary is a stop-and-ask design
    change at any round; reviewers report a defect's class and all its
    siblings in one round; procedure-style docs keep to verified
    claims. Measured over the next ten PRs (#1163).
38. **V1 finish line (2026-10-08):** the outcomes and release gates in
    "V1 Finish Line" define done. They adopt an external assessment
    (Codex, static read of `17b9904e`) with four changes: the bounded-work
    P1s come first, #1086 leads the predicate work, reasoning is gated
    by measured answer quality rather than features, and embedder swap
    without code change (#719) is optional. A deployment gate was added.
39. **Attachment export (2026-10-08, #1217):** original attachment
    bytes are delivered by an operator command, not through MCP: a
    one-off, no-network container that reads the Maildir read-only,
    verifies each file and payload against its stored hashes, and
    writes only to a destination the owner approves for that run.
    Only leaf attachments are exported: an attached email or other
    MIME container is refused, because its stored hash covers a
    re-serialised form, not the bytes as sent.
    `search_attachments` returns each hit's `attachment_occurrence_id`
    to select from. The MCP server stays read-only with no Maildir
    access; a download route or resource needs its own decision.
    **Reversed by the owner the same day:** no export command is built.
    The owner retrieves original attachments from the Proton inbox, and
    the system serves extracted text only (see Not doing).

40. **Attachments, completeness and evidence ranking (2026-10-08):**
    DOCX, PPTX and XLSX extraction runs in a child process with memory,
    CPU and wall-clock limits (#1040); per-message extraction launches
    stay unbounded as an accepted P2 risk until a scheduling design
    lands (#1236; updated 2026-10-11: PR #1355 added the per-message
    extraction budget, and #1236 stays open for child confinement,
    #1354). Attachments are enumerated exhaustively by
    `query_attachments` and their stored text read by `get_attachment`, whose
    streaming reader reports an exact `total_chars` on every page;
    `date_basis` for both listing tools selects effective, sent or
    delivery time and reports missing selected clocks as indeterminate
    (#1150, #796). Keyset paging is retained with scan-and-sort for
    sent and delivery clocks under the current schema; index optimization is #1123.
    Per-message completeness flags keep a leaf from answering a
    confident "no" on incomplete data: a stored match still answers
    yes, and finding nothing answers "can't tell" unless the relevant
    flag says the data is complete, `authority_class` included (#1086). The keyword-matched evidence
    passage is ranked by in-thread word rarity over a candidate-only
    in-memory index, never a corpus-wide scan (#858, #1246). The
    choices behind each are in the issues' decision comments.

41. **Message-ID grouping (2026-10-08, #991):** `query_messages`
    gets an opt-in mode that returns one row per Message-ID; the
    default stays one row per claimant, and every claimant remains
    reachable. A group matches when one of its claimants matches the
    whole filter on its own, and is represented by a matching copy.
    Grouping is by a sender-controlled identifier, never proof of
    identical content. `search_emails` and `get_evidence` do not
    group. A `distinct_message_ids` count ships first. The full
    contract is in #991's decision comment.
42. **Extraction isolation (2026-10-08, #1290):** every attachment
    extractor, format preflight included, runs as a disposable,
    resource-limited child through the hardened `run_tool`
    (process-tree kill, parent-owned scratch, limits sized for every
    process the child's tree runs at once, output caps derived from
    the configured input budget times the decode's worst expansion).
    Only `text` is exempt: a fixed codec set and constant-pass decoding
    with no structural parsing, to be locked by a CI test (#1295;
    updated 2026-10-11: not written yet, #1295 is open). Not yet met
    (2026-10-11): the dispatcher's OOXML ZIP-size guard
    (`_validate_zip_payload`) still reads the central directory in the
    indexer process until #1438 moves it. Order:
    #1230, the runner (#1291), `image` (#1292), `pdf` (#1293), `html`
    with body HTML conversion (#1294), then #922; extracted text stays
    byte-identical. The stdlib message parse stays in-process for now:
    its read cap precedes parsing, structural caps follow it, and
    worst-case containment is not established. Its `RecursionError`
    becomes a terminal dead letter (#1296); whole-message isolation is
    its own decision if a measured shape shows the parse unbounded.
    Concurrency (#698) is bounded disposable launches; a persistent
    worker needs its own decision proving containment equal to a fresh
    process. Process separation is not filesystem or network
    confinement, which stays #698's requirement (#1236).

43. **Defence in depth (2026-10-08):** each trust boundary (untrusted
    mail and attachments, secrets, network exposure, Bridge TLS) keeps
    every control it has: none is removed, relaxed, replaced or made
    conditional because another covers the same threat without an owner
    decision. It sets no minimum number of controls, since some
    boundaries have one by nature (pull-only sync is one setting;
    keeping mail out of logs is coding discipline plus marker tests),
    and an additional layer is a design decision, never a review finding
    against a PR that does not touch the boundary. The rule applies to
    every control enforcing a boundary, listed or not; the reference
    table of the controls, shared assumptions and accepted limits is
    `docs/architecture.md` "Trust Boundaries", documentation, not a test.
44. **Retrieval, not case management (2026-10-08):** the MCP server
    finds mail, lists it completely, and returns what is stored, with
    honest counts and completeness. Grouping mail into matters, linking
    threads by a shared identifier (for example a case or order number),
    tracking requests and assigning statuses are the client's job. The
    client can search for an identifier with `search_emails` or
    `query_messages` `text=` and group the results itself. Neither search
    is exhaustive for an identifier that appears only in subjects or
    attachments, and neither verifies that the literal identifier occurs
    in each result, so the precision of any grouping is the client's
    responsibility. Ruled out so far: request reconciliation and
    lifecycle statuses, and identifier extraction with cross-thread
    linking.
45. **IMAP ingestion boundary (2026-10-10, #1346):** provider policy
    owns connection and trust requirements, mailbox selection, roles,
    source categories and the assessment of any optional provider
    identity or provenance. The transport owns validated configuration
    rendering, sync, Maildir layout and state, recovery and content-safe
    diagnostics; mbsync and Maildir stay the first transport, behind the
    bytes-plus-source-metadata seam (#1077). The core owns exact
    evidence, threading, indexing, retrieval and citations, and never
    requires a provider identity. Durable account scope and
    connection or address coverage are distinct; sources, mailbox
    occurrences and scoped collection observations are distinct. Unknown
    metadata stays unknown, a failed or incomplete scan cannot establish
    absence, and equal bytes do not establish one logical message.
    Retained companion metadata restores dated label observations; a
    Maildir-only rebuild needs a refresh (owner, 2026-10-11, #1313). The
    Proton path is extracted unchanged: rendered configuration,
    environment variables, channel and store names, public interfaces,
    every control and the pin-rotation semantics, except that pin
    rotation and forced deletion become one-run commands instead of
    persistent settings, with the pin and fingerprint checks unchanged
    (owner, 2026-10-12, #1353, #1609). New configuration or
    state formats, collectors, role behaviour, reconciliation, renamed
    channels or stores, any path around a Proton control (a synthetic
    source included) and additional sources each need their own
    decision; open parts stay in #1083, #1313, #1316 and #1317.
46. **Proton provider settings (2026-10-11, #1497):** `SOURCE_MODE`
    (only `proton-bridge`, the default) selects the provider and is read
    once at startup; no control is conditional on it. Every Proton value
    is an operator setting that defaults to today's value and is
    validated by one shared helper at `scripts/validate-env.sh` and the
    mbsync entrypoint before rendering or any network use; a forbidden
    value is refused with fixed text naming the decision that would
    allow it, never the value. Certificate pin rotation is the
    exception: it becomes a one-run command, not a setting (owner,
    2026-10-12, #1353, #1609). The Bridge stays on this Mac: the host
    is an allowlist of container aliases tested to forward to the
    host's `127.0.0.1`, the certificate name a loopback identity; off-Mac
    Bridge is #1499. #1313 enables metadata-only labels; the payload
    exclusions of `Labels/*`, `All Mail` and top-level `Starred` remain
    required (owner, 2026-10-11).
    Channel and store names follow log redaction (#1498). Replacing the
    Compose literals with this validation is an owner decision under
    decision 43, which stands unchanged.
47. **Mailbox occurrences (2026-10-11, #1083; amended 2026-10-12 and
    2026-10-13):** each Maildir copy of a message is its own
    occurrence, keyed by account, account-relative folder, Maildir uniq
    and claimant ID, bound only after its full content hash is
    verified. The account is a row of an append-only `accounts`
    registry holding its scope UUID and provider (#1596);
    `ACCOUNT_SCOPE` in `.env`, generated once by setup, is the
    single-account form. The indexer and mcp-server each require
    exactly one stored account matching the configured UUID and
    provider and otherwise fail closed; recovery is restore or rebuild.
    Occurrences own folder, an account-root-relative path unique among
    live rows, flags, observation dates, lifecycle, provenance and
    arrival time; `messages` keeps content, headers and source dates.
    Relocating the account root alone keeps occurrences. A flag rename
    keeps an occurrence; a folder, uniq or content change replaces it.
    Pending deletions are keyed by occurrence. A message is reaped only
    after losing its last active occurrence that is not `T`-flagged in
    any account, under the existing grace, brakes and retention, with a
    per-account brake beside the unchanged global one; a partial scan
    establishes no absence. Filters match within one occurrence,
    results are deduplicated before counting and paging, source counts
    stay the default, and Trash and source categories derive from the
    folder at read time under the provider of the occurrence's account.
    A message is in the default scope when it has an active occurrence
    outside its provider's Trash role, considering only its non-`T`
    active occurrences when any exist, otherwise all its active ones
    (owner, 2026-10-13, #1568). Schema v14 (#1596, #1566) is additive
    and changes no reader; v15 (#1567, #1568, #1569) moves the readers
    and drops the old columns. The v14 migration binds the account row,
    seeds one occurrence per message whose `indexed_files` record for
    its stored path carries the same content hash (stored records only;
    the file is not re-read) and queues a reparse without re-embedding.
    Claimant, chunk and thread IDs are unchanged; account selectors and
    other multi-account behaviour are decision 49. The near-side UID
    and `.mbsyncstate` stay unread.

48. **Exhaustive thread queries (2026-10-12, #1166):** `query_threads`
    shares the public message filters and evaluates the whole expression on
    each claimant before grouping. It lists definite matching threads in
    thread-ID keyset order, counts unknown messages separately from
    unknown-only threads, and retains whole-thread context with nullable
    participant counts and three-valued attachment presence. Each page is
    one fresh read snapshot; frozen cross-page populations are not claimed.
    No schema change or selectable date basis is introduced.
49. **Several accounts in one deployment (2026-10-12, #1491):** the
    goal is several accounts of different providers feeding one index
    and MCP endpoint; decision 50's per-account configuration comes
    before any multi-account code, with no interim list format.
    Threading stays global: membership resolves by Message-ID, and
    claimant, chunk and thread IDs and thread-level indexing are
    unchanged. An account filter constrains which messages qualify at
    occurrence level; thread context may include members held only by
    another account, each labelled with its account. Account-local
    conversations were rejected. The legacy account keeps `/maildir`;
    added accounts mount at `/accounts/<UUID>`. Each account has its
    own collector container with only its own non-empty mode-600
    password secret, Maildir and state volumes, resource limits and
    healthcheck; the indexer and mcp-server get no password, and the
    indexer drops its health dependency on a single collector. Sync
    stamp, watcher, deletion brake and collection status are per
    account, with bounded round-robin scheduling. Search defaults to
    the union of accounts, with account UUID-set filters, account
    provenance on results, folders grouped by account, cursors bound to
    the resolved account set, and source, occurrence and logical counts
    kept distinct. Disabling an account keeps its data; explicit
    removal retires that account's occurrences and labels, keeps
    sources other accounts hold and keeps last-copy safeguards;
    registry rows stay. Rotating a password recreates one collector.
    Owner identities are per account, authority rules global. Generic
    IMAP uses implicit TLS with system CA trust and hostname
    verification and no certificate pinning (isync trusts a non-CA
    `CertificateFile` certificate whatever its name, so a leaf pin
    would drop the hostname check), password or app-password
    authentication only, and explicit folder roles, an unknown role
    staying unknown. Generic collectors use their own egress network,
    separate from `bridge-net`, under a provider-aware Semgrep rule,
    and never receive the Bridge password. Proton's controls are
    unchanged; #1499 stays separate.
50. **Operator configuration (2026-10-12, #1353):** a TOML file with
    schema version 1 is the operator interface. A host-side translator
    in Python 3.11+ (`tomllib`; host Python becomes a prerequisite)
    produces today's runtime projections and
    `.generated/compose.accounts.yml`, one hardened collector per
    enabled account with only its own secret, volumes and provider
    network; legacy service and volume names, `/maildir` and global
    threads stay. `[global]` holds the model layers and their secret
    references, MCP, indexer policy and authority rules. Each
    `[[accounts]]` entry holds an immutable `scope_uuid`, an editable
    `name`, `enabled`, `provider`, `maildir_root`, `password_file`,
    connection and sync settings, mailbox selection, identity addresses
    and exactly one provider table (`proton_bridge` or `imap`). At most
    one account uses `/maildir`, every other one
    `/accounts/<its scope_uuid>`. Proton accounts accept mailbox
    exclusions only, and `mbsync/proton-settings.sh` still enforces the
    mandatory ones; `imap` is defined but refused until #1606.
    `config/mode` (mode 600, gitignored) holds exactly `legacy` or
    `toml` and is written only by an explicit command (`make
    migrate-config` writes `toml`, a fresh `make init-config`
    `legacy`); an existing deployment without it, or a mixed
    configuration, is refused with fixed text, with no fallback in
    either direction. In TOML mode Compose runs with
    `--env-file .generated/compose.env`, `.env` is not read and
    inherited application variables are stripped. Migration imports
    the effective `ACCOUNT_SCOPE` as the one account at `/maildir`,
    checks it against the `accounts` row and never regenerates it. The
    host rejects unknown keys, wrong types and ranges and mixed account
    forms with fixed text; `scripts/validate-env.sh` independently
    checks every account projection with value-free diagnostics, and
    each service still validates what it is given. Configuration files
    are mode 600, generated directories 700, and all are gitignored.
    Generated Compose is scanned by Semgrep and `compose_test.sh`,
    whose hardening checks allow each declared collector only its own
    secret, volume and network. Every application setting is mapped
    before the translator ships. Maintenance overrides
    (certificate pin rotation, forced deletion) become one-run
    commands, not configuration (#1609). One enabled account is allowed
    until #1605.
51. **Collection status (2026-10-12, #1592, #1593):** mbsync publishes
    a bounded format-1 JSON status file in the Maildir root, atomically
    replaced before and after every attempt; the success stamp and its
    acknowledgement are unchanged, and reading the status file never
    acknowledges delivery. The file carries the account's scope UUID,
    the current attempt, the last terminal outcome (`succeeded`,
    `succeeded_with_withheld_folders`, or `failed` with a fixed failure
    class, `interrupted` included) with a nullable withheld count, and
    the last success, ordered by publication, never by timestamps;
    folder names never appear. A far-side failure combined with any
    other error is `failed`, and a leftover `in_progress` proves
    nothing about whether the collector is alive. After a failure,
    `current` stays false until a later success has been delivered (its
    attempt is the one the acknowledged success stamp names); a success
    that withheld folders keeps today's criteria and shows the count;
    upstream Bridge/Proton currency stays the fixed "unknown". The
    indexer stores the outcome per account in `account_collection_state`
    at schema v16, after decision 47's v15. A missing, legacy,
    malformed, unsupported or mismatched file reads as unknown with a
    fixed-text WARNING, never as a success, and a rebuilt index
    recovers the state from the file. `get_mailbox_status` gains a
    `collection` block. The declared Bridge address scope is stored per
    account; a changed declaration is reported with when it changed and
    never refused (#1593, after #1592).
52. **Provider identities and label observations (2026-10-12,
    #1598):** a provider-identity candidate is derived from stored
    header evidence, only from a complete header capture with exactly
    one candidate field, so the migration queues no reparse;
    reassessment runs in bounded, resumable batches keyed by policy
    version and occurrence provenance. #1598 owns the acceptance and
    binding rule, and until the owner approves one nothing is accepted:
    candidates, versioned assessments with reasons and dated membership
    observations are stored with no authoritative association or
    logical deduplication, so label features show nothing. Assessments
    are per occurrence and per account; `Recovered Messages` is always
    rejected, and equal bytes count for neither acceptance nor
    rejection. Label observations carry account, provider ID, label and
    scan generation with no foreign key to an identity table, keep
    their own retention and survive the reaping of every source; no
    observation or candidate creates a logical-identity row. Logical
    messages are a view over each occurrence's effective accepted
    assessment, and a label joins a specific occurrence's accepted
    assessment in the same account, never through the claimant or
    equal bytes. Logical counts apply each occurrence's eligibility,
    then deduplicate before counting and paging; source counts stay
    the default.
53. **Upstream Gluon and Bridge harness (2026-10-12, #1583):** the
    pinned Gluon and Bridge commits are built and run locally for
    #1316's identity evidence matrix, with acquisition kept apart from
    execution. Commits, the Go toolchain `go.mod` names, modules
    (unchanged `go.sum`, `go mod verify`), the Debian base digest
    (checked against the publisher's) and exact-version packages are
    fetched and verified first, with each artifact's provenance
    recorded; assembly then runs with `--network none`, and no image
    code runs with network access. Execution uses
    one disposable non-root container built from the Debian base, with
    all capabilities dropped, `no-new-privileges`, memory and pids
    limits, no ports, sockets, volumes, mounts, secrets, Maildir or
    Bridge access, inputs by `docker cp`, and `GOTOOLCHAIN=local`,
    `GOPROXY=off` and `-mod=readonly`. Output is allowlisted counts,
    byte-equality outcomes, timings and fixed tokens, with no identity
    values; the harness and synthetic fixtures live under
    `mbsync/tests/upstream/`, and upstream trees, builds and the image
    are removed. Results go into `docs/architecture.md` as executed
    rows; they are necessary, not sufficient, evidence for #1598's
    acceptance rule, and server-side guarantees stay with #1584.

54. **Selected query clocks and paging (2026-10-13, #1150):** the owner
    approved serving `effective`, `sent` and `occurred` on
    `query_messages` and `query_attachments`, retaining keyset paging
    with scan-and-sort for `sent` and `occurred` under the current
    schema. Missing selected clocks remain indeterminate. Index
    optimization stays in #1123; new clock indexes are not a
    correctness prerequisite. The Opus 5.5 and Codex GPT-6.1 Sol
    panel agreed. The synthetic timings establish neither complete-walk
    performance nor tail-latency or concurrency limits.

## Notes for Agents

- Read `AGENTS.md` before making changes.
- Find current work in GitHub issues; this file is direction, not a
  queue. Labels, milestones, blocked-by relations and title shape
  follow AGENTS.md "Issue Conventions".
- The V1 Finish Line says what is required; Phases 0–5 are the
  priority order within it.
- Edit this file only when a decision, roadmap status or known
  limitation changes; keep it concise, and do not add done-lists,
  measurements or PR chronology.

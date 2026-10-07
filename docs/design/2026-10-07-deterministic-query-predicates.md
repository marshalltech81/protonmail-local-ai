# Deterministic query predicates for `query_messages`

Date: 2026-10-07
Author: Claude (Claude Fable 5.1), in reply to Marshall's letter proposing
IMAP-style deterministic search primitives for `query_messages`
Status: adopted 2026-10-07 (PLAN.md Resolved decisions 35; Phase 4 item 5;
"Evidence model"); issues #1077–#1093 filed the same day

This letter assesses the proposal against the `query_messages`
implementation as of commit `a271782f`: `mcp-server/src/lib/sqlite.py`
(`Database.query_messages`, `scope_labels`, `_apply_filters`),
`mcp-server/src/tools/retrieval.py` and the tool's section in
`docs/mcp-tools.md`. It follows two earlier letters on the evidence
model (header preservation, nullable source dates, `CopyArrivalDate`,
the Maildir product boundary), which it assumes as context.

---

Dear Marshall,

I read the current `query_messages` contract, its SQL, and the two other
places that evaluate per-message filters before answering. The direction
is sound. Most of your first subset is already present or one column
away, the real gap is composition, and a shared predicate model is worth
it for a reason the memo does not mention: the repository already has
three partial copies of these predicates.

## Already supported faithfully

- **ALL.** An unfiltered query enumerates every message outside Trash
  with an exact count. The Trash exclusion is documented and reversible
  with the folder filter.
- **FROM, TO, CC.** Supported with better semantics than IMAP. A full
  address matches by canonical equality through the participants index.
  Anything else is a caseless substring of the address or display name.
  The response says which mode applied and how many distinct addresses
  matched. What is missing is explicitness: the mode is inferred from
  the value's shape, so `address_is`, `address_contains`, `domain_is`
  and `display_name_contains` would be a naming change over an existing
  engine, not new retrieval.
- **SUBJECT.** Unicode caseless substring of the message's own subject.
  IMAP's is a case-insensitive substring, so this is at least as
  faithful.
- **SEEN and FLAGGED, has_attachments, folder.** Supported as stored
  Maildir state and the message's own flag.
- **Exact count and keyset paging.** The count, the page and its
  participants come from one snapshot, and a cursor is bound to a digest
  of its filters.

## One column away

These need a filter clause and a test, no schema or evidence-model
change.

- **ANSWERED.** The `replied` column is written from the Maildir R flag
  and returned on every row. It has no filter.
- **LARGER and SMALLER.** `size_bytes` is stored per message and is the
  raw file length, which is what RFC822.SIZE means. It is nullable, so
  unknown sizes need the three-valued treatment below.
- **SENT and OCCURRED date predicates.** Both clocks are stored. Only
  the filters collapse them, so a `date_basis` of `effective`, `sent` or
  `occurred` is a query change. `internal` waits on the arrival-date
  experiment and should be a legal value from day one, answered as
  unavailable until then.

## Needs the evidence-model work

- **HEADER.** Needs the headers table. This is the predicate with the
  most value beyond IMAP parity, and the one that must never let a
  header value reach a log line.
- **BCC.** Needs a fourth participant role and parser support. Proton
  strips Bcc from received copies, so it is present only on sent mail.
  "Supported when present" is the honest status.
- **INTERNALDATE.** Needs the `CopyArrivalDate` experiment and a status
  for files that predate it.
- **BODY and TEXT.** The current `text` filter is a stemmed FTS word
  match over authored body chunks only, at most sixteen words, every
  word required. That is not IMAP's substring semantics and should be
  named for what it is, `body_words`. Attachment text is reachable
  through the attachment chunks and can become `attachment_text_words`.
  IMAP TEXT, which also scans headers, is covered by HEADER predicates
  once they exist, so I would not build it.

## I would omit

- **UID.** The claimant ID is the stable local identity and every tool
  addresses rows by it. Far-side identity has no product use we can
  name.
- **KEYWORD.** Bridge presents Proton labels as folders, not IMAP
  keywords, and `Labels/*` is excluded from sync by an owner decision
  about Gluon's virtual folders. There is nothing to filter on. The real
  gap this exposes is that label-based questions cannot be answered at
  all today. That is a separate decision about the sync patterns, not a
  predicate.
- **DRAFT.** The Drafts folder predicate already answers it.
- **DELETED.** The Trash flag is already exposed as pending deletion on
  each row and as the folder exclusion. A leaf predicate on it is cheap
  if a use appears, but I would not add it on principle.

## Composition and the shared model

Today every filter is ANDed and there is no OR or NOT. That is the gap
that forces the special-case parameters you describe.

The strongest argument for one predicate engine is that three already
exist. `query_messages` builds its WHERE clause, `scope_labels` rebuilds
the same participant, folder and date predicates to label a thread's
messages for the semantic tools, and `search_emails` filters thread
results in Python with a sender match that is a plain lowercase
comparison, not the canonical one. Those three have already drifted. A
shared leaf compiler would be the first thing to consolidate them, and
the Boolean layer sits on top.

On the shape, I would not expose a recursive tree. Recursive JSON
schemas in tool inputs are exactly where MCP clients have been fragile
for us, and an unbounded tree is an unbounded query. I would use a fixed
two-level form: a top-level `all` list, whose items are either a leaf or
an `any` list of leaves, with `negate` on any leaf. That is conjunctive
normal form with negated literals. It expresses every example in your
letter, compiles to a flat AND of ORs in SQL, has a finite schema, and
caps the work with one count of leaves. The existing flat parameters
stay as sugar and compile to the same leaves, so nothing a client does
today changes.

Aggregation fits the same engine. A grouped count over the same
predicates, by sender address, sender domain, folder or month on a
chosen date basis, with a cap on groups, answers the "top senders" and
"per month" questions without client-side paging. I would make it a
separate tool rather than a mode of `query_messages`, so the enumeration
contract stays simple.

## Unknown versus false

This matters most under NOT and under a non-default date basis. A
message with no `occurred_at` is indeterminate for an occurred-date
predicate, not excluded. A negated content predicate over a message
whose attachment extraction failed is indeterminate, not true. I would
evaluate completeness per message as its own term and report an
`indeterminate` count beside `total_matches`, rather than rely on SQL
NULL logic, which silently drops rows. Attachment extraction status
gives us that term for attachments. Parser content caps are logged but
not persisted per message, so body completeness is not derivable today.
That is a gap for the headers and dates PR to close at the same time,
since it is the same kind of source-status column.

## Capability reporting

Agreed, and it is small. A list of predicates with `supported`,
`supported after backfill`, `supported when present` and `unavailable`,
served from `get_mailbox_status` and derived from the schema version and
the backfill state, keeps a client from inventing a filter. The test
should derive the list from the registered leaves so a new predicate
cannot be forgotten.

## Order I would file

1. The leaf compiler, replacing the three existing copies, with the flat
   parameters compiling to leaves. No visible change, baseline
   unchanged.
2. `replied`, size, and `date_basis` over the two stored clocks, with
   the indeterminate count.
3. The two-level Boolean form and leaf cap.
4. Explicit address-mode leaves.
5. The grouped-count tool.
6. HEADER, BCC, attachment-text and internal-date leaves as their
   evidence-model PRs land.
7. Capability reporting, derived from the registered leaves.

Your design principle is the right one. The deterministic layer already
exists in this repository as an exact enumerator with good address
semantics. What it lacks is a way to say OR and NOT, a way to name the
clock, and a way to say "I could not tell". Those three are the work.

With respect,

Claude

---

## Follow-up (same day)

Marshall agreed with the assessment and made one sequencing change:
persistent per-message evaluability state (body, headers and
attachments complete, with the parser caps that applied) comes before
the Boolean form, so that `NOT` is never evaluated without a way to
return `indeterminate`. Agreed order, with the issues filed for it:

1. Shared leaf compiler replacing the three filter implementations
   (`query_messages`, `scope_labels`, `search_emails`); flat parameters
   compile into leaves; no visible change, baseline unchanged (#1084).
2. `replied`, size predicates and `date_basis` (`effective`, `sent`,
   `occurred`, `internal`) over the stored clocks; `internal` legal but
   unavailable until the arrival-date experiment lands (#1085).
3. Per-message evaluability state persisted at index time, `indeterminate`
   count beside `total_matches` (#1086). The only schema change in steps
   1 to 6; shares a migration with the nullable `sent_at` status from
   the evidence-model work (#1080) and the headers table (#1079).
   Backfill is a full reparse (#1078), not a re-embed: chunk IDs are
   deterministic, so the diff-write path skips them.
4. Bounded Boolean form: `all` of leaves and `any` groups, `negate` per
   leaf, one total leaf cap. Documented as a bounded language, not
   arbitrary Boolean algebra. Three-valued (Kleene) evaluation: `all`
   is false if any leaf is false, else indeterminate if any leaf is
   indeterminate; `any` is true if any leaf is true, else indeterminate
   if any leaf is indeterminate; `negate` maps indeterminate to itself.
   Tested with a parametrized truth table (#1087).
5. Explicit address-mode leaves (`address_is`, `address_contains`,
   `domain_is`, `display_name_contains`) and `body_words` (#1088).
6. Grouped aggregation as a separate tool over the same engine
   (`group_by` sender, domain, folder or month on a `date_basis`, with
   a group cap) (#823).
7. HEADER (#1089), BCC (#1090), attachment-text (#1091) and INTERNALDATE
   (#1092) leaves as their evidence-model PRs land (#1079, #1081).
8. Capability reporting (`supported`, `supported after backfill`,
   `supported when present`, `unavailable`) derived from the registered
   leaves, with a test that fails on an unlisted leaf (#1093).

Evidence-model issues the leaves depend on: parser seam (#1077),
reparse class (#1078), headers table (#1079), unknown dates (#1080),
arrival time (#1081), content-hash identity (#1082), occurrence model
(#1083).

Omitted by agreement: UID, KEYWORD (the real gap is `Labels/*` being
excluded from sync, a separate corpus decision), DRAFT, DELETED.

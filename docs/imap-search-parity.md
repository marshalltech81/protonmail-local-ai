# Deterministic search and IMAP SEARCH

This is a local query API, not an IMAP command parser. The criterion names below
refer to [RFC 3501 section 6.4.4](https://www.rfc-editor.org/rfc/rfc3501#section-6.4.4).
The supported surface is `query_messages`, with the same predicates available on
`query_threads` and `aggregate_messages`. Retrieval remains thread-based for
`search_emails` and `get_evidence`.

Every count covers the currently indexed local snapshot. It is not a count of a
live selected IMAP mailbox. Read `get_mailbox_status` for collection state,
`current`, queue/dead-letter counts and predicate evidence coverage. Mail that has
not been indexed is outside both matches and indeterminate counts. Default scope
requires an active copy outside Trash; the flat `folder` parameter changes that
scope. A `where` folder leaf intersects the flat scope; to search Trash, also
set flat `folder="Trash"`. All folder/flag/authority occurrence terms of the
Boolean expression must hold on one eligible copy. NOT is evaluated on that
copy before the message-level existence decision.
Claimant IDs distinguish different files claiming the same Message-ID. Optional
Message-ID grouping is technical grouping, not authenticated message identity.

## Criterion matrix

“Local” means a documented local counterpart, with the differences shown; it does
not promise wire-protocol equivalence. Explicit address, header, date, size,
folder, state and phrase predicates can participate in the bounded Boolean form.

| Criterion | Local counterpart or intentional boundary |
|---|---|
| `ALL` | No optional filters: indexed messages in the default copy scope |
| `ANSWERED`, `UNANSWERED` | `replied=true/false`, observed Maildir R state |
| `SEEN`, `UNSEEN` | `seen=true/false`, observed Maildir S state |
| `FLAGGED`, `UNFLAGGED` | `flagged=true/false`, observed Maildir F state |
| `BCC` | Explicit Bcc-role address leaves; observed positives only, unmatched evidence stays unknown (#1090, #1644) |
| `FROM` | Explicit From-role address/name leaves; ambiguous or incomplete evidence can be unknown |
| `TO`, `CC` | Explicit To/Cc address/name leaves; incomplete capture can be unknown |
| `SUBJECT` | `subject_contains`: casefolded substring in the retained subject; no match is unknown if subject capture is incomplete |
| `HEADER` | `header_exists` or `header_contains` with an explicit header name; stored top-level header evidence, with its existing completeness rules |
| `BODY` | `body_phrase`: literal text in each complete decoded inline MIME rendering, including quotes/signatures and retained alternatives. HTML is converted text, not raw markup. Authored-only and classified scopes are separate leaves |
| `TEXT` | No single equivalent over every header and body. Combine named header/subject/body/attachment predicates explicitly; existing `text` remains stemmed authored-body words |
| `BEFORE`, `ON`, `SINCE` | No IMAP internal-date equivalent (#1092). Independent `occurred_from/to` use stored delivery evidence; `effective_from/to` use delivery then send-date evidence |
| `SENTBEFORE`, `SENTON`, `SENTSINCE` | Independent `sent_from/to` use parsed Date-header evidence. Bounds are inclusive ISO instants; date-only operands cover UTC days. Construct an exclusive boundary as the preceding representable microsecond |
| `LARGER`, `SMALLER` | `size_min/max` are inclusive bounds on local file bytes. Adjust a strict boundary by one byte within the accepted integer range; this is not the server's RFC822.SIZE |
| `NOT`, `OR`, parenthesized combinations | `negate` on a leaf, `any` groups within `all`; at most 16 nodes. Arbitrary nesting and an IMAP syntax string are rejected |
| `UID`, sequence sets | Unavailable: no selected-mailbox UID/sequence-number namespace is indexed |
| `KEYWORD`, `UNKEYWORD` | Unsupported arbitrary server keywords. Provider label observations are not accepted identities (#1313, #1598) |
| `DRAFT`, `UNDRAFT` | Intentionally unsupported server Draft flag. A folder named Drafts does not establish that flag |
| `DELETED`, `UNDELETED` | Intentionally unsupported server Deleted flag; local Trash/reaping state has different semantics |
| `RECENT`, `NEW`, `OLD` | Unavailable session-relative server state |
| `CHARSET` | No IMAP charset option. Operands are Unicode strings; phrase matching uses Python Unicode casefold with original-character boundaries |

Address comparisons are chosen explicitly: canonical equality, substring, domain,
display name or address-or-name. They describe claimed headers, not authenticated
senders. The flat `participant` filter retains its existing visible-role meaning;
explicit Bcc-inclusive roles do not establish recipient exhaustiveness.
All independent date leaves preserve missing evidence as unknown. The existing
flat date bounds and page `date_basis` keep their prior meanings. UTC day bounds
and preserved instants differ from an IMAP server's date-only comparisons.

## Literal phrases

The six `where` leaves are `body_phrase`, `authored_body_phrase`, `quote_phrase`,
`signature_phrase`, `forwarded_phrase` and `attachment_text_phrase`. They accept
1–1000 codepoints **after casefolding**. Leading/trailing spaces, punctuation,
line endings and NUL remain significant. There is no stemming, token adjacency,
whitespace normalization or word-boundary requirement. A match must start and end
at original character boundaries: `ss` matches `ß`, but `s` does not match inside
that expansion.

A phrase can cross an internal 8192-character fragment boundary within one
contiguous source. Separate MIME renderings and separate attachments are never
joined. Classified scopes use the preserved source classifier's contiguous runs;
classification is heuristic and independent of retrieval chunking. Attachment
phrases search the occurrence's current extracted representation, including PDF
or OCR text when that representation exists. They do not search raw file bytes.
Failed, deferred, stale or incomplete extraction cannot certify a negative.

A retained current positive can match even when other source evidence is
incomplete. A no-match is false only with complete indexed evidence for that
scope; otherwise it is unknown and contributes to `indeterminate`. AND, OR and
NOT preserve that distinction. Unknown schema/evidence is never interpreted as
absence. Unsupported leaves and invalid operands are rejected before execution.

For example:

```json
{"all":[
  {"leaf":"sent_from","value":"2026-01-01"},
  {"any":[
    {"leaf":"body_phrase","value":"approved  amount"},
    {"leaf":"attachment_text_phrase","value":"approved  amount"}
  ]}
]}
```

Counts, pages and per-leaf diagnostics use one read snapshot. Cursors bind the
normalized expression, date basis and literal semantics version. A later page
opens a new snapshot; concurrent indexing can change the population.

## Work and storage bounds

The index stores fixed ASCII-encoded gram memberships in separate FTS5 indexes.
Long operands select at most eight distinct three-codepoint grams with AND,
without positional phrase matching. Short operands use original-boundary-aligned
one/two-codepoint memberships. Candidate windows are verified against the original
source, including scope boundaries; counts and paging reuse matched integer IDs.
There is no fallback to scanning every document.

The stored Unicode casefold version must match the reader; a mismatch leaves
phrase evidence unassessed until compatible reindexing, never a definite miss.
Each message shares admission limits across body and attachment windows: 10,000
windows, 128 MiB source-window bytes, 64 MiB logical metadata and 512 MiB charged
membership construction. Body windows reuse the existing source fragments;
attachment text is read incrementally into bounded fragments. No attachment
payload bytes enter SQLite. A cap leaves the affected negative evidence unknown,
retains admitted current positives and emits rate-limited count-only warnings.

Each leaf admits at most 200,000 candidates, with one million across the request
and 64 MiB of source/metadata verification transfer. A cooperative ten-second
budget covers preparation and subsequent count/page work. It is not a hard wall
clock interrupt inside one native SQLite operation. Exhaustion is a fixed query
error: no partial counts or scan fallback. Broad one-character searches can
therefore require a more selective phrase or a smaller combination of leaves.

Schema v25 queues a reparse through the normal job queue. Unchanged retrieval
chunks retain their IDs and vectors. Until backfill assesses a message, new
phrase evidence remains unknown. Index replacements and source deletion remove
both literal posting rows and use the existing FTS merge/scrub policy.

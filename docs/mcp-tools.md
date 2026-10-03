# MCP Tool Reference

All tools are available inside Claude Desktop once the stack is running.

## Response format

The search, retrieval, and system tools (Groups 1, 2, and 4) publish an
`outputSchema` and return two views of the same result:

- `content` — the readable prose described below, unchanged.
- `structuredContent` — typed JSON matching the tool's `outputSchema`.
  IDs chain from typed fields: `search_emails` → `results[].thread_id` →
  `get_thread` → `messages[].claimant_id` → `get_message`, and
  `get_evidence` / `search_attachments` carry `attachment_id`. Paging
  state is typed as well (`get_thread.next_offset`,
  `query_messages.next_cursor` / `has_more` / `total_matches`).

**Message-ID and claimant ID.** The sender sets a message's Message-ID,
so two different indexed files can carry the same one (a reused or
forged ID). The index keeps both rather than letting one overwrite the
other, and names each by its claimant ID: the Message-ID plus `#` and
the first sixteen hex digits of the raw file's SHA-256, for example
`<id@x.example>` stored as `id@x.example#3f9a2c1b7d40e865`. It stays the same
across flag renames and folder moves, since those do not change the
file's bytes. Every message row, evidence chunk, and attachment hit
carries both `message_id` (the header value) and `claimant_id`.
`get_message` accepts either; a bare Message-ID that several messages
claim returns an error listing their claimant IDs instead of choosing
one. Both claimants sit in the thread their Message-ID resolves to.

Every message row (`get_thread`, `get_message`, `query_messages`),
evidence chunk (`get_evidence`), and attachment hit
(`search_attachments`) carries `source_file`: the raw message file the
result came from, so an answer can be checked against the original
bytes. It holds `source_type` (`maildir_message`), `locator` (the
file's path in the Maildir volume as the indexer sees it, `/maildir/...`,
kept current across mbsync flag renames), `sha256` and `size_bytes` of
the raw file when it was indexed (null if not recorded), and
`indexed_at`. For an attachment chunk or hit it is the file of the
message that carries the attachment. It is read in the same query as
the result it belongs to. To verify a cited passage, hash the file:
`docker compose exec indexer sha256sum <locator>`.

Structured output is bounded because headers are sender-controlled.
Lists hold at most 10 entries (recipients per role, References, thread
participants, attachment senders), each with a full count (`to_count`,
`references_count`, `participant_count`, `sender_count`, ...). Header
values past 500 characters (subjects, display names, addresses, reply
headers, participant and sender strings, attachment filenames and MIME
types) are cut with a marker. Every tool applies the same cut in its
prose, the intelligence tools apply it to the headers they send to the
model, and `get_thread` also cuts bodies. IDs are never cut, since a shortened ID
would not chain; `get_thread` states the thread ID once rather than on
every message row. `get_message` bounds headers the same way and pages
the body by character offset, so every page is bounded and the pages
together hold the whole body.

A failure (unknown thread or message, invalid argument, provider or
database error) is an MCP error result (`isError: true`); it carries no
structured content. Fixed messages state the reason: an unknown thread
or message, an invalid argument (naming the field), an unavailable
vector index. Any other database or conversion error is reported, and
logged, as its exception type name alone (for example `Error:
OperationalError`), because an SQLite message can quote the query or
stored mail; the server log then shows the same type for that call. An
empty match is not a failure: it is a normal result with an empty list.

In the tools that call the embed or inference provider (`search_emails`,
`get_evidence` and the intelligence tools), a provider or database
failure is reported, and logged, as follows, because a provider's
response or an SQLite message can quote the query or the mail: an SDK
status error as its type and status code, a connection or timeout error
and the server's own fixed-text errors (such as an empty or wrong-sized
response) in full with secrets redacted, and anything else as its
exception type name alone.

Of the intelligence tools (Group 3), `ask_mailbox`,
`summarize_thread` and `extract_from_emails` publish an
`outputSchema`: the answer, summary or records with checked citations
(see [`ask_mailbox`](#ask_mailbox),
[`summarize_thread`](#summarize_thread) and
[`extract_from_emails`](#extract_from_emails)). The opt-in experimental `brief_issue` and `check_conclusion` also
publish an `outputSchema` (see [Experimental tools](#experimental-tools));
unlike the others, their format may change.

Arguments are checked against each tool's input schema before the tool
runs: a wrong type or an argument the tool does not declare is an error
result naming the problem.

## Stage timings in the server log

`search_emails`, `get_evidence`, `search_attachments`, the three
intelligence tools and the experimental `brief_issue` and
`check_conclusion` log one line per call at `INFO` on the `mcp.timings`
logger, on success and on failure:

```text
tool=search_emails outcome=ok total_ms=41.7 stages_ms={'query_embedding': 22.4, 'thread_fts': 3.1, 'chunk_fts': 2.0, 'attachment_fts': 0.9, 'thread_vec': 4.6, 'chunk_vec': 6.2, 'fusion': 0.8} counts={'thread_fts': 4, 'chunk_fts': 9, 'attachment_fts': 0, 'thread_vec': 100, 'chunk_vec': 812, 'filtered': 57, 'results': 10} config={'rerank': 'none'}
```

- `stages_ms` holds only the stages that ran, so a keyword-mode search
  has no `query_embedding` or vector lanes. Stages: `query_embedding`,
  `contact_lookup` (the `from_name` resolution), the keyword lanes
  `thread_fts` / `chunk_fts` / `attachment_fts`, the vector lanes
  `thread_vec` / `chunk_vec` (each covering every widening step of a
  filtered search), `fusion` (RRF plus post-fusion filters),
  `evidence_fetch`, `rerank`, `attachment_search` and `inference`
  (summed over every completion the call made).
- `counts` holds candidates per lane, `filtered` (after fusion and
  filters), `results`, `evidence_chunks`, `rerank_candidates`,
  `inference_calls` and, on a filtered vector search,
  `thread_vec_expansions` / `chunk_vec_expansions` (re-queries with a
  wider window).
- `config` names the rerank and inference modes.

The line carries names fixed in the code, numbers and mode names only:
never the query, other arguments, subjects, addresses, bodies or
provider responses. `total_ms` minus the stage sum is the time spent
outside the timed stages (validation, row conversion, prompt building).

## Trash is left out by default

Mail in the `Trash` folder stays synced and indexed, but mailbox-wide
tools leave it out unless the call names it (#441). Under mirror
retention a message deleted in Proton lives on as its Trash copy until
it is purged from Trash, so without this a deleted message kept
turning up in search. Spam and every other folder are searched as
before.

- Thread tools (`search_emails`, `get_evidence` without `thread_id`,
  `ask_mailbox`, `extract_from_emails`, `brief_issue`,
  `check_conclusion`, and `summarize_thread`'s subject-phrase fallback)
  leave out a thread only when **every** message of it is in Trash.
  This is the per-message membership the `folders` filter uses: a
  thread with one message in Trash and a reply in INBOX stays, and its
  Trash message's passages can still appear as evidence. Passing
  `folders` replaces the default, so `folders=["Trash"]` searches
  Trash and `folders=["INBOX", "Trash"]` both. `search_emails`
  resolves `from_name` over the same scope, so a sender whose mail is
  all in Trash is not chosen for a default search.
- Message tools leave out the messages filed in Trash: `query_messages`
  without `folder` (pass `folder="Trash"` to list them) and
  `search_attachments`, which has no folder filter.
- Tools that read one named thread or message (`get_thread`,
  `get_message`, `get_evidence` with `thread_id`, `summarize_thread`
  with a thread ID) and the folder browsers (`list_threads`,
  `list_folders`) are unaffected.
- The exclusion counts as a filter for the vector lanes' window
  widening, so a mailbox whose closest matches are in Trash still finds
  its best match elsewhere. A mailbox with no Trash mail runs the same
  unfiltered search as before.

The excluded folder list is `DEFAULT_EXCLUDED_FOLDERS` in
`mcp-server/src/lib/sqlite.py`, matched exactly as `folders` values
are.

## Reaped sources

Under mirror retention the indexer reaps a message once its grace
window passes after it was deleted in Proton or its file went missing
from the local Maildir. A claimant ID, Message-ID or thread ID taken
from an earlier answer (a citation, a search hit) can then name a
source the index no longer holds. Such a lookup reports the reap
instead of reading like an ID that never existed:

- `get_message` fails with `Message reaped from the index on <date>
  (mirror retention): <id>` when no live message has the ID and one
  it names was reaped.
- `get_thread` and `get_evidence` with `thread_id` fail with `Thread
  reaped from the index on <date> (mirror retention): <id>` when the
  whole thread was reaped.
- `get_thread` on a thread that survives a partial reap lists the
  reaped messages' claimant IDs and reap times in `reaped_messages`
  (oldest first, at most 20, with `reaped_messages_truncated`), so a
  cited message missing from the page is accounted for.

The date is when the local index reaped the source, not when it was
deleted upstream (that is at least the grace window earlier), and the
record does not say which of the two causes applied. The live lookup
and the reap record are read in one snapshot, and a bare Message-ID
reads one record however many reaped files claimed it. A reaped
claimant ID keeps naming its reaped file: a live message whose
sender-chosen Message-ID equals that string does not answer for it
(it stays reachable by its own claimant ID). Thread-scoped
`get_evidence` checks the thread again when the evidence fetch finds
no passages, so a reap that lands while the query is embedded reads
as reaped rather than as no evidence.

The reaped content is gone: the index keeps only the message's
claimant ID, Message-ID, thread ID and reap time, never its subject,
body, participants or attachments. These records last 30 days after
the reap; after that the lookup returns `not found` again. They live
only in the index, so rebuilding the index from Maildir drops them
too: a reaped file is not reindexed, and an ID reaped before the
rebuild then reads as `not found`. A message restored upstream is
indexed again under the same claimant ID and reads as live. Other
tools that take a thread ID (`summarize_thread`) are unchanged.

## Group 1 — Search

### `search_emails`
Search the mailbox and return matching **threads** (conversations), not
individual messages. Each result bundles its messages with subject,
participants, date range, folder, and a short snippet. To read the
contents of a returned thread, follow up with `get_thread` or
`summarize_thread` using the result's `Thread ID`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural language or keyword query |
| `mode` | string | `hybrid` | `hybrid`, `semantic`, or `keyword` |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Filter by canonical sender address (or domain like `@example.com`); substring fallback when the value can't canonicalize |
| `from_name` | string | none | Filter by sender name; resolved through `find_contact` to a canonical address before applying, matching any display name the address carries in a From header on a thread it primarily sent (the index keeps no author order within one message, so a name written for it as a second author on such a thread also matches). Use when the user names a person but not their email. `from_addr` wins if both are given. |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Filter by attachment presence |
| `participant` | string | none | Filter to threads where this person appears in **any** role — From, To, or Cc. Distinct from `from_addr`/`from_name`, which are sender-only. Accepts an address, a domain (`@example.com`), or a name fragment |
| `limit` | int | `10` | Max threads to return |
| `authority_class` | string | none | Keep threads with a message whose From sender carries this source-authority class: `counsel`, `management`, `vendor`, `government`, `personal`, `other`, or `unclassified`. Assigned by the operator's rules file (`docs/setup.md`); a filter only, never a ranking weight. Spam-folder messages never count, so a thread matches only through its non-Spam messages. Blank is ignored; any other value is an error |

**When to use which mode:**
- `hybrid` — best for most queries (default)
- `keyword` — exact names, invoice numbers, email addresses
- `semantic` — conceptual queries, topic-based search

An unrecognized `mode` returns an error; it is not silently remapped
to `hybrid`. In `semantic` mode, if neither vector table
(`threads_vec`, `message_chunks_vec`) can be queried, the call returns
an error rather than an empty result; if only one fails, the other
still answers. `limit` is clamped to `[1, 50]` at the tool boundary so
an out-of-range value (e.g. from an LLM-generated tool call) cannot
drive an unbounded query against the index.

**Query handling notes:**
- Keyword queries are tokenized before hitting FTS5. Punctuation,
  colons, and unbalanced quotes are stripped so natural search strings
  (``"Who sent the invoice?"``) run a valid ``MATCH`` instead of
  silently returning no results. Email addresses and hostnames are
  preserved as single tokens, and combining marks stay inside their
  word, so a decomposed accent (`e` + U+0301) matches like the
  precomposed letter. Known gap: the indexes tokenize with unicode61
  `remove_diacritics=1`, which does not fold a precomposed letter
  carrying two diacritics (Vietnamese `ệ`) and does not relate composed
  and decomposed spellings of scripts it does not fold (Hangul), so
  those spellings match only the form the mail was indexed in until an
  index-side normalization and reindex.
- If FTS5 still rejects a sanitized query, search falls back to a
  ``LIKE`` scan over subject / body / participants so recall is
  preserved. The scan matches the query as a literal substring: `%`,
  `_` and `\` in the query match only themselves.
- A folder filter keeps a thread when any of its messages is filed in
  one of the named folders, the same membership `list_threads` and
  `list_folders` use, so a thread started in INBOX with a reply in Sent
  matches `folders=["Sent"]`. The thread's reported `folder` stays its
  representative folder.
- When any filter (folder, sender, date range, attachment flag, authority class) is
  applied, search oversamples raw candidates by ``limit * 4`` rather
  than ``limit * 2`` so deeper-ranked matches still qualify after
  filtering. The vector lanes then widen their KNN window (doubling,
  up to sqlite-vec's cap of 4096 rows per lane) until it holds enough
  threads that pass the filters, so a narrow filter still finds its
  best semantic match when many closer out-of-scope threads exist.
  Results remain a ranking, not an exhaustive list of every match.
- Date bounds accept either a full ISO 8601 timestamp or a date-only
  value. Every date-only form Python's `date.fromisoformat` accepts
  counts (`"2024-12-31"`, `"20241231"`, the week date `"2025-W01-2"`),
  with or without a trailing `Z`; date-only values are promoted to
  start/end of day in UTC before being pushed into SQL so the filter
  matches the full day the user named. A timestamp, including an
  explicit midnight, bounds at that instant; naive timestamps are read
  as UTC.
- A `date_from` later than `date_to` names an empty interval and is
  rejected with an error naming both fields, the same way by every tool
  that takes both bounds. The bounds are compared after UTC
  normalization and date-only promotion, so `date_from` and `date_to`
  set to the same date select that whole day.
- Date bounds apply to each message's effective time: its delivery
  date (`occurred_at`, the date of its topmost `Received:` header, in
  UTC) when known, else its send date (`sent_at`, its `Date:` header in
  UTC). A thread matches when its span, from its messages' earliest to
  latest effective time, overlaps the range, so a thread with messages
  either side of a short range matches it. The tools that hand passages
  to a model (`get_evidence`, `ask_mailbox`, `extract_from_emails`,
  `brief_issue`, `check_conclusion`) retrieve threads the same way, and
  any passage of a matching thread may be shown, including passages
  from messages outside the range. Each passage carries its own
  message's `sent_at` and `occurred_at` (null when unknown), so its
  dates stay visible; see [Message time](architecture.md#message-time).

---

### `get_evidence`
Return the exact indexed passages (evidence chunks) that back a
question — the same chunks `ask_mailbox` feeds its model, but with
**no LLM synthesis**. Use it to audit or cite an answer, or as the
fast synthesis-free path when only the source text is needed.

Each chunk carries its `chunk_id` (the ID `ask_mailbox` citations
name), its parent thread, Message-ID and claimant ID, the source
(message body, or an attachment with filename + MIME type), its
`kind` (`body`, `quote`, `signature`, `forwarded` or `attachment`;
`calendar` is reserved; quote, signature and forwarded passages come
only from messages with no text of their own, and the prose names
them as `Source: message body (<kind>)`), its
message's send and delivery dates (`sent_at` and `occurred_at`, the
same values and format as that message's headers), and the passage's
character offsets. With `date_from` / `date_to`, threads are selected
by span as in `search_emails`, and their passages can come from
messages outside the range; check each chunk's `occurred_at` and
`sent_at`. Attachment-derived
chunks (extracted PDF / OCR / document text) are included — unlike
`get_thread`, which is body-only.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | The question or topic to gather evidence for |
| `thread_id` | string | none | Scope evidence to one thread; omit to search the whole mailbox. Rejected in combination with `folders`, `from_addr`, `date_from`, `date_to`, `has_attachments` or `max_threads`, which select threads |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Filter by sender address or domain |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Restrict to threads with attachments |
| `max_threads` | int | none | Rank threads exactly as `ask_mailbox` does with this `max_threads` and return their evidence; clamped to `[1, 10]` like `ask_mailbox`'s. Omit it to rank by `limit` instead |
| `limit` | int | `12`, or `max_threads` × 6 | Max evidence chunks to return (with `max_threads`, a smaller value keeps the first `limit` chunks of the audit set in rank order); clamped to `[1, 60]`, the most `ask_mailbox` can put in one prompt (10 threads × 6 chunks), so the cap never cuts below an answer's evidence set |
| `include_scores` | bool | `false` | Annotate each thread with the retrieval lanes that matched (`thread_fts` / `chunk_fts` / `attachment_fts` / `thread_vec` / `chunk_vec` / `rerank`) and each chunk with its vector distance |

The mailbox-wide path runs the same hybrid retrieval as `ask_mailbox`
(the same code), with the same cap of six chunks per thread, and
flattens the per-thread evidence into a flat `limit`-chunk budget, at
up to 1,600 characters a chunk. With `max_threads` set, retrieval ranks
that many threads, the number that sizes the lane fetch and the
reranker's candidate pool, so `get_evidence(query, filters,
max_threads=N)` returns the evidence `ask_mailbox(question=query,
filters, max_threads=N)` retrieved: the same threads in the same order,
each with the same chunks in the same order
([#537](https://github.com/marshalltech81/protonmail-local-ai/issues/537)).
This holds at the default `limit` (`max_threads` × 6, the most those
threads can carry). A smaller `limit` cuts the same set in rank order:
the first `limit` chunks, with the threads past the cut left out.
A selected thread with no indexed chunks is listed in its place with
an empty `chunks` list, since `ask_mailbox` shows the model that
thread's indexed text instead; read it with `get_thread`.
`has_attachments` is not an `ask_mailbox` filter; leave it unset for an
audit. Without `max_threads`, `limit` also sets how many threads are
ranked, so the result can surface different threads from an answer
(more so with a reranker), and when the top threads are short the
budget takes passages from lower-ranked threads.
The `thread_id`-scoped path returns that thread's chunks ranked
against the query the way `ask_mailbox` ranks them: chunks of any
attachment whose filename or MIME type the query matches come first
(strongest match first), then the thread's other attachment chunks,
then body chunks, each group by vector distance. With no attachment
match the order is vector distance alone. At `limit=6` the result is
the slice `ask_mailbox` gives its model for that thread. This path
bypasses RRF fusion, so `include_scores` shows per-chunk vector
distance but no lane provenance. A `thread_id` whose thread was reaped
fails with `Thread reaped from the index` rather than `Thread not
found` ([Reaped sources](#reaped-sources)).

### `search_attachments`
Locate indexed attachments by filename, MIME type, and extracted
text. Use it for attachment-centric questions ("find the quote PDF
from Acme", "which emails had W-2 attachments?"). With no `query` it
lists attachments by the structured filters alone, newest message
first. Each result carries `sent_at` and `occurred_at`, the send and
delivery dates of the message carrying the attachment, beside its
thread's `date_last`; the date filters and the no-query order use the
message's effective time (`occurred_at`, else `sent_at`).

To read what an attachment says, use `get_evidence` (the matching
passages of its extracted text, each capped at 1600 characters) or
`ask_mailbox` (an answer synthesized from those passages). This tool
locates attachments and previews their extracted text; none of the
three returns the whole document.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | none | Match against filename, MIME type, and extracted text; omit to list by filter alone |
| `content_type` | string | none | Exact MIME-type filter, e.g. `application/pdf`; blank means no filter |
| `from_addr` | string | none | Restrict to attachments on threads sent by this address or domain |
| `date_from` | string | none | ISO 8601 date lower bound on the carrying message's effective time (`occurred_at`, else `sent_at`) |
| `date_to` | string | none | ISO 8601 date upper bound |
| `extracted_only` | bool | `false` | Return only attachments whose text extraction succeeded |
| `limit` | int | `20` | Max attachments to return; clamped to `[1, 50]` |

Two FTS lanes run when `query` is set — the attachment filename/MIME
index and the extracted-text index — with filename matches listed
first. Each result reports the parent thread so a follow-up
`get_thread` / `get_evidence` call can round-trip. When one message
carries the same file more than once (under different names or MIME
types), an extracted-text match is reported once, as the first copy
that passes `content_type`. Attachments on messages filed in Trash are
left out; the tool has no folder filter, so reach them through
`search_emails(folders=["Trash"])` or `get_evidence` on the thread
([Trash](#trash-is-left-out-by-default)).

---

## Group 2 — Retrieval

### `get_thread`
Read a thread by ID as its messages, oldest first. Each message shows
its own headers (Message-ID, claimant ID, subject, From / To / Cc, send date and,
when known, delivery date (`occurred_at`) in UTC,
folder, In-Reply-To, attachment flag, [read state](#read-state);
recipient lists past 10 are summarized as a count) and its indexed body after quoted-reply
stripping. Attachment text is not included. When no message body is
indexed yet, the accumulated thread text (a retrieval artifact that
also carries quoted replies) is shown instead.

Responses are bounded: messages are paged (the response states the
thread's message count and the `offset` for the next page), and each
body is cut at 4,000 characters with a marker stating how many were
left out — `get_message` pages through the full body. Header content is
sender-controlled, so it is bounded the same way: at most 10 recipients
per role, 10 thread participants, and 10 References are listed (with a
"+N more" count), and any header value past 500 characters is cut with
a marker. The page is read from one database snapshot.

Messages of the thread reaped under mirror retention are listed by
claimant ID and reap time in `reaped_messages`; a fully reaped thread
fails with `Thread reaped from the index` rather than `Thread not
found` ([Reaped sources](#reaped-sources)).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID from search results |
| `include_attachments_metadata` | bool | `true` | Show the local attachment-availability note when the indexed thread has attachments |
| `offset` | int | `0` | Messages to skip, oldest first |
| `limit` | int | `10` | Messages per page; clamped to `[1, 50]` |

### `get_message`
Return one message's own headers — subject, From / To / Cc, send date
and, when known, delivery date (`occurred_at`) in UTC, folder,
In-Reply-To, References, attachment flag, [read state](#read-state) — with its thread ID and
subject, and one page of its indexed body reconstructed from the
per-message chunk store (overlap between adjacent chunks is removed by
character offset). The index keeps no raw per-message body, so this is
the indexed text **after quoted-reply stripping**; it falls back to
thread context when no body chunks are indexed for the message.
Attachment text is not included — use `get_evidence` for that. The
prose ends its header block with the raw source file's path, size, and
SHA-256.

Headers are bounded like every other tool's: at most 10 recipients per
role and 10 References are listed, with a "+N more" note in the prose
and the full count in `to_count`, `references_count`, ...; any header
value past 500 characters (the thread subject included) is cut with a
marker.

The body is paged by character offset (#489). A page holds at most
20,000 characters: about 6,700 tokens at the 3 characters per token the
inference budget counts, a fifth of the default 32,768-token context
window and half of a full `get_thread` page (10 bodies of 4,000
characters). The structured output carries the page in `body`, its
start in `body_offset`, the whole body's length in `body_total_chars`,
and `next_offset` when more remains (null at the end); the prose states
the character range shown and the offset for the next call. Calling
with each `next_offset` in turn, from 0, returns pages whose
concatenation is the whole body. A cut never splits a code point, and
one that would separate a combining mark or a zero-width-joined
character from the character before it moves back (at most 32 code
points) so the sequence starts the next page. An `offset` equal to the
body's length returns an empty page; a negative one, or one past the
end (any offset above 0 when no body is indexed), fails as an invalid
argument naming `offset`. Each call rebuilds the body from its chunks,
so a page reflects the index at the time of that call.

`message_id` takes a claimant ID, which names one message, or a bare
Message-ID, which works while one indexed message carries it. When
several do, the call fails with an error listing each claimant ID with
its send date and folder, oldest (by effective time) first; call again with one of them. A
successful response lists, in `other_claimants`, any other messages
sharing the Message-ID, in claimant-ID order. The sender sets the
Message-ID, so either list is capped at 20 entries: past that the error
says the Message-ID names "more than 20" messages and lists the oldest
20, and a successful response sets `other_claimants_truncated` (false
otherwise). Each list walks a `messages` index in its own order and
stops one row past the cap, so the cost does not grow with the number
of files claiming the Message-ID. An ID whose message was reaped under
mirror retention fails with `Message reaped from the index` rather
than `Message not found` ([Reaped sources](#reaped-sources)).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `message_id` | string | required | Claimant ID, or the Message-ID header value |
| `offset` | int | `0` | Body character to start the page at; pass the previous response's `next_offset` |

### Read state

Every message row (`get_thread`, `get_message`, `query_messages`)
carries `seen`, `flagged` and `replied`: whether the message is read,
flagged (starred) and answered in Proton. mbsync mirrors that state into
the Maildir filename's `:2,` flags (`S`, `F`, `R`) and the indexer
records it from there, so it is as current as the last sync. A message
delivered to `new/` without flags reads as unread. The prose shows it as
`Status: read, flagged, replied` (or `unread`). The server is
read-only: nothing here changes the state in Proton.

### `list_threads`
Browse threads in a folder: every thread with at least one message
filed in it, newest activity first. A thread's `folder` field is its
representative folder: where the message that started it was filed
when the thread was first indexed (not updated when messages move). A
thread listed under `Sent` because of one sent reply can still report
`INBOX`.

`unread` and `flagged` read each message's [read state](#read-state):
a thread is listed when one of its messages filed in `folder` is unread
(or flagged). An unread reply filed elsewhere does not list the thread
here. The structured output echoes `filter_type`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `folder` | string | `INBOX` | Folder to list |
| `filter_type` | string | `all` | `all`, `unread` (threads with an unread message in `folder`) or `flagged` (threads with a flagged / starred message in `folder`); any other value is an error |
| `limit` | int | `20` | Number of threads |
| `offset` | int | `0` | Pagination offset |

### `list_folders`
List every folder holding at least one indexed message, with its
thread count: the number of distinct threads with a message in that
folder, the same threads `list_threads(folder=...)` pages through. A
thread with messages in several folders counts once in each, so the
counts can sum to more than the total thread count.

### `find_contact`
Resolve a name / address / domain fragment to canonical email
addresses found in the index. Use it when the user asks **about** a
person ("do I have Jane Smith's email?", "show me everyone at
example.com"). For "emails from Jane Smith", call
`search_emails(from_name=...)` directly instead: it resolves the name
internally to the most-active matching sender and reports the address
it used in `resolved_from_addr`. Resolve a name here first only when
you need a different matching contact than that one (then pass it as
`from_addr`), or for a tool that filters by address alone, such as
`get_evidence` or `search_attachments`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Name, address, or domain fragment (case-insensitive) |
| `limit` | int | `10` | Maximum contacts to return; clamped to `[1, 50]` |

The aggregator matches the query against each `message_participants`
row's canonical address or display name (Unicode caseless: both sides
are casefolded, so `STRASSE` matches `Straße`),
then aggregates every row of each matched canonical email (so the same
contact across many threads collapses to one row, and a match on one
display name still reports the contact's other names and threads), and
ranks results by `thread_count` descending with email as the
tiebreaker. `thread_count` counts every thread; `names` lists at most
10 of the display names the contact was written with, each cut at 500
characters, and `name_count` gives the full number. Same-thread
duplicates do not double-count. `organization` is the contact's
exact address domain from the indexer's deterministic entities, or
null for a free-mail provider (see `docs/architecture.md`, Entities);
it is never inferred from display names. `authority_class` is the
contact's source-authority class from the operator's rules file
(`unclassified` when no rule matched), matched against the claimed From
address rather than a verified sender (Spam-folder mail is excluded
from the `authority_class` filters, not from this report), and
`authority_rule` the rule
that set it (`address:<pattern>` or `domain:<pattern>`, null when
unclassified).

### `query_messages`
Enumerate **every** message matching exact criteria, with an exact
total. Unlike `search_emails`, which ranks threads by relevance and
returns the top `limit`, this returns the complete matching set of
individual messages, newest effective time (`occurred_at`, else
`sent_at`) first (claimant ID breaks ties),
and pages through it with a cursor. Use it for "all" and "how many"
questions.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sender` | string | none | From address (see address matching below) |
| `recipient` | string | none | To or Cc address |
| `participant` | string | none | Any role: From, To, or Cc |
| `subject` | string | none | Unicode caseless substring of the message's own subject (casefolded, so `STRASSE` matches `Straße`) |
| `text` | string | none | Every word must appear in the message's indexed body (FTS word match with stemming; words may be in different chunks). Attachment text and stripped quoted replies are not searched; at most 16 words |
| `folder` | string | none | Exact folder name. Without it, messages filed in Trash are left out; pass `"Trash"` to list them ([Trash](#trash-is-left-out-by-default)) |
| `date_from` | string | none | Inclusive ISO 8601 lower bound on the message's effective time (`occurred_at`, else `sent_at`) |
| `date_to` | string | none | Inclusive upper bound; a date-only value covers the whole UTC day |
| `has_attachments` | bool | none | The message's own attachment flag, either way |
| `seen` | bool | none | `true` for messages read in Proton, `false` for unread ([read state](#read-state)) |
| `flagged` | bool | none | `true` for flagged (starred) messages, `false` for the rest |
| `authority_class` | string | none | The source-authority class of the message's From sender (any author, for a multi-author From): `counsel`, `management`, `vendor`, `government`, `personal`, `other`, or `unclassified`; a message in Spam never matches; blank is ignored, any other value is an error |
| `limit` | int | `25` | Messages per page; clamped to `[1, 100]` |
| `cursor` | string | none | `next_cursor` from the previous page of the same query |

All given filters must match; blank filters are ignored. With none,
every indexed message outside Trash is enumerated, so a count from an
unfiltered query is not a mailbox-wide total: Trash takes a separate
`folder="Trash"` query. A `date_from` later than
`date_to` is rejected, as in `search_emails`.

**Address matching.** A value that is a full address
(`jane@example.com`, `Jane <jane@example.com>`) matches by canonical
equality through the `message_participants(address, role)` index.
Anything else (`@example.com`, `Jane`) is a case-insensitive substring
of the address or display name; the display name compares casefolded
(Unicode caseless). The response names the mode used for
each filter.

**Response contract.** The response states the filter interpretation,
`total_matches` (over the whole set), `returned` with the match range,
and `has_more`; when more remain it includes `next_cursor`. Each
message carries its send and delivery dates, folder, read state, attachment flag, subject,
From / To / Cc (at most 10 per role, with a count of the rest),
Message-ID, claimant ID, and Thread ID; the structured output adds In-Reply-To and
up to 10 References. Header values are sender-controlled, so any past
500 characters is cut with a marker. The count, the page, and its participants are read in one
snapshot.

**Paging.** Keyset pagination on `(effective_at, claimant_id)`: messages
indexed while a caller pages never shift or duplicate later pages. A
cursor is bound to the filters it was issued for; a cursor from
another query, or a malformed one, is rejected with an error rather
than silently restarting.

---

## Group 3 — Intelligence

The intelligence tools bound the email text they put in each prompt so
it stays within local-LLM context windows. The bounds differ by tool:

- **`ask_mailbox` and `extract_from_emails`** build the prompt from the
  most relevant indexed chunks (message bodies and attachment text)
  returned by hybrid search. If a thread has no matching chunks, the
  tool falls back to the indexed thread body and finally to the
  200-character ``snippet``. A body chunk of at least 200 characters
  whose text repeats an earlier body chunk of the same thread (ignoring
  `>` quote markers, spacing and case, as with a quoted reply) is
  dropped before it uses any space. Shorter passages ("Approved."),
  attachment chunks and copies in different threads are all kept.
  `extract_from_emails` sends one prompt per thread, with up to ``2000``
  characters of evidence (three chunks at most), each passage under a
  labelled header as in `ask_mailbox`. `ask_mailbox` puts up
  to ``max_threads`` threads in one prompt, with up to six chunks per
  thread and one evidence budget of ``2000`` characters per thread
  retrieved, shared across them: a thread that needs less leaves the
  rest to the others, so a long top-ranked passage is not cut at 2000
  characters while shorter threads below it leave room unused. Within
  a thread, the best-matching chunk is spent first; a chunk cut to fit
  says which characters it kept. When passages are left out or cut,
  `ask_mailbox` adds a fixed-text note after the email blocks giving
  the counts (never any content) and asks the model to say its answer
  may be incomplete.
- **`summarize_thread`** works on a single thread and does not use the
  per-chunk path. Its context is the thread's accumulated indexed body
  (or the ``snippet`` when the body is empty), up to ``8000``
  characters, followed by up to ``4000`` characters of the thread's most
  recent body chunks, which recovers the newest replies that the
  indexer's front-preserved body cap drops. Each section's headers
  count against its cap. It is body-only: attachment text is never
  included.

These character caps are one bound; the model window is the other
(#285). Every intelligence prompt is counted whole: system prompt,
security notice, provenance headers, the question, schema or
conclusion, the coverage note, and (for the tools that can make one)
room for the repair instruction. It must fit in
`INFERENCE_CONTEXT_TOKENS` (default 32768) less `INFERENCE_MAX_TOKENS`
of reply and 64 tokens of chat-template overhead. mcp-server has no
tokenizer, so a prompt is counted at three characters per token, which
over-counts English prose (about four per token) by a third. Text that
tokenizes more densely (CJK scripts, long digit or base64 runs) can
still run over; the provider then cuts the reply short or rejects the
call, as it would without the count. Escaping delimiter tags in the
mail lengthens it after the budget is set, so the budget leaves room
for that as well.

At the default window the character caps above bind first, so prompts
are what they were before the window was counted. For a small local
model, set `INFERENCE_CONTEXT_TOKENS` to its window (for example 8192
or 4096): the evidence budget shrinks to what fits, and the coverage
note reports what was left out or cut. `summarize_thread` keeps at
least a 2:1 share for its body and recent-message sections and gives
room one does not need to the other. `extract_from_emails` adds a
counts-only evidence note when the window cut passages from any
thread, so a `null` answer from such a thread is not read as a genuine
absence. Thread subjects and participants are sender-controlled and can
be long, so when the thread blocks of `ask_mailbox`, `brief_issue` or
`check_conclusion` alone do not fit, lower-ranked threads are left out
whole and the coverage note counts them. When the request itself (a
very long question or schema, with the instructions and the top
thread's headers) cannot fit, the tool returns an error naming the two
settings before any model call. A window that leaves fewer than 1024
prompt tokens after `INFERENCE_MAX_TOKENS` fails startup.

### Prompt-injection hardening

Email is attacker-controlled input: any external sender can attempt to
inject instructions into the user's inbox that an LLM might treat as
commands. The intelligence tools mitigate this two ways:

1. **System-prompt framing.** Every `ask_mailbox`, `summarize_thread`,
   and `extract_from_emails` call (and the experimental `brief_issue`
   and `check_conclusion`) prepends a security notice telling the
   model that email content is untrusted data, must not be followed as
   instructions, and that the model must not reveal the system prompt or
   act on URLs/addresses/phone numbers found inside email bodies.
2. **Explicit delimiters.** Each retrieved thread is wrapped in
   `<untrusted_email>…</untrusted_email>` tags in the user message. The
   user's task (question, summarization instruction, extraction request and schema)
   is placed outside those tags so the model has a clear lexical
   boundary between trusted task and untrusted evidence. Every field
   inside a block (subject, participants, body) is attacker-controlled,
   so any delimiter-shaped text in it (`</untrusted_email>` in any case
   or spacing, opened by `<` or by the fullwidth `＜` or small-form `﹤`
   that NFKC folds onto `<`) is escaped to `&lt;/untrusted_email>` —
   email content cannot close the untrusted region early and smuggle
   text outside it. The tag name is matched by the letters it reads
   as, not its code points (#533): fullwidth, mathematical, circled and
   small-capital letters, ligatures, accents, zero-width characters and
   a closed list of Cyrillic, Greek and Armenian look-alikes for the
   tag's letters (a Cyrillic `е`, a Greek `ο`) do not hide it. This is
   a fixed list for the tag names, not a general confusables table.

These are defense-in-depth measures — they do not guarantee immunity.
Operators running `INFERENCE_MODE=anthropic` should still treat retrieved email
content as potentially hostile.

### `ask_mailbox`
Ask a natural language question about your email.
Retrieves relevant threads and synthesizes an answer.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `question` | string | required | Your question in plain English |
| `from_addr` | string | none | Scope to a specific sender |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `max_threads` | int | `5` | Context threads to use |

`max_threads` is clamped to `[1, 10]` at the tool boundary so an
inflated caller-supplied value cannot expand into an oversized prompt
that blows past the model's context window.

**Citations (#284).** Each passage in the prompt starts with a header
line holding a server-assigned evidence label and the passage's own
message: `[E3 | message <claimant ID> | from <sender> | sent
<date> | chunk N chars X-Y]` (attachment passages also name the file
and MIME type; a thread shown by its indexed text, because it had no
matching chunks, gets `[E4 | thread text]`). The sender is the
message's first `From` entry and the date its own sent date, not the
thread's latest, so passages from different messages with the same
chunk index stay distinct. Labels are numbered by thread rank, then
passage order, before the prompt budget is spent, so the same
retrieval gives the same labels; a passage left out for budget leaves
its number unused. The headers, like every other mail-derived value,
sit inside the `<untrusted_email>` blocks; the instruction to cite
labels is in the system prompt. A header is at most 512 characters: one
that would be longer is rebuilt with its claimant ID (keeping its `#`
suffix), sender, filename and MIME type each cut to 96 characters, so
a header never crowds its passage out of a thread's share. The sender
is read from the index with its display name and address each cut to
1,000 characters; the structured citation's `sender` is cut at 500.

The model is asked to cite the label of the passage supporting each
statement inline (`[E2]`, `[E1, E3]`), to mark statements the passages
do not support `[unsupported]` (or only partly support `[uncertain]`),
to quote only words copied exactly from the passage a statement cites,
and to open with "Not found in the provided emails" when the passages
do not answer the question. After generation the server checks the
answer against the passages it supplied:

- **Labels.** A label is `E` and any number of digits. One no supplied
  passage has, however many digits it has (`[E10000]`), is an
  `unknown_labels` problem, and an answer that cites nothing (and does
  not open with that phrase) is a `no_citations` problem.
- **Statements.** The answer is cut into statements at line breaks and
  at sentence ends followed by whitespace (a label written after the
  full stop, as in `Moved. [E2]`, belongs to the sentence before it; a
  closing quote, parenthesis or Markdown `*`, `_` or backtick may sit
  between the full stop and the space; a full stop inside a quotation
  does not end a statement). The full-width `。！？` end a statement
  without a following space. A label or `[unsupported]` written inside
  a quotation is quoted text, not a citation or a mark. A statement
  that cites no supplied passage and is not marked `[unsupported]` or
  `[uncertain]` is an `uncited_statements` problem, reported only when
  the answer cites something (otherwise it is `no_citations`).
  Markdown headings (`#` to `######` and a space), lines ending in a
  colon, fragments of fewer than three words (each kana or CJK
  ideograph counts as a word) and every statement of a not-found
  answer are not checked.
- **Quotes.** Text of three or more words, or of over 1,000
  characters, in double quotes (straight or curly, on one line, paired
  left to right) is a quotation. A pair
  whose text starts or ends with a space is the outer side of a nested
  quotation or a stray mark (`27"`): it is listed as `not_checked` and
  its text is not treated as quoted, so nested quotations are never
  verified by their outer fragments. A quotation is searched in the text
  shown to the model for the passages its statement cites (with
  delimiter tags escaped as the prompt escapes them): the comparison
  collapses whitespace, treats curly and straight quote marks alike,
  ignores trailing `.,;:` and lets an ellipsis (`...` or `…`) skip
  text, but is otherwise exact (case included) and matches whole words
  only (`"on Fri"` does not match "on Friday"; kana and CJK ideographs
  need no word boundary). A combining mark (Unicode category M) belongs
  to the character before it, so a quote may not end or start between
  them: `"meet at cafe"` does not match a decomposed `café` (`e` +
  U+0301). Known gap: text is not Unicode-normalized, so a precomposed
  `é` in a quote does not match a decomposed one in the passage, or
  the reverse, and such a quote is `unmatched`. A quote found
  there is `verified`; one found only in another supplied passage is
  `misattributed` (a `misattributed_quotes` problem naming where it was
  found); one found nowhere is `unmatched` (an `unmatched_quotes`
  problem). A quote in a statement that cites nothing is `uncited` and
  is not searched. At most 20 quotes per answer of at most 1,000
  characters each are searched, each in each supplied passage at most
  once; further or longer quotes are `not_checked`. The text compared
  is the indexed text (extracted and normalized by the indexer), not
  the raw message, so a verified quote shows the words are in the
  index, not that they are the exact bytes of the MIME source.

Any problem gets exactly one repair call: the same prompt plus a fixed
corrective instruction after the question, naming the problem kinds
with counts only (the rejected answer is not replayed). Its answer is
checked again and returned with whatever problems remain. An answer
cut off at `INFERENCE_MAX_TOKENS` is not repaired. Only counts are
logged.

Structured output:

| Field | Description |
|---|---|
| `answer` | The model's answer with its inline labels |
| `citations` | One entry per valid cited label, in first-cited order: `label`, `chunk_id`, `claimant_id`, `message_id`, `thread_id`, `sender`, `sent_at`, `occurred_at`, `source` (`body`, `attachment` or `thread`), `attachment_id`, `attachment_filename`, `char_start`, `char_end` (end of the part shown to the model) |
| `statements` | The answer cut into statements: `text`, `labels` (the supplied passages it cites) and `status` (`cited`, `unsupported`, `uncertain`, `uncited`, `invalid` for only unknown labels, or `not_checked`) |
| `quotes` | Each quotation: `text` (cut at 1,000 characters), `statement` (index into `statements`), `status` (`verified`, `misattributed`, `unmatched`, `uncited`, `not_checked`) and `found_in` (labels of the passages it was found in) |
| `citation_problems` | `[]` when the check passed, else entries `{kind, labels, statements, quotes}`, `kind` one of `unknown_labels`, `no_citations`, `uncited_statements`, `unmatched_quotes`, `misattributed_quotes`; `statements` and `quotes` are indexes into those lists, and `labels` holds the unknown labels or, for `misattributed_quotes`, the passages the quotes were found in |
| `repair_attempted` | Whether the one repair call was made |
| `threads` | The threads searched, best match first (the `search_emails` thread shape) |

The prose in `content` is the answer, a `Citations:` list, any
citation-check lines (fixed text with counts and labels, never the
model's words), a `Quote check:` count when the answer quotes, and
the `Sources searched:` list. To audit a
citation, call `get_evidence` with the same question and the
citation's `thread_id`: the cited `chunk_id` is among the first six
returned chunks, attachment-matched passages included. To audit the
whole answer, call `get_evidence` with the same question, filters and
`max_threads`, no `thread_id` and no `limit`: it returns the evidence
this call retrieved, the same chunks in the same order. The prompt budget can
still leave some of those passages out of the prompt (the coverage
note tells the model how many). A `thread` citation
has no chunk; read it with `get_thread`.

The check is about labels, statement coverage and quoted words, not
meaning. A valid label or a verified quote does not show that the
passage supports the statement, and statement splitting is a heuristic:
an abbreviation followed by a space ("Oct. 5") can split a sentence and
flag its first half as uncited. A passage whose text imitates a header (`[E7 | from
...]`) stays inside its untrusted block, and the structured citation
for a label comes from the server's own map, not from text the model
read; a label that exists only in mail text is reported as unknown.

An answer the model stopped writing at `INFERENCE_MAX_TOKENS` is
returned with a closing `[Answer cut off …]` notice rather than as if
complete; `summarize_thread` does the same.

### `summarize_thread`
Summarize a thread in different styles.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID to summarize |
| `style` | string | `brief` | `brief`, `detailed`, `action-items`, `timeline` |

When `thread_id` matches no thread, the tool treats it as a subject
phrase: it runs a hybrid search and summarizes the top candidate whose
subject shares a meaningful word with the phrase, or returns `Thread
not found` when none does. An input containing `@` is never treated
as a phrase, so a missing or mistyped thread ID (a Message-ID such as
`<abc@example.com>`) returns `Thread not found` without calling the
embedder, rather than matching a subject on its domain or local part.
To find a thread by address, or by a match that is only in message
bodies, call `search_emails` first and pass the returned `Thread ID`.

**Citations (#284).** The summary follows the `ask_mailbox` citation
contract. The thread's indexed text is `E1`, under a `[E1 | thread
text]` header: it is the thread's accumulated body and names no single
message. Each recent passage is `E2`, `E3` ... in the order shown
(oldest first), numbered before the budget is spent, under the
labelled header `ask_mailbox` uses, with its own message's claimant
ID, sender and sent date. The model is asked to cite a label after
each statement or list item, to mark what the passages do not support
`[unsupported]` or `[uncertain]`, to quote only words copied exactly,
and to open with "Not found in the provided emails" when the passages
hold nothing for the requested style (no action items, say). The
summary is then checked exactly as an `ask_mailbox` answer is
(labels, statements, quotes), with the same single repair call and no
repair of a summary cut off at `INFERENCE_MAX_TOKENS`.

Every style is checked the same way. The statement splitter already
cuts at line breaks, so each `action-items` bullet and each `timeline`
entry is a statement that must cite or be marked, a list introduction
ending in a colon is not checked, and `brief` and `detailed` are cut
into sentences. The known gap is the same as for any answer: a bullet
of fewer than three words ("Call Bob") is a fragment and is not
checked. A summary of a short thread may quote words found in both the
thread text and a recent passage; it is verified when it cites either.

Structured output: `summary`, `style` (the style used; an unknown
style is summarized as `brief`), `thread` (the `search_emails` thread
shape), and `citations`, `statements`, `quotes`,
`citation_problems` and `repair_attempted` as in
[`ask_mailbox`](#ask_mailbox). The prose in `content` is the summary
under its `Summary (<style>) — <subject>:` heading, then the
`Citations:` list and any citation-check lines (fixed text, counts and
labels). An `E1` citation has source `thread` and no chunk; read it
with `get_thread`. A recent passage's `chunk_id` is a body chunk of
the thread.

### `extract_from_emails`
Extract structured data from emails matching a query. Attachment text
(digital and OCR'd PDFs, images) that ranks for the query is included in
each thread's context, so fields that appear only in an attachment can be
extracted.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | What to search for; also sent to the model as the request, so it can pick which records in a passage are wanted |
| `schema` | dict | required | JSON schema for extraction |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `limit` | int | `20` | Max threads to search |

**Example schema:**
```json
{"vendor": "string", "amount": "number", "due_date": "string"}
```

The extractor accepts either a single JSON object matching the schema
or a JSON array of such objects (useful when a thread contains
multiple invoices, receipts, etc.). Every returned record carries
`_source_thread` (the thread subject), `_date` (the thread's last
message date) and `_evidence` (below), so these three names are
reserved: a schema that declares any of them, as a shorthand key or in
JSON Schema `properties` or `required`, is rejected before any model
call. `limit` is clamped to `[1, 50]`
at the tool boundary. Each retrieved thread drives one LLM call, so
inflated values fan out into that many model calls.

**Schema forms and what is checked.** Each returned record is checked
against the schema's declared fields and basic JSON types; this is a
shape check, not a full JSON Schema validator.

- *Shorthand* (`{"field": "type"}`, as in the example): every field is
  optional, and a field that is present and not `null` must have its
  type.
- *JSON Schema* (a schema with `"type": "object"` or a `properties`
  object): every name in `required` must be present, and each property
  present in the record must have its `type` (one name or a list, so
  `["number", "null"]` allows `null`).

Types are checked only when they are JSON type names: `string`,
`number`, `integer`, `boolean`, `object`, `array`, `null`. A `bool` is
not a `number`, and `3.0` is an `integer`. Any other type value (`"date"`,
a description) is not checked, and neither is anything else a JSON
Schema can say: `enum`, `format`, `pattern`, numeric or length limits,
nested `properties` / `items`, `additionalProperties`, combinators and
`$ref`. Fields the schema does not declare are kept.

A thread whose answer was cut off at `INFERENCE_MAX_TOKENS`, or was not
a JSON object, array of objects, or `null` / `[]`, or held a record that
failed the schema check, is counted as failed, never as having no data.
A failing record is dropped; the thread's other records are kept. An answer the provider stopped with a
content filter or refusal is an error. When any
thread fails, the records come back as the first content item and a
second item says how many of the searched threads could not be
extracted and why; if none were extracted the response says so rather
than "No structured data … found", which is reserved for every thread
answering `null` or `[]`.

**Citations (#284).** Each thread's passages are labelled as in
`ask_mailbox`, numbered across the whole call (the second thread's
first label follows the first thread's last), so one label names one
passage in the output. The model is asked to add to each record an
`_evidence` object mapping every field it filled to the labels of the
passages its value came from (`{"vendor": ["E1"], "amount": ["E2"]}`).
The server then checks each record against the passages of its own
thread only (a label shown only to another thread's prompt is unknown
here):

- **Labels.** For each field with a value (not `null`, `""`, `[]` or
  `{}`; `_source_thread` and `_date` are the server's), the labels in
  its `_evidence` entry (a label or a list of them; `"[E1]"` and
  `"E1"` are the same) are split into valid and unknown. A field is
  `cited` (a valid label), `invalid` (only unknown labels, an
  `unknown_labels` problem) or `uncited` (an `uncited_fields`
  problem). The record's `_evidence` is rewritten to each such field's
  valid labels.
- **Values.** A string value is looked for in its field's cited
  passages with the `ask_mailbox` quote comparison: `verified`, or
  `misattributed` when found only in another passage of the thread (a
  `misattributed_values` problem), or `unmatched`. Extracted values
  are often normalized (a reformatted date or amount, a trimmed name),
  so `unmatched` is reported but is not a problem. A value of a field
  that cites nothing is `uncited` and not searched. At most 20 values
  of at most 1,000 characters are searched per thread, each in each of
  the thread's passages at most once; the rest are `not_checked`, as
  are values that are not strings.

There is no repair call: each thread still drives exactly one model
call, and a record with problems is kept and reported. Only counts are
logged.

Structured output:

| Field | Description |
|---|---|
| `records` | The records, as in the first content item, each with `_source_thread`, `_date` and `_evidence` |
| `citations` | One entry per valid label any record cites, in first-cited order, with the `ask_mailbox` citation fields |
| `fields` | One entry per field with a value: `record` (index into `records`), `field`, `labels`, `status` (`cited`, `uncited`, `invalid`), `value_check` (`verified`, `misattributed`, `unmatched`, `uncited`, `not_checked`) and `found_in` |
| `citation_problems` | `[]` when every field cites a supplied passage, else entries `{record, kind, labels, fields}`, `kind` one of `unknown_labels`, `uncited_fields`, `misattributed_values` |
| `notice` | The incomplete-extraction or evidence note in `content`, or `null` |
| `threads` | The threads searched, best match first |

In `content`, the records stay the first item (pure JSON) and any
incomplete-extraction or evidence note the second; a last item holds
the `Citations:` list and the citation- and value-check lines (fixed
text and counts). As with `ask_mailbox`, the check is about labels and
words, not meaning: a valid label or a verified value does not show
that the passage states that value for that record.

---

## Experimental tools

**EXPERIMENTAL: the output format may change between releases.**
Experimental tools are registered only when `MCP_EXPERIMENTAL_TOOLS=true`
(default `false`; any value other than `true` / `false` / empty, in any
case, fails startup). They also need inference: with
`INFERENCE_MODE=none` they are not registered and the startup log says
so. They are read-only and store nothing.

### `brief_issue`
An ephemeral, cited brief of one issue across the mailbox (PLAN.md
Phase 3 item 3, #291): a chronology, actors' positions, decisions, open
questions and conflicting evidence. Nothing it produces is persisted or
indexed, so a brief can never come back as evidence.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `topic` | string | required | The issue to brief, as the user phrased it |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders; name `"Trash"` to include Trash ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Scope to a specific sender |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `max_threads` | int | `5` | Context threads to use, clamped to `[1, 10]` |

Retrieval and evidence are those of `ask_mailbox`: hybrid search with
evidence chunks, one shared prompt budget, and the same labelled
passage headers (`[E3 | message <claimant ID> | from <sender> | sent
<date> | ...]`) inside the `<untrusted_email>` blocks, with the
coverage note when passages were left out. Only message-level passages
are offered: a retrieved thread with no matching chunks, which
`ask_mailbox` would show by its thread text, has no message, sender or
sent date to cite, so it is left out of the prompt (it still appears in
`threads`). The search asks for three times `max_threads` threads so
that the slot of a thread left out this way (for example one not yet
chunked during indexing) goes to the next thread with a message
passage; at most `max_threads` threads are offered. When no retrieved
thread has a message passage, no model call is made and the brief is
empty with `insufficient_evidence: true`.
The topic and a fixed task line follow the blocks. The system prompt asks for one JSON object of
a fixed shape and says that every entry must cite the labels of the
passages that state it; that the newest message is not authoritative
because it is newest, so a correction, cancellation or supersession is
reported only when a passage states it; that passages which disagree
with none saying which is right go in `conflicts`; and that a topic the
passages do not cover returns empty lists with `insufficient_evidence:
true`.

The reply is cut at 100,000 characters (an oversized reply is not
parsed), unwrapped from a code fence, parsed with `json.loads`, and
validated against the brief shape: every section must be present with
the right types, and a chronology `date` must be a real calendar date
as `YYYY-MM-DD` (ASCII digits) or `null`, so a date such as `2024-2-01`
or `2023-02-29` fails the shape and gets the repair call. The server then sorts `chronology` oldest first by
`date` (a stable sort; undated entries go last in the model's order),
whatever order the model used. Each entry's labels are then checked against the
passages supplied: a label no passage has is `unknown_labels`, an entry
with no label is `no_citations`, and a conflict citing fewer than two
supplied passages is `too_few_labels`. A brief that sets
`insufficient_evidence: true` but has entries in any section is
contradictory and reported once as `insufficient_but_populated`
(`section: "brief"`, `item: 0`); so is the converse, a brief with every
section empty and `insufficient_evidence: false`, as
`empty_but_sufficient`. Words an entry quotes are then checked with
`ask_mailbox`'s quote checker (see [Quotes](#ask_mailbox) above, same
rules and caps, at most 20 quotes per reply): each quotation of three or
more words in an entry's text fields (`actor` and `event`, `actor` and
`position`, `decision`, `question`, `description`) is searched in the
indexed text of the passages that entry cites. A quote found only in
another supplied passage is a `misattributed_quotes` problem of the
entry (its `labels` name where it was found), one found nowhere an
`unmatched_quotes` problem. The model is asked to quote only words
copied exactly, inside escaped double quotes in the JSON. A reply that
is not a brief, or that has any problem, gets exactly one repair call: the same prompt
plus a fixed instruction after the task (the rejected reply is not
replayed). The repaired brief is used when it parses; otherwise the
first one when it parsed; otherwise the raw reply is returned with
`status: "invalid_json"`. A reply cut off at `INFERENCE_MAX_TOKENS` is
not repaired and comes back with `status: "truncated"`; a brief needs
more output than an `ask_mailbox` answer, so raise
`INFERENCE_MAX_TOKENS` (for example to 4096) when that happens. Only
counts are logged.

Structured output:

| Field | Description |
|---|---|
| `experimental` | Always `true` |
| `status` | `ok`, `invalid_json` or `truncated` |
| `brief` | When `ok`: `chronology` (`date`, `date_source`: `sent` / `mentioned` / `unknown`, `actor`, `event`, `labels`; sorted oldest first, undated last), `positions` (`actor`, `position`, `labels`), `decisions` (`decision`, `labels`), `open_questions` (`question`, `labels`), `conflicts` (`description`, `labels`), `insufficient_evidence`; else `null` |
| `raw_text` | The unparsed reply when `status` is not `ok`, else `null` |
| `as_of` | Latest sent date (`YYYY-MM-DD`) among the passages supplied; the brief describes the evidence up to then |
| `citations` | Each valid cited label, first-cited order, in the `ask_mailbox` citation shape (claimant, sender, own sent date, chunk) |
| `quotes` | Each quotation in an entry: `text` (cut at 1,000 characters), `status` (`verified`, `misattributed`, `unmatched`, `uncited` when the entry cites no supplied passage, `not_checked`), `found_in` (labels of the passages it was found in), `section` and `item` (the entry holding it) |
| `citation_problems` | Entries `{section, item, kind, labels}`, `kind` one of `unknown_labels`, `no_citations`, `too_few_labels`, `insufficient_but_populated`, `empty_but_sufficient` (these two with `section: "brief"`), `unmatched_quotes`, `misattributed_quotes` (`labels`: where the quotes were found); `[]` when every check passed |
| `repair_attempted` | Whether the one repair call was made |
| `threads` | The threads searched, best match first: the top `max_threads`, and further down to the last thread whose passages were offered |

The prose in `content` opens with an EXPERIMENTAL notice and the
"Evidence as of" date, then the brief's sections, the `Citations:`
list, any citation-check lines, a `Quote check:` count when the brief
quotes, and `Sources searched:`.

Limits: the check is about labels and quoted words only. A valid label
or a verified quote does not prove the passage supports the entry
(semantic support needs a model judge and is not built), and an entry's
`actor` and `date` are the model's reading. The
date in a passage header the model sees is the message's own sent
date; the delivery date (`occurred_at`, which date filters use when
known) is returned on each citation and shown in the `Citations:`
list, but not in the model's passage headers. `as_of` is computed by
the server from the passages' sent dates, not by the model.

### `check_conclusion`
Checks a caller-supplied conclusion against the mailbox (PLAN.md Phase
5 item 2): finds passages that support, contradict, qualify or
supersede it. A query-time tool: nothing it produces is persisted or
indexed, so a finding can never come back as evidence, and every
finding is returned with the source passages it rests on.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `conclusion` | string | required | The statement to check, 1–2000 characters (empty or longer is an error before any provider call) |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders; name `"Trash"` to include Trash ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Scope to a specific sender |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `max_threads` | int | `5` | Context threads to use, clamped to `[1, 10]` |

Retrieval, evidence labels, the prompt budget and the coverage note are
those of `brief_issue`, with the conclusion as the search query; as
there, only message passages are offered (a thread with no matching
chunks is listed in `threads` but its thread text is not shown, since
it has no message, sender or date to quote), and with none the model is
not called. The conclusion is caller text, so it is framed rather than trusted: it goes
after the `<untrusted_email>` blocks inside its own
`<conclusion>…</conclusion>` block (any `conclusion` or
`untrusted_email` tag inside it is escaped, so it cannot close its block
or open a mail block), followed by a fixed task line, and never enters
the system prompt. The system prompt says the conclusion is a claim to
test, not instructions or evidence; asks for one JSON object
(`verdict_summary`, `findings` of `{relation, explanation, labels}`, at
most 20, and `insufficient_evidence`); defines the four relations; and
says the newest message is not authoritative because it is newest, so
`supersedes` is used only when a passage states the change.

The reply is capped, parsed and validated as for `brief_issue`. Each
finding's labels are rewritten to the canonical `E<n>` form (`"[E1]"`
becomes `E1`, repeats dropped) so they join to its `sources[].label`,
and the finding is checked: a relation other than the four is
`invalid_relation`, a label no supplied passage has is
`unknown_labels`, and a finding with no label is `no_citations`. The
check as a whole (problem `item` null) must agree with its abstention
flag: `insufficient_evidence: true` with findings is
`insufficient_but_populated`, and no findings with
`insufficient_evidence: false` is `no_findings_but_sufficient`. Any
failure gets exactly one repair call with fixed text, as does a quote
problem (below); a reply that is
still not a check comes back raw with `status: "invalid_json"`, and a
reply cut off at `INFERENCE_MAX_TOKENS` comes back with `status:
"truncated"` and no repair. Only counts are logged.

The server attaches a `sources` entry to each finding for every valid
label it cites: the `ask_mailbox` citation fields (claimant, sender,
own sent date, chunk) plus `excerpt`, the first 300 characters of the
passage text the model was shown, verbatim from the index (longer text
is cut with a marker). The excerpt is the server's, not the model's, so
it is not checked.

Words the model quotes are checked with `ask_mailbox`'s quote checker
(see [Quotes](#ask_mailbox) above, same rules and caps, at most 20
quotes per reply, the verdict first): each quotation of three or more
words in a finding's `explanation` is searched in the indexed text of
the passages that finding cites, and each in `verdict_summary` in the
passages any finding cites (the verdict cites nothing itself and only
summarizes the findings). A quote found only in another supplied
passage is a `misattributed_quotes` problem (its `labels` name where it
was found), one found nowhere an `unmatched_quotes` problem, both with
the finding's `item`, or `item` null for the verdict. Either gets the
one repair call. The model is asked to quote only words copied exactly,
inside escaped double quotes in the JSON.

Structured output:

| Field | Description |
|---|---|
| `experimental` | Always `true` |
| `status` | `ok`, `invalid_json` or `truncated` |
| `verdict_summary` | The model's short overall verdict (cut at 1000 characters); `null` unless `ok` |
| `findings` | `{relation, explanation, labels, sources}`; `[]` unless `ok` |
| `insufficient_evidence` | The model's abstention flag; `null` unless `ok` |
| `raw_text` | The unparsed reply when `status` is not `ok`, else `null` |
| `as_of` | Latest sent date (`YYYY-MM-DD`) among the passages supplied |
| `quotes` | Each quotation: `text` (cut at 1,000 characters), `status` (`verified`, `misattributed`, `unmatched`, `uncited` when nothing it could cite is a supplied passage, `not_checked`), `found_in` (labels of the passages it was found in) and `item` (the finding, or `null` for the verdict summary) |
| `citation_problems` | Entries `{item, kind, labels}` (`item` null for a problem of the check as a whole, or of the verdict summary for the quote kinds); `kind` adds `unmatched_quotes` and `misattributed_quotes` to the kinds above; `[]` when the check passed |
| `repair_attempted` | Whether the one repair call was made |
| `threads` | The threads searched, best match first, as in `brief_issue` |

The prose in `content` opens with an EXPERIMENTAL notice and the
"Evidence as of" date, then the verdict, each finding (relation in
capitals, explanation, labels) followed by its sources' sender, date
(and, for an attachment passage, `attachment <filename>`, cut for
length as in the `Citations:` list) and quoted excerpt, any citation-check lines, a `Quote check:` count when the reply quotes, and `Sources searched:`.

Limits: the check is about labels, relations and quoted words only. A
valid label or a verified quote does not prove the passage says what the
finding claims (semantic support needs a model judge and is not built),
and the
excerpt is the start of the passage, which may not contain the sentence
the finding rests on. Whether a later message really supersedes an
earlier one is the model's reading of a passage that states the change.

---

## Group 4 — System

### `get_mailbox_status`
Reports whether the local index is current and what it holds.
**Call this first** before answering questions about email content.

| Field | Meaning |
|---|---|
| `current` | `true` only when all three hold: mbsync completed a sync within three sync intervals (never less than 5 minutes), the indexer reported within 10 minutes, and no message is pending or retrying. A sync or indexer timestamp more than 2 minutes ahead of the server clock also makes it `false` |
| `not_current_reasons` | One line per failed condition; empty when `current` is `true` |
| `last_sync_at` / `sync_interval_secs` | mbsync's last successful sync from Bridge, and how often it syncs |
| `indexer_last_seen_at` | When the indexer last reported (at most every 30 s with its health heartbeat, including during the initial index) |
| `queue` | `pending` (found, not yet failed), `retrying` (failed at least once, including jobs deferred during an embedder outage; will retry), `dead` (failed permanently and incompletely indexed: missing from search, or found only by keyword, until `make requeue-dead`) |
| `total_threads`, `total_messages`, `oldest_message`, `newest_message` | What the index holds |
| `conflicting_message_ids` | How many Message-IDs more than one indexed file claims (see "Message-ID and claimant ID" above); 0 when none |
| `extra_claimant_files` | Files beyond the first claimant of each conflicting Message-ID (two Message-IDs with 2 and 3 claimants give 3) |

Message-ID conflicts are reported as counts only, with a fixed hint
in the text output (`get_message` on such a Message-ID lists its
claimant IDs; call it with a claimant ID to read one file). Status never
lists the Message-IDs themselves, so it stays free of mailbox content
and small however many conflicts exist. Conflicts do not affect
`current`. The count groups `messages` on `message_id`, which SQLite
answers from the `idx_messages_message` index alone.

Dead messages do not make the index non-current: nothing more happens
to them without an operator, so they are reported rather than waited
on. `current` cannot see mail that reached Proton after the last sync,
or a delivery whose filesystem event the indexer missed (the periodic
Maildir rescan picks that up within `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS`).

mcp-server never talks to Bridge. mbsync writes a stamp at the Maildir
root after each successful sync. The indexer acknowledges a sync only
once every message it delivered is queued, and records it with its own
liveness in the `ingestion_state` table that this tool reads (see
"Index currency" in `docs/architecture.md`).

The same helper powers ``make status`` on the host: the Makefile target
invokes the module-level ``get_mailbox_status`` directly against the
shared SQLite index, so it reports what MCP clients see.

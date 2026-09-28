# MCP Tool Reference

All tools are available inside Claude Desktop once the stack is running.

## Response format

The search, retrieval, and system tools (Groups 1, 2, and 4) publish an
`outputSchema` and return two views of the same result:

- `content` — the readable prose described below, unchanged.
- `structuredContent` — typed JSON matching the tool's `outputSchema`.
  IDs chain from typed fields: `search_emails` → `results[].thread_id` →
  `get_thread` → `messages[].message_id` → `get_message`, and
  `get_evidence` / `search_attachments` carry `attachment_id`. Paging
  state is typed as well (`get_thread.next_offset`,
  `query_messages.next_cursor` / `has_more` / `total_matches`).

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
headers, participant and sender strings) are cut with a marker.
`get_thread` and `query_messages` apply the same cut in their prose, and
`get_thread` also cuts bodies. IDs are never cut, since a shortened ID
would not chain; `get_thread` states the thread ID once rather than on
every message row. `get_message` returns full headers and the full body.

A failure (unknown thread or message, invalid argument, provider or
database error) is an MCP error result (`isError: true`) whose text
states the reason; it carries no structured content. An empty match is
not a failure: it is a normal result with an empty list.

The intelligence tools (Group 3) have no typed output model; their
answer is the prose in `content`.

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
| `folders` | list | all | Scope to specific folders |
| `from_addr` | string | none | Filter by canonical sender address (or domain like `@example.com`); substring fallback when the value can't canonicalize |
| `from_name` | string | none | Filter by sender name; resolved through `find_contact` to a canonical address before applying. Use when the user names a person but not their email. `from_addr` wins if both are given. |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Filter by attachment presence |
| `participant` | string | none | Filter to threads where this person appears in **any** role — From, To, or Cc. Distinct from `from_addr`/`from_name`, which are sender-only. Accepts an address, a domain (`@example.com`), or a name fragment |
| `limit` | int | `10` | Max threads to return |

**When to use which mode:**
- `hybrid` — best for most queries (default)
- `keyword` — exact names, invoice numbers, email addresses
- `semantic` — conceptual queries, topic-based search

An unrecognized `mode` returns an error; it is not silently remapped
to `hybrid`. `limit` is clamped to `[1, 50]` at the tool boundary so
an out-of-range value (e.g. from an LLM-generated tool call) cannot
drive an unbounded query against the index.

**Query handling notes:**
- Keyword queries are tokenized before hitting FTS5. Punctuation,
  colons, and unbalanced quotes are stripped so natural search strings
  (``"Who sent the invoice?"``) run a valid ``MATCH`` instead of
  silently returning no results. Email addresses and hostnames are
  preserved as single tokens.
- If FTS5 still rejects a sanitized query, search falls back to a
  ``LIKE`` scan over subject / body / participants so recall is
  preserved.
- When any filter (folder, sender, date range, attachment flag) is
  applied, search oversamples raw candidates by ``limit * 4`` rather
  than ``limit * 2`` so deeper-ranked matches still qualify after
  filtering.
- Date bounds accept either a full ISO 8601 timestamp or a date-only
  value (``"2024-12-31"``); date-only values are promoted to start/end
  of day in UTC before being pushed into SQL so the filter matches
  the full day the user named.

---

### `get_evidence`
Return the exact indexed passages (evidence chunks) that back a
question — the same chunks `ask_mailbox` feeds its model, but with
**no LLM synthesis**. Use it to audit or cite an answer, or as the
fast synthesis-free path when only the source text is needed.

Each chunk carries its parent thread + Message-ID, the source
(message body, or an attachment with filename + MIME type), the
message date, and the passage's character offsets. Attachment-derived
chunks (extracted PDF / OCR / document text) are included — unlike
`get_thread`, which is body-only.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | The question or topic to gather evidence for |
| `thread_id` | string | none | Scope evidence to one thread; omit to search the whole mailbox. Rejected in combination with `folders`, `from_addr`, `date_from`, `date_to` or `has_attachments`, which select threads |
| `folders` | list | all | Scope to specific folders |
| `from_addr` | string | none | Filter by sender address or domain |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Restrict to threads with attachments |
| `limit` | int | `12` | Max evidence chunks to return; clamped to `[1, 50]` |
| `include_scores` | bool | `false` | Annotate each thread with the retrieval lanes that matched (`thread_fts` / `chunk_fts` / `attachment_fts` / `thread_vec` / `chunk_vec` / `rerank`) and each chunk with its vector distance |

The mailbox-wide path runs the same hybrid retrieval as `ask_mailbox`
and flattens the per-thread evidence into a flat `limit`-chunk budget.
The `thread_id`-scoped path returns that thread's chunks ranked
against the query; it bypasses RRF fusion, so `include_scores` shows
per-chunk vector distance but no lane provenance.

### `search_attachments`
Locate indexed attachments by filename, MIME type, and extracted
text. Use it for attachment-centric questions ("find the quote PDF
from Acme", "which emails had W-2 attachments?"). With no `query` it
lists attachments by the structured filters alone, newest thread
activity first.

To read the full text inside an attachment, use `ask_mailbox` or
`get_evidence` — this tool locates attachments and previews their
extracted text; it does not return the whole document.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | none | Match against filename, MIME type, and extracted text; omit to list by filter alone |
| `content_type` | string | none | Exact MIME-type filter, e.g. `application/pdf` |
| `from_addr` | string | none | Restrict to attachments on threads sent by this address or domain |
| `date_from` | string | none | ISO 8601 date lower bound (parent thread activity) |
| `date_to` | string | none | ISO 8601 date upper bound |
| `extracted_only` | bool | `false` | Return only attachments whose text extraction succeeded |
| `limit` | int | `20` | Max attachments to return; clamped to `[1, 50]` |

Two FTS lanes run when `query` is set — the attachment filename/MIME
index and the extracted-text index — with filename matches listed
first. Each result reports the parent thread so a follow-up
`get_thread` / `get_evidence` call can round-trip.

---

## Group 2 — Retrieval

### `get_thread`
Read a thread by ID as its messages, oldest first. Each message shows
its own headers (Message-ID, subject, From / To / Cc, send date in UTC,
folder, In-Reply-To, attachment flag; recipient lists past 10 are
summarized as a count) and its indexed body after quoted-reply
stripping. Attachment text is not included. When no message body is
indexed yet, the accumulated thread text (a retrieval artifact that
also carries quoted replies) is shown instead.

Responses are bounded: messages are paged (the response states the
thread's message count and the `offset` for the next page), and each
body is cut at 4,000 characters with a marker stating how many were
left out — `get_message` returns the full body. Header content is
sender-controlled, so it is bounded the same way: at most 10 recipients
per role, 10 thread participants, and 10 References are listed (with a
"+N more" count), and any header value past 500 characters is cut with
a marker — `get_message` returns full headers. The page is read from
one database snapshot.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID from search results |
| `include_attachments_metadata` | bool | `true` | Show the local attachment-availability note when the indexed thread has attachments |
| `offset` | int | `0` | Messages to skip, oldest first |
| `limit` | int | `10` | Messages per page; clamped to `[1, 50]` |

### `get_message`
Return one message's own headers — subject, every From / To / Cc
entry, send date (UTC), folder, In-Reply-To, References, attachment
flag — with its thread ID and subject, and its full indexed body
reconstructed from the per-message chunk store (overlap between
adjacent chunks is removed by character offset). The index keeps no raw
per-message body, so this is the indexed text **after quoted-reply
stripping**; it falls back to thread context when no body chunks are
indexed for the message. Attachment text is not included — use
`get_evidence` for that. The prose ends its header block with the raw
source file's path, size, and SHA-256.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `message_id` | string | required | Message-ID header value |
| `folder` | string | `INBOX` | Retained for interface compatibility; ignored in the default local-only retrieval mode |
| `body_format` | string | `text` | Retained for interface compatibility; ignored in the default local-only retrieval mode |

### `list_threads`
Browse threads in a folder.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `folder` | string | `INBOX` | Folder to list |
| `filter_type` | string | `all` | Currently only `all`; unread/flagged state is not indexed |
| `limit` | int | `20` | Number of threads |
| `offset` | int | `0` | Pagination offset |

### `list_folders`
List all folders and thread counts.

### `find_contact`
Resolve a name / address / domain fragment to canonical email
addresses found in the index. Use this **before** `search_emails`
when the user names a person but not their address (e.g. "emails
from Jane Smith"); pass the chosen result's email to
`search_emails(from_addr=<email>)` for sender-filtered results.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Name, address, or domain fragment (case-insensitive) |
| `limit` | int | `10` | Maximum contacts to return; clamped to `[1, 50]` |

The aggregator matches the query against each `message_participants`
row's canonical address or display name (Unicode case-insensitive),
groups by canonical email (so the same contact across many threads
collapses to one row), and ranks results by `thread_count` descending
with email as the tiebreaker. Every display name the contact was
written with is reported. Same-thread duplicates do not double-count.

### `query_messages`
Enumerate **every** message matching exact criteria, with an exact
total. Unlike `search_emails`, which ranks threads by relevance and
returns the top `limit`, this returns the complete matching set of
individual messages, newest send date first (Message-ID breaks ties),
and pages through it with a cursor. Use it for "all" and "how many"
questions.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sender` | string | none | From address (see address matching below) |
| `recipient` | string | none | To or Cc address |
| `participant` | string | none | Any role: From, To, or Cc |
| `subject` | string | none | Case-insensitive substring of the message's own subject |
| `text` | string | none | Every word must appear in the message's indexed body (FTS word match with stemming; words may be in different chunks). Attachment text and stripped quoted replies are not searched; at most 16 words |
| `folder` | string | none | Exact folder name |
| `date_from` | string | none | Inclusive ISO 8601 lower bound on the send date |
| `date_to` | string | none | Inclusive upper bound; a date-only value covers the whole UTC day |
| `has_attachments` | bool | none | The message's own attachment flag, either way |
| `limit` | int | `25` | Messages per page; clamped to `[1, 100]` |
| `cursor` | string | none | `next_cursor` from the previous page of the same query |

All given filters must match; blank filters are ignored, and with
none every indexed message is enumerated.

**Address matching.** A value that is a full address
(`jane@example.com`, `Jane <jane@example.com>`) matches by canonical
equality through the `message_participants(address, role)` index.
Anything else (`@example.com`, `Jane`) is a case-insensitive substring
of the address or display name. The response names the mode used for
each filter.

**Response contract.** The response states the filter interpretation,
`total_matches` (over the whole set), `returned` with the match range,
and `has_more`; when more remain it includes `next_cursor`. Each
message carries its send date, folder, attachment flag, subject,
From / To / Cc (at most 10 per role, with a count of the rest),
Message-ID, and Thread ID; the structured output adds In-Reply-To and
up to 10 References. Header values are sender-controlled, so any past
500 characters is cut with a marker — `get_message` returns full
headers. The count, the page, and its participants are read in one
snapshot.

**Paging.** Keyset pagination on `(sent_at, message_id)`: messages
indexed while a caller pages never shift or duplicate later pages. A
cursor is bound to the filters it was issued for; a cursor from
another query, or a malformed one, is rejected with an error rather
than silently restarting.

---

## Group 3 — Intelligence

The intelligence tools build LLM prompts from the most relevant indexed
chunks returned by hybrid search, bounded by a fixed per-thread character
budget (``2000`` by default) so multi-thread contexts stay within local-LLM
context windows. If a thread has no matching chunks, the tool falls back to
the indexed thread body and finally to the 200-character ``snippet``.

### Prompt-injection hardening

Email is attacker-controlled input: any external sender can attempt to
inject instructions into the user's inbox that an LLM might treat as
commands. The intelligence tools mitigate this two ways:

1. **System-prompt framing.** Every `ask_mailbox`, `summarize_thread`,
   and `extract_from_emails` call prepends a security notice telling the
   model that email content is untrusted data, must not be followed as
   instructions, and that the model must not reveal the system prompt or
   act on URLs/addresses/phone numbers found inside email bodies.
2. **Explicit delimiters.** Each retrieved thread is wrapped in
   `<untrusted_email>…</untrusted_email>` tags in the user message. The
   user's task (question, summarization instruction, extraction schema)
   is placed outside those tags so the model has a clear lexical
   boundary between trusted task and untrusted evidence. Every field
   inside a block (subject, participants, body) is attacker-controlled,
   so any delimiter-shaped text in it (`</untrusted_email>` in any case
   or spacing) is escaped to `&lt;/untrusted_email>` — email content
   cannot close the untrusted region early and smuggle text outside it.

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
| `folders` | list | all | Scope to specific folders |
| `max_threads` | int | `5` | Context threads to use |

`max_threads` is clamped to `[1, 10]` at the tool boundary so an
inflated caller-supplied value cannot expand into an oversized prompt
that blows past the model's context window.

An answer the model stopped writing at `INFERENCE_MAX_TOKENS` is
returned with a closing `[Answer cut off …]` notice rather than as if
complete; `summarize_thread` does the same.

### `summarize_thread`
Summarize a thread in different styles.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID to summarize |
| `style` | string | `brief` | `brief`, `detailed`, `action-items`, `timeline` |

### `extract_from_emails`
Extract structured data from emails matching a query. Attachment text
(digital and OCR'd PDFs, images) that ranks for the query is included in
each thread's context, so fields that appear only in an attachment can be
extracted.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | What to search for |
| `schema` | dict | required | JSON schema for extraction |
| `folders` | list | all | Scope to specific folders |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `limit` | int | `20` | Max threads to search |

**Example schema:**
```json
{"vendor": "string", "amount": "number", "due_date": "string"}
```

The extractor accepts either a single JSON object matching the schema
or a JSON array of such objects (useful when a thread contains
multiple invoices, receipts, etc.). `limit` is clamped to `[1, 50]`
at the tool boundary. Each retrieved thread drives one LLM call, so
inflated values fan out into that many model calls.

A thread whose answer was cut off at `INFERENCE_MAX_TOKENS` or was not
valid JSON is counted as failed, never as having no data. When any
thread fails, the records come back as the first content item and a
second item says how many of the searched threads could not be
extracted and why; if none were extracted the response says so rather
than "No structured data … found", which is reserved for every thread
answering `null`.

---

## Group 4 — System

### `get_mailbox_status`
Reports whether the local index is current and what it holds.
**Call this first** before answering questions about email content.

| Field | Meaning |
|---|---|
| `current` | `true` only when all three hold: mbsync completed a sync within three sync intervals (never less than 5 minutes), the indexer reported within 10 minutes, and no message is pending or retrying |
| `not_current_reasons` | One line per failed condition; empty when `current` is `true` |
| `last_sync_at` / `sync_interval_secs` | mbsync's last successful sync from Bridge, and how often it syncs |
| `indexer_last_seen_at` | When the indexer last reported (at most every 30 s with its health heartbeat, including during the initial index) |
| `queue` | `pending` (found, not yet failed), `retrying` (failed at least once, including jobs deferred during an embedder outage; will retry), `dead` (failed permanently and incompletely indexed: missing from search, or found only by keyword, until `make requeue-dead`) |
| `total_threads`, `total_messages`, `oldest_message`, `newest_message` | What the index holds |

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

---

## Appendix — Mail-changing actions (design only, not implemented)

The MCP server is read-only. None of the tools below exist: there is no
SMTP path and no draft, move, or flag operation, and no setting enables
one. The decision is recorded under "Not doing" in `PLAN.md`, and the
earlier unregistered implementation is in git history. This interface
sketch is kept only as a starting point if that decision is reopened.

#### `send_email`
Send a new email via ProtonBridge SMTP.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `to` | list | required | Recipient addresses |
| `subject` | string | required | Subject line |
| `body` | string | required | Email body |
| `body_format` | string | `text` | `text` or `html` |
| `cc` | list | none | CC recipients |
| `bcc` | list | none | BCC recipients |
| `reply_to_message_id` | string | none | Sets threading headers |

#### `move_message`
Move a message from one folder to another.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uid` | string | required | IMAP UID of the message |
| `src_folder` | string | required | Source folder name |
| `dst_folder` | string | required | Destination folder name |

#### `mark_read`
Mark one or more messages as read or unread.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uids` | list | required | IMAP UIDs of the messages |
| `folder` | string | `INBOX` | Folder containing the messages |
| `read` | bool | `true` | `true` to mark read, `false` to mark unread |

#### `flag_message`
Flag or unflag a message (starred/important).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uid` | string | required | IMAP UID of the message |
| `folder` | string | `INBOX` | Folder containing the message |
| `flagged` | bool | `true` | `true` to flag, `false` to unflag |

#### `reply_to_thread`
Never implemented even in the earlier code; the sketch was `send_email` with
`reply_to_message_id` set to the Message-ID of the thread's last message.

#### `create_draft`
Never implemented even in the earlier code; it would need IMAP APPEND to the
Drafts folder.

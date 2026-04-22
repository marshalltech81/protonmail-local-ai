# MCP Tool Reference

All tools are available inside Claude Desktop once the stack is running.

## Group 1 — Search

### `search_emails`
Search your mailbox using semantic, keyword, or hybrid search.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural language or keyword query |
| `mode` | string | `hybrid` | `hybrid`, `semantic`, or `keyword` |
| `folders` | list | all | Scope to specific folders |
| `from_addr` | string | none | Filter by sender (partial match) |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Filter by attachment presence |
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

## Group 2 — Retrieval

### `get_thread`
Fetch indexed thread context by ID from the local SQLite index.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID from search results |
| `include_attachments_metadata` | bool | `true` | Show the local attachment-availability note when the indexed thread has attachments |

### `get_message`
Fetch local index context for a single message by Message-ID.

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
| `filter_type` | string | `all` | `all`, `unread`, or `flagged` |
| `limit` | int | `20` | Number of threads |
| `offset` | int | `0` | Pagination offset |

### `list_folders`
List all folders and thread counts.

---

## Group 3 — Intelligence

The intelligence tools build LLM prompts from the indexed thread body
accumulated by the indexer (``threads.body_text``, up to ~8000 characters
per thread), bounded by a fixed per-thread character budget (``2000`` by
default) so multi-thread contexts stay within local-LLM context windows.
If a thread row has no stored body text (legacy rows from an earlier
schema), the 200-character ``snippet`` is used as a fallback.

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

### `summarize_thread`
Summarize a thread in different styles.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `thread_id` | string | required | Thread ID to summarize |
| `style` | string | `brief` | `brief`, `detailed`, `action-items`, `timeline` |

### `extract_from_emails`
Extract structured data from emails matching a query.

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

---

## Group 4 — Actions

Actions are disabled by default because `MCP_READ_ONLY=true` in the standard deployment.
The tools below describe the intended interface, but they are not registered unless
the project explicitly enables a safe write path. The code now fails closed if a
future write path tries to use live Bridge transport without explicit
cert-pinned TLS configuration.

### `send_email`
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

### `move_message`
Move a message from one folder to another.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uid` | string | required | IMAP UID of the message |
| `src_folder` | string | required | Source folder name |
| `dst_folder` | string | required | Destination folder name |

### `mark_read`
Mark one or more messages as read or unread.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uids` | list | required | IMAP UIDs of the messages |
| `folder` | string | `INBOX` | Folder containing the messages |
| `read` | bool | `true` | `true` to mark read, `false` to mark unread |

### `flag_message`
Flag or unflag a message (starred/important).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uid` | string | required | IMAP UID of the message |
| `folder` | string | `INBOX` | Folder containing the message |
| `flagged` | bool | `true` | `true` to flag, `false` to unflag |

### `reply_to_thread`
**Not yet implemented.** Use `send_email` with `reply_to_message_id` set to the
Message-ID of the last message in the thread as a workaround.

### `create_draft`
**Not yet implemented.** Requires IMAP APPEND to the Drafts folder.

---

## Group 5 — System

### `get_index_status`
Returns total threads, messages, date range of indexed email.
**Call this first** before answering questions about email content.

The same helper powers ``make status`` on the host: the Makefile target
invokes the module-level ``get_index_status`` directly against the shared
SQLite index so the reported counts match what MCP queries see.

### `get_sync_status`
Reports local index mode and, when enabled in a future live-Bridge deployment,
Bridge connectivity and sync health.

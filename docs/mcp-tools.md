# MCP Tool Reference

All tools are available inside Claude Desktop once the stack is running.

The server retrieves and reports mail. Linking threads and tracking matters are left to the client; see PLAN.md decision 44.

## Response format

The search, retrieval, and system tools (Groups 1, 2, and 4) publish an
`outputSchema` and return two views of the same result:

- `content` — the readable prose described below, unchanged.
- `structuredContent` — typed JSON matching the tool's `outputSchema`.
  IDs chain from typed fields: `search_emails` → `results[].thread_id` →
  `get_thread` → `messages[].claimant_id` → `get_message`, and
  `get_evidence` / `search_attachments` carry `attachment_id`. Paging
  state is typed as well (`get_thread.next_offset`,
  `query_messages.next_cursor` / `has_more` / `total_matches`, and the
  same fields on `query_attachments`, whose
  `attachments[].attachment_occurrence_id` → `get_attachment`, paged by
  `next_offset`).

**Message-ID and claimant ID.** The sender sets a message's Message-ID,
so two different indexed files can carry the same one (a reused or
forged ID). The index keeps both rather than letting one overwrite the
other, and names each by its claimant ID: the Message-ID plus `#` and
the first sixteen hex digits of the raw file's SHA-256, for example
`<id@x.example>` stored as `id@x.example#3f9a2c1b7d40e865`. It stays the same
across flag renames and folder moves, since those do not change the
file's bytes. Every message row, evidence chunk, and attachment hit
carries both `message_id` (the header value) and `claimant_id`
(a `query_messages` `fields` projection can leave out `message_id`).
`get_message` accepts either; a bare Message-ID that several messages
claim returns an error listing their claimant IDs instead of choosing
one. Both claimants sit in the thread their Message-ID resolves to.

Every message row (`get_thread`, `get_message`, `query_messages`),
evidence chunk (`get_evidence`), and attachment hit
(`search_attachments`) carries `source_file` (unless a `query_messages`
`fields` projection leaves it out): the raw message file the
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
model, and `get_thread` also cuts bodies. IDs and `aggregate_messages`
group values (an address, domain or folder) are never cut, since a
shortened one would not chain; `get_thread` states the thread ID once rather than on
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

## Safety annotations

Every tool, the experimental `brief_issue` and `check_conclusion`
included, declares the same three MCP safety hints plus its own
human-readable `title` (#899):

```json
{"title": "Search Emails", "readOnlyHint": true, "destructiveHint": false, "openWorldHint": false}
```

Each tool is read-only, non-destructive and closed-world. Without these
hints the MCP defaults apply (`readOnlyHint: false`,
`destructiveHint: true`, `openWorldHint: true`), which advertise a
destructive, open-world tool, and OpenAI and Anthropic both require the
hints (see below). ChatGPT refused a `query_messages` call on
2026-10-06 because "we couldn't determine the safety status of the
request", which led to #899.

The hints are necessary but not sufficient. With all three set and
served, ChatGPT refused a call with the same message again on
2026-10-07 (#919), and the server logged nothing for that call: the
tool never ran. Others report the same message for tools
that declare the hints
([OpenAI community](https://community.openai.com/t/chatgpt-app-mcp-tool-calls-blocked-by-openai-safety-checks-before-reaching-mcp-server/1386059)),
and report that it is intermittent, so an identical retry can succeed
([report](https://github.com/totec448-spec/chat-on-steroids/issues/555)).
OpenAI's [MCP guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
says it has "built-in safeguards to help detect and block these
threats", and its
[Apps SDK reference](https://developers.openai.com/apps-sdk/reference)
says the hints "only influence how ChatGPT or Codex frames the tool
call to the user". The refusal is a nondeterministic check on OpenAI's
side that this server cannot control; see
[Troubleshooting](troubleshooting.md#chatgpt-says-a-tool-call-was-blocked-by-openai)
for how to confirm it and what to do.

Tool descriptions are not prefixed with "Read-only." (decided in #899,
kept in #919): the block also hits tools that already declare the
read-only hint, so the prefix is not adopted as a fix (#818 is
trimming descriptions).

A tool's description is its handler docstring before `Args:`, but
FastMCP parses the docstring as Google style and sends only its first
text section: a line of words ending in a colon with an indented block
under it starts an admonition, and everything from that line on is
dropped from what the client receives (#1011; `ask_mailbox` lost its
routing and person guidance this way). Keep such blocks out of tool
docstrings; `mcp-server/tests/test_tool_annotations.py` checks every
tool's description on the wire against its docstring.

- **Read-only.** The MCP specification defines `readOnlyHint` as "If
  true, the tool does not modify its environment." OpenAI's
  [Apps SDK reference](https://developers.openai.com/apps-sdk/reference)
  describes it as a tool that "only retrieves or computes information
  and doesn't create, update, delete, or send data outside the
  conversation." The server opens SQLite `?mode=ro` and has no IMAP
  access, and no tool sends, moves, flags, drafts or deletes mail.
  The server's own diagnostic logging (the [`mcp.timings`
  line](#stage-timings-in-the-server-log), warnings) does not make a
  tool a writer. It is operational telemetry that carries no
  content-bearing arguments or mail (only allowlisted, validated values
  such as modes, limits and ISO dates; see `log_tool_call`), like an
  access log, not an effect of the tool. Neither
  definition mentions logs.
- **Not destructive.** `destructiveHint: false`. No tool deletes or
  overwrites anything.
- **Closed world.** The specification says: "If true, this tool may
  interact with an 'open world' of external entities. If false, the
  tool's domain of interaction is closed." OpenAI describes an
  open-world tool as one that "accesses the public internet or
  open-ended external entities". The owner chose `openWorldHint: false`
  for every tool (2026-10-06), including the tools that send query
  text or mail excerpts to the operator-configured embed, rerank or
  inference provider: `search_emails`, `get_evidence`, `ask_mailbox`,
  `summarize_thread`, `extract_from_emails`, `brief_issue` and
  `check_conclusion`. (`search_attachments` and the retrieval and
  status tools call no provider; their results go only to the calling
  client and its model.) Their domain of interaction is the mailbox. The provider is a fixed
  backend the operator chose, not an open-ended set of entities.
  Deriving the hint from whether a provider is local or remote was
  considered and not chosen. Data egress to a remote provider is
  disclosed elsewhere: by the startup `Privacy:` warnings and by the
  Privacy section of `make status` (see
  [Architecture](architecture.md#privacy-model)).

The tools that send retrieved mail to a remote inference or rerank
provider still advertise `readOnlyHint: true`. This is an owner
decision, accepted as a stated risk on 2026-10-06: nothing in the
mailbox or other state changes, and the provider is the operator's own
chosen backend. As a result, a client that auto-approves read-only
tools may send mail excerpts to that provider without prompting. That
egress is disclosed by the startup `Privacy:` warnings and by
`make status`.

These cover both clients' requirements. OpenAI lists `readOnlyHint`,
`destructiveHint` and `openWorldHint` as required, and all three are
set explicitly. Anthropic's
[connector documentation](https://claude.com/docs/connectors/building/mcp.md)
says "All MCP tools must declare both of these annotations:
`readOnlyHint` … `destructiveHint`". Its
[Software Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy)
says "MCP servers must provide all applicable annotations for their
tools, in particular readOnlyHint, destructiveHint, and title". FastMCP
also uses the annotation `title` as the tool's display `title`.

Annotations are hints, not an authorization control. The specification
says they "are not guaranteed to provide a faithful description of tool
behavior" and that "Clients should never make tool use decisions based
on ToolAnnotations received from untrusted servers". OpenAI says
"servers must still enforce their own authorization logic". Access
control remains the `/mcp` bearer token and the Host/Origin guard, and
read-only behavior remains the `?mode=ro` connection and the absence of
any Bridge access.

Every tool takes its annotations from the shared `read_only(title)`
helper in `mcp-server/src/tools/outputs.py`.
`mcp-server/tests/test_tool_annotations.py` lists every tool through a
real client and fails on a tool it does not know. A tool that would
change mail or state, or reach arbitrary external entities, needs the
owner's approval and its own classification.

## Stage timings in the server log

Every tool, the experimental `brief_issue` and `check_conclusion`
included, logs one line per call at `INFO` on the `mcp.timings` logger,
on success and on failure (`outcome=error`; see
[Troubleshooting](troubleshooting.md#reading-a-tool-calls-log-line)):

```text
tool=search_emails outcome=ok total_ms=41.7 stages_ms={'query_embedding': 22.4, 'thread_fts': 3.1, 'chunk_fts': 2.0, 'attachment_fts': 0.9, 'thread_vec': 4.6, 'chunk_vec': 6.2, 'fusion': 0.8} counts={'thread_fts': 4, 'chunk_fts': 9, 'attachment_fts': 0, 'thread_vec': 100, 'chunk_vec': 812, 'filtered': 57, 'results': 10} config={'rerank': 'none'}
```

- `stages_ms` holds only the stages that ran, so a keyword-mode search
  has no `query_embedding` or vector lanes. Stages: `query_embedding`,
  `contact_lookup` (the `from_name` resolution), the keyword lanes
  `thread_fts` / `chunk_fts` / `attachment_fts`, the vector lanes
  `thread_vec` / `chunk_vec` (each covering every widening step of a
  filtered search), `fusion` (RRF plus post-fusion filters),
  `evidence_fetch`, `evidence_precision` (`get_evidence` with `source`,
  `scope=in_scope` or `dedupe_attachments` on the mailbox-wide path),
  `scope_labels` (the per-message
  [evidence scope](#evidence-scope-in-scope-or-context) lookup of
  `ask_mailbox` and `get_evidence`), `rerank`, `attachment_search` and
  `inference`
  (summed over every completion the call made).
- `counts` holds candidates per lane, `filtered` (after fusion and
  filters), `results`, `evidence_chunks`, `rerank_candidates`,
  `inference_calls` and, on a filtered vector search,
  `thread_vec_expansions` / `chunk_vec_expansions` (re-queries with a
  wider window). The retrieval tools record what they returned (#886):
  `total_matches` and `returned` (`query_messages`, `query_attachments`,
  `aggregate_messages`, whose `returned` counts groups), `groups`
  (`aggregate_messages`: every group, not just the page),
  `incomplete_from_messages` (`aggregate_messages` sender dimensions), `indeterminate`
  (`query_messages`, `query_attachments`, `aggregate_messages`, and
  `search_attachments` with `sender`), and, when `query_messages`',
  `aggregate_messages`' or `query_attachments`' `indeterminate` is not 0, one
  `indeterminate_cause_<cause>` count of 1 per cause its filters can
  have (`sender_ambiguous`, `address_list`, `display_names`, `subject`,
  `body`, `attachment_list`, `size`; [Response
  contract](#query_messages)), `messages`
  (`get_thread`, `get_message`), `attachments` (`get_attachment`, with
  `text_unavailable_<status>` when it returns no text; `none` for no
  extraction recorded), `threads` (`list_threads`),
  `contacts` (`find_contact`) and `folders` (`list_folders`). They run
  no timed stages, so their `stages_ms` and `config` are empty, as are
  `get_mailbox_status`'s `counts`.
- A `degraded_<lane>` count means a lane failed and the call fell back,
  still with `outcome=ok` (#877): `thread_vec` / `chunk_vec` (the
  vector lane errored; that lane contributed nothing), `thread_fts`
  (the FTS query errored and a LIKE scan ran instead), `like_fallback`
  (the LIKE scan errored too), `chunk_fts` / `attachment_fts`,
  `attachment_filename` / `attachment_text` / `attachment_scan`
  (`search_attachments`), `attachment_indeterminate` (the
  `search_attachments` `sender` count; reported unavailable),
  `attachment_match` (no attachment-first
  evidence ordering), `keyword_chunks` (no keyword-matched evidence
  passage; every passage is `selected_by=vector` or `attachment_match`,
  and a rate-limited `Keyword passage lookup failed` WARNING names the
  exception type), `evidence_chunks` (no passages; the thread body
  is used), `recent_chunks` (`summarize_thread` without the latest
  replies), `rerank` (results in RRF order although `config` says
  `rerank=cohere`) and `rerank_subjects` (reranked without subjects).
  See [Troubleshooting](troubleshooting.md#a-tool-call-reports-degraded-retrieval).
- `from_name_matches` (the tools that resolve `from_name`) is the
  number of senders the name matched, up to 10, and
  `from_name_matches_capped` is 1 when more than 10 matched
  ([Resolving `from_name`](#search_emails)). Numbers only: the
  addresses are never logged.
- `keyword_units_unranked` (the tools that select evidence passages)
  is the number of distinct query words past the 16 the keyword
  passage ranking compares; those words still make a passage a keyword
  match but do not rank it, and a rate-limited `Keyword passage
  ranking used the first 16 distinct query words` WARNING says so
  ([#1246](https://github.com/marshalltech81/protonmail-local-ai/issues/1246)).
- `evidence_filtered` is 1 when a `get_evidence` call used a
  [precision control](#precision-controls) or `dedupe_attachments`. With `scope=in_scope`,
  `evidence_context_dropped` counts the `context` passages left out and
  `evidence_threads_scope_emptied` the threads left with none; with
  `source` on the mailbox-wide path, `evidence_threads_source_emptied`
  counts the ranked threads with no passage of that source. With
  `max_chunks_per_thread`, `evidence_chunks_capped` counts the passages
  the cap removed; with `max_chars_per_chunk`,
  `evidence_chunks_truncated` counts the passages it cut. With
  `dedupe_attachments`, `evidence_attachment_copies_collapsed` counts
  the [repeated attachment passages](#collapsing-repeated-attachments)
  collapsed and `evidence_carriers_unlisted` the carriers past the ten
  each `carried_by` lists. A rejected
  control logs `get_evidence rejected invalid <field>` once per field
  per minute, and later repeats as one count line.
- `evidence_capped_threads` (the intelligence tools) counts the threads
  whose passages the fixed per-thread evidence budget (2,000 characters
  per thread) left out or cut. It is a design cap, not a token limit,
  so no setting raises it and it logs no warning; a cut made by the
  model window is the `token limit hit` warning instead.
- `token_limit_output_max_tokens`, `token_limit_context_window`,
  `token_limit_evidence_budget` and
  `token_limit_prompt_over_budget` mark a call that hit that token
  limit; the call also logs a
  [`token limit hit`](troubleshooting.md#the-log-shows-token-limit-hit)
  warning with the counts.
- `config` names the rerank and inference modes, for the tools that use
  them.

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
  Trash message's passages can still appear as evidence, labelled
  `context` in `ask_mailbox` and `get_evidence`
  ([Evidence scope](#evidence-scope-in-scope-or-context)). Passing
  `folders` replaces the default, so `folders=["Trash"]` searches
  Trash and `folders=["INBOX", "Trash"]` both. `search_emails`,
  `ask_mailbox` and `extract_from_emails` resolve `from_name` over the
  same scope, so a sender whose mail is all in Trash is not chosen for a
  default search.
- Message tools leave out the messages filed in Trash: `query_messages`
  and `aggregate_messages` without `folder` (pass `folder="Trash"` to
  list or count them),
  `query_attachments` likewise for the attachments those messages
  carry, and `search_attachments`, which has no folder filter.
- Tools that read one named thread, message or attachment
  (`get_thread`, `get_message`, `get_attachment`, `get_evidence` with
  `thread_id`, `summarize_thread` with a thread ID) and the folder
  browsers (`list_threads`,
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

## Sender attribution

RFC 5322 allows one `From` header. A message that repeats it is
malformed or crafted (two `From` headers that disagree are a known
spoofing shape), and nothing says which one is the author. The indexer
keeps the first `From` header as the message's sender and records, per
message, whether that attribution is safe (#1144). Every message row
(`get_message`, `get_thread`, `query_messages`) and every passage
citation (`ask_mailbox`, `summarize_thread`, `extract_from_emails` and
the experimental tools) carries it as `sender_ambiguous`:

- `false`: one `From` header; the sender is what it says (still the
  claimed address, not a verified one).
- `true`: the message repeats `From`, or the indexer's header scan
  stopped at its field cap (10,000 fields) before a second `From` could
  be seen, so one cannot be ruled out. The index does not record which.
  `from` lists the first header's authors only and may not be the
  author.
- `null`: not assessed yet. Mail indexed before the upgrade that added
  the record stays `null` until its queued reparse reaches it; a
  message whose indexing job is dead-lettered stays `null` until
  `make requeue-dead`.

Only `false` qualifies for the `authority_class` filters: `true` and
`null` never match any class, `unclassified` included, because the
author cannot be told. `query_messages` counts such a message outside
Spam as `indeterminate`, not as a miss
([#1161](https://github.com/marshalltech81/protonmail-local-ai/issues/1161));
a Spam message stays a decided miss. So right after the upgrade the
`authority_class` filters match fewer messages, and none at first,
until the reparse drains (`get_mailbox_status` shows the queue). The `from` rows are kept,
but `query_messages`' `sender` filter cannot decide such a message: it
counts it as `indeterminate`, not as a match, and `participant` does
the same unless the value is in its To or Cc
([#1153](https://github.com/marshalltech81/protonmail-local-ai/issues/1153);
[unknown values](#filter-predicates)). The prose of
`get_message` adds a `Sender:` line, and a `query_messages` row the
words `sender ambiguous` or `sender not yet checked`, unless the value is `false`.
In an intelligence prompt the passage header's sender is followed by
`(unverified: sender attribution unsafe)` or `(unverified: sender not yet
checked)`, and the prose `Citations:` list repeats the note.
`find_contact` is unchanged, and `search_emails` decides its sender
filters on the thread's recorded senders as before (#1154).
`search_attachments` `sender` leaves such a message's attachments out
and counts them as `indeterminate`, and the evidence-scope labels mark
its passages `context` ([in scope or context](#evidence-scope-in-scope-or-context)).

A message is attributed to its own (outer) `From` only. An attached
email's `Subject`, `From`, `To`, `Cc`, `Date` and body are indexed as
that attachment's text
([#922](https://github.com/marshalltech81/protonmail-local-ai/issues/922)),
so the search tools find them, but the inner `From` is a claim inside
a claim: it is never a sender, participant or source-authority input,
and the sender filters do not read it
([#1235](https://github.com/marshalltech81/protonmail-local-ai/issues/1235)).

## Filter predicates

Every message-level filter is one *leaf* of the predicate module
`mcp-server/src/lib/predicates.py`
([#1084](https://github.com/marshalltech81/protonmail-local-ai/issues/1084)):
a named predicate over one message with one value, compiled to SQL in
one place. `query_messages` and the evidence-scope labels of the
intelligence tools ([in scope or context](#evidence-scope-in-scope-or-context))
conjoin their leaves on each message; `search_emails` and the tools
that share its filters decide each leaf on its own against the thread
(below). A `query_messages` cursor is bound to a digest of its leaf
list: passed with other filters it is rejected ("cursor was issued for
different filters"), never read against them.

| Leaf | Value | Built by | Matches a message when |
|---|---|---|---|
| `sender` | address, domain or name fragment | `query_messages` `sender`; `search_attachments` `sender`, on the carrying message; `search_emails` `from_addr` and the tools that share it | its From role carries the value ([address matching](#query_messages)); unknown when its `sender_ambiguous` is not `false` ([Sender attribution](#sender-attribution)), or when it does not and its From addresses are not complete ([unknown values](#filter-predicates)) |
| `recipient` | address, domain or name fragment | `query_messages` `recipient` | its To or Cc role carries the value; unknown when neither does and either role's addresses are not complete |
| `participant` | address, domain or name fragment | `participant` on `query_messages`, `search_emails` and the tools that share it | its To or Cc role carries the value, or its From role does as for `sender` (SQL three-valued OR: unknown when To and Cc do not and `sender` is unknown) |
| `address_is` | role, full address | `sender`, `recipient` and `participant` given a full address; `query_messages` `where` | an address in the role equals the value's canonical address |
| `address_contains` | role, text | `query_messages` `where` | an address in the role contains the text, lowercased; display names are not read |
| `display_name_contains` | role, text | `query_messages` `where` | a display name written for an address in the role contains the text, casefolded, each name on its own; also unknown when its `participant_names_complete` is not `1` and none does |
| `address_or_name_contains` | role, text | `sender`, `recipient` and `participant` given anything else (a domain such as `@example.com`, a name or a fragment); `query_messages` `where` | an address in the role, or a display name written for it, contains the text; also unknown when its `participant_names_complete` is not `1` and none does |
| `domain_is` | role, domain | `query_messages` `where` | an address in the role has exactly that domain, lowercased; a subdomain does not match |
| `subject` | text | `query_messages` `subject` | its own subject contains the text, casefolded; unknown when it does not and the stored subject was cut or not checked |
| `text` | words | `query_messages` `text` | as `body_words`, which it compiles to |
| `body_words` | words | `query_messages` `text` and `where` | every word occurs in its indexed body (FTS, stemmed; at most 16 words); unknown when one does not and the indexed body is not complete |
| `folder` | folder names | `query_messages` `folder`; `search_emails` `folders` | it is filed in one of them |
| `not_in_folders` | folder names | the default scope, when no folder is named | it is filed in none of them (`Trash`; [Trash](#trash-is-left-out-by-default)) |
| `effective_from` | UTC instant | `date_from` | its effective time is at or after the instant; unknown without a delivery date or a parsed send date |
| `effective_to` | UTC instant | `date_to` | its effective time is at or before the instant; unknown likewise |
| `sent_from` | UTC instant | no tool yet ([#1150](https://github.com/marshalltech81/protonmail-local-ai/issues/1150)) | its send date (`sent_at`) is at or after the instant; unknown without a parsed one |
| `sent_to` | UTC instant | no tool yet (#1150) | its send date is at or before the instant; unknown without a parsed one |
| `occurred_from` | UTC instant | no tool yet (#1150) | its delivery date (`occurred_at`) is at or after the instant; unknown without one |
| `occurred_to` | UTC instant | no tool yet (#1150) | its delivery date is at or before the instant; unknown without one |
| `dated` | clock name | no tool yet (#1150) | it has that clock (a delivery date, or a parsed send date), so it has a place in a page ordered by it; unknown without one |
| `has_attachments` | bool | `has_attachments` | its own attachment flag equals the value; unknown when it stores no attachment and its attachment list is not complete |
| `seen` | bool | `query_messages` `seen` | its read flag equals the value |
| `flagged` | bool | `query_messages` `flagged` | its flagged flag equals the value |
| `replied` | bool | `query_messages` `replied` | its answered flag (the Maildir `R` flag) equals the value |
| `size_min` | bytes | `query_messages` `size_min` | its local file size is at least the value; unknown without a stored size |
| `size_max` | bytes | `query_messages` `size_max` | its local file size is at most the value; unknown without a stored size |
| `authority_class` | class name | `authority_class` | its From sender carries the class, outside Spam, and its `sender_ambiguous` is `false`; unknown outside Spam when its `sender_ambiguous` is not `false` ([Sender attribution](#sender-attribution)), or when no stored From address carries the class and its From addresses are not complete |

**Explicit address leaves
([#1088](https://github.com/marshalltech81/protonmail-local-ai/issues/1088)).**
The five address leaves each take a role: `from`, `to`, `cc` or
`visible_recipient` (To or Cc), plus the internal From, To or Cc set
that `participant` uses; the Bcc-inclusive roles arrive with Bcc
([#1090](https://github.com/marshalltech81/protonmail-local-ai/issues/1090)).
In every role, finding nothing is false only when the role's addresses
are complete, and the From role decides only when `sender_ambiguous`
is `false`, as for `sender`. `sender` (role `from`), `recipient`
(`visible_recipient`) and `participant` compile to `address_is` or
`address_or_name_contains` by the value's shape, and `text` to
`body_words`, with the same SQL as before, so their results do not
change. `query_messages` takes the explicit leaves through its
[`where` parameter](#where-explicit-leaves).

`query_attachments` builds the same leaves as `query_messages` for its
`sender`, `recipient`, `participant`, `folder` and date filters and
decides them on the message carrying each attachment
([#796](https://github.com/marshalltech81/protonmail-local-ai/issues/796)).

**Unknown values and the `indeterminate` count
([#1085](https://github.com/marshalltech81/protonmail-local-ai/issues/1085)).**
A leaf over a field the index can hold as NULL (`occurred_at` for a
message without a parseable delivery date, `size_bytes` for one whose
file size was not recorded) is neither true nor false of such a
message. A date bound reads only a date the message carries
([#1080](https://github.com/marshalltech81/protonmail-local-ai/issues/1080)):
under `date_from` / `date_to` a message with no delivery date and no
parsed send date (a missing or unparseable `Date:` header) is unknown,
whatever time it was first indexed, and so is one without a delivery
date whose send date is not yet checked (`sent_at_status` null: mail
indexed before the upgrade, until its reparse). So is `sender` (and the From side of `participant`) of a
message whose `sender_ambiguous` is `true` or `null`, whether or not
its stored From carries the value, since its author cannot be told
([#1153](https://github.com/marshalltech81/protonmail-local-ai/issues/1153)),
and `authority_class` of such a message outside Spam
([#1161](https://github.com/marshalltech81/protonmail-local-ai/issues/1161)).
A `sender`, `recipient` or `participant` value matched as a substring
(a name or fragment, not a full address) that matches none of a
message's stored addresses and display names is unknown, not false,
when its `participant_names_complete` is not `1`: `0` when the
indexer's per-message name budget dropped a name, `null` for mail not
reparsed since the upgrade that added it
([#1140](https://github.com/marshalltech81/protonmail-local-ai/issues/1140)).
A match on a stored address or name still decides it, and a full
address never reads the flag. The leaves conjoin with SQL's three-valued AND: a message is
a match when every leaf is true, rejected when any leaf is false, and
otherwise *indeterminate*: left out of the matches and of
`total_matches`, and counted in the response's `indeterminate` field,
which the prose states whenever it is not 0 (an empty first page then
ends "No messages are known to match." rather than "No messages
match."). `total_matches` is then not the complete count: report
`indeterminate` with it. The count is one extra `COUNT(*)` over the
same predicate, read in the same snapshot, and runs only when a leaf
that can be unknown is present; a query of decided leaves only (the
default) costs nothing more and reports 0.

**Stored content that may be incomplete
([#1086](https://github.com/marshalltech81/protonmail-local-ai/issues/1086)).**
The indexer records per message whether the content each filter reads
is complete: the subject (cut at 2,000 characters), the From, To and Cc
addresses (each role on its own: a header over a parse limit, an entry
that yields no storable address, or, for all three, a header scan cut
short; a repeated `From` leaves its later headers unread), the
attachment list (a parse limit that stopped the walk) and the indexed
body (text parts past a parse limit). Each flag is `1` complete, `0`
known loss, or `null` not assessed: mail indexed before the upgrade
that added them until its reparse runs, a dead-lettered message until
`make requeue-dead`, and, for the body only, a message whose body
chunks are not committed yet. A stored match still decides a leaf; a
leaf that finds nothing is false only under `1`, and unknown under `0`
or `null`. So `subject`, `text`, `has_attachments`, `sender`,
`recipient`, `participant` and `authority_class` (outside Spam) can
leave a message indeterminate, inside the gates above (`sender` and
the From side of `participant` and `authority_class` are still unknown
whenever `sender_ambiguous` is not `false`). `has_attachments` is
decided either way by a stored attachment, and an empty list decides
it only when complete. Attachment *text* (`search_attachments`) is not
covered: the indexer records its completeness per attachment
([#1242](https://github.com/marshalltech81/protonmail-local-ai/issues/1242)),
but no filter reads it yet.

**Thread-level evaluation (`search_emails`).** The thread filters are
decided per leaf, each on its own: one message can satisfy the sender
leaf and another the date leaf, and the thread matches, while
`query_messages` with the same filters finds no message. `sender` and
`participant` are matched against the thread's recorded senders and
participants (the display strings on the thread row: each message's
primary author, and everyone on any of its messages) with the same
canonical-equality-or-substring rule as the per-message leaves; the
date bounds against the thread's effective-time span (`date_last` on
or after `date_from`, `date_first` on or before `date_to`);
`has_attachments` against the thread's own flag; `folder` and
`authority_class` by the existence of a message of the thread
satisfying the leaf. These are the semantics from before the module
existed, kept so results and the retrieval baseline do not move.

## Group 1 — Search

### `search_emails`
Search the mailbox and return matching **threads** (conversations), not
individual messages. Each result bundles its messages with subject,
participants, date range, folder, and a short snippet. To read the
contents of a returned thread, follow up with `get_thread` or
`summarize_thread` using the result's `Thread ID`.

For the latest or N most recent messages from a person, use
`query_messages(sender=..., limit=N)`, newest first. A page holds at most
100 messages, so follow `next_cursor` until N are collected. This tool
ranks by relevance, not date. Read a body with `get_message` only when
the answer needs its content; `get_thread` can return messages outside
the requested sender and count.

For outstanding-item questions, the description gives the same
verification guidance as [`query_messages`](#query_messages): look for
completion, corrections, reopening and later guidance across threads
and senders before calling an item open or closed. A message in Sent
supports only that it was transmitted. Only a message that explicitly
acknowledges that transmission supports receipt; a later reply in the
same thread does not, as it may answer something else. Neither
establishes that the action was carried out; nor does a sent request or
delivered advice. Label each finding confirmed, proposed or unverified,
with message references.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Natural language or keyword query |
| `mode` | string | `hybrid` | `hybrid`, `semantic`, or `keyword` |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Filter by canonical sender address (or domain like `@example.com`); substring fallback when the value can't canonicalize. Decided on the thread's recorded senders, so a thread matches when any message's primary author matches, while `query_messages` `sender` checks each message's From role ([Filter predicates](#filter-predicates)) |
| `from_name` | string | none | Filter by sender name; resolved through `find_contact` to a canonical address before applying, matching any display name the address carries in a From header on a thread it primarily sent (the index keeps no author order within one message, so a name written for it as a second author on such a thread also matches). Use when the user names a person but not their email. `from_addr` wins if both are given. |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Filter by attachment presence |
| `participant` | string | none | Filter to threads where this person appears in **any** role — From, To, or Cc. Distinct from `from_addr`/`from_name`, which are sender-only. Accepts an address, a domain (`@example.com`), or a name fragment. Blank means no filter; padding is stripped |
| `limit` | int | `10` | Max threads to return |
| `authority_class` | string | none | Keep threads with a message whose From sender carries this source-authority class: `counsel`, `management`, `vendor`, `government`, `personal`, `other`, or `unclassified`. Assigned by the operator's rules file (`docs/setup.md`); a filter only, never a ranking weight. Spam-folder messages never count, nor do messages whose `sender_ambiguous` is not `false` ([Sender attribution](#sender-attribution)), so a thread matches only through its other messages. Blank is ignored; any other value is an error |

**Resolving `from_name`
([#864](https://github.com/marshalltech81/protonmail-local-ai/issues/864)).**
`search_emails`, `get_evidence`, `ask_mailbox` and `extract_from_emails`
resolve `from_name` the same way: one `find_contact` lookup over
From-line senders in the call's folder scope, and the matching sender
with the most threads becomes the `from_addr` filter. Each reports the
lookup in its structured output: `resolved_from_addr`, the address
filtered by, and `from_name_matches`, the number of distinct sender
addresses the name matched. The lookup ranks at most 10 contacts, so
the count stops at 10 (10 means ten or more; the timing line's
`from_name_matches_capped` says when more matched). Above 1, several
senders share the name, possibly different people, and only the first
was filtered by; pass another sender's address as `from_addr` to
choose it (`extract_from_emails` takes no `from_addr`: name the sender
more fully, or pass the address as `participant`, which matches any
role). Only the top address is reported. `find_contact` can help find
the others, but it ranks every role across every folder, Trash
included, and lists at most `limit` contacts, so recipients or
out-of-scope contacts can crowd out the senders counted here. No match
reports `null` and `0`; a call without `from_name`, or with an
explicit `from_addr`, reports `null` for both. The address is in the
structured output only: the prose does not show it and nothing about
it is logged.

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
- A date-only bound is a UTC day, not the user's local day. A question
  in local time needs an ISO 8601 offset on the bound: "since January 1"
  in New York is `date_from="2026-01-01T00:00:00-05:00"`. A message sent
  at 21:30 New York time on 31 December is stored as 02:30 UTC on 1
  January, so `date_from="2026-01-01"` includes it and the offset bound
  does not. `search_emails`, `search_attachments` and `query_messages`
  echo the instants applied as `date_bounds` (`date_from` / `date_to` in
  UTC, null for a bound not given; the field is null without a date
  filter) and in a `Date bounds (UTC):` line of the prose. The tools do
  not parse natural-language dates.
- A `date_from` later than `date_to` names an empty interval and is
  rejected with an error naming both fields, the same way by every tool
  that takes both bounds. The bounds are compared after UTC
  normalization and date-only promotion, so `date_from` and `date_to`
  set to the same date select that whole day.
- Date bounds apply to each message's effective time: its delivery
  date (`occurred_at`, the date of its topmost `Received:` header, in
  UTC) when known, else its send date (`sent_at`, its `Date:` header in
  UTC). The bound compares against the delivery date whenever there is
  one, so a message sent just before midnight and delivered after it
  falls on the later day, even though its `sent_at` is the earlier one;
  only a message without a delivery date is bounded by its `sent_at`.
  A message with neither is ordered by the time it was first indexed,
  which is not a date it carries: `query_messages`, `aggregate_messages`
  and `query_attachments` count it as `indeterminate` under a bound, and
  the evidence tools label its passages `context`.
  A thread matches when its span, from its messages' earliest to
  latest effective time, overlaps the range, so a thread with messages
  either side of a short range matches it. A thread's span still
  includes the first-indexed time of such a message, and so does
  `search_attachments`' date bound ([#1373](https://github.com/marshalltech81/protonmail-local-ai/issues/1373)). The tools that hand passages
  to a model (`get_evidence`, `ask_mailbox`, `extract_from_emails`,
  `brief_issue`, `check_conclusion`) retrieve threads the same way, and
  any passage of a matching thread may be shown, including passages
  from messages outside the range. Each passage carries its own
  message's `sent_at`, `sent_at_status` and `occurred_at` (a date null
  when unknown), so its dates stay visible; see
  [Message time](architecture.md#message-time).

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
message's send and delivery dates (`sent_at`, `sent_at_status` and
`occurred_at`, the same values and format as that message's headers),
and the passage's
character offsets. With `date_from` / `date_to`, threads are selected
by span as in `search_emails`, and their passages can come from
messages outside the range; check each chunk's `occurred_at` and
`sent_at`. Each chunk's `scope` says whether its own message meets
every sender, participant, date and folder filter (`in_scope`) or not
(`context`), exactly as `ask_mailbox` labels it
([Evidence scope](#evidence-scope-in-scope-or-context)); the prose
ends each chunk line with `in scope` or `context`. The `thread_id`
path takes no filters, so every chunk there is `in_scope`.
Attachment-derived
chunks (extracted PDF / OCR / document text) are included — unlike
`get_thread`, which is body-only.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | The question or topic to gather evidence for |
| `thread_id` | string | none | Scope evidence to one thread; omit to search the whole mailbox. Rejected in combination with `folders`, `from_addr`, `from_name`, `participant`, `date_from`, `date_to`, `has_attachments` or `max_threads`, which select threads |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `from_addr` | string | none | Filter by sender address or domain |
| `date_from` | string | none | ISO 8601 date lower bound |
| `date_to` | string | none | ISO 8601 date upper bound |
| `has_attachments` | bool | none | Restrict to threads with attachments |
| `participant` | string | none | Any role: From, To, or Cc, as in `search_emails` and `ask_mailbox` |
| `from_name` | string | none | Sender name or role, resolved through `find_contact` exactly as `ask_mailbox` resolves it, so an answer scoped with it can be audited (the `find_contact` tool counts every role, so its top match can differ). An unmatched name returns no evidence. `from_addr` wins if both are given |
| `max_threads` | int | none | Rank threads exactly as `ask_mailbox` does with this `max_threads` and return their evidence; clamped to `[1, 10]` like `ask_mailbox`'s. Omit it to rank by `limit` instead |
| `limit` | int | `12`, or `max_threads` × 6 | Max evidence chunks to return (with `max_threads`, a smaller value keeps the first `limit` chunks of the audit set in rank order); clamped to `[1, 60]`, the most `ask_mailbox` can put in one prompt (10 threads × 6 chunks), so the cap never cuts below an answer's evidence set |
| `include_scores` | bool | `false` | Annotate each thread with the retrieval lanes that matched (`thread_fts` / `chunk_fts` / `attachment_fts` / `thread_vec` / `chunk_vec` / `rerank`; `keyword_slot` marks a thread moved up as the best thread keyword hit) and each chunk with its vector distance |
| `source` | string | `any` | `body` or `attachment` keeps only that source's passages, chosen before the per-thread cap ([Precision controls](#precision-controls)) |
| `scope` | string | `any` | `in_scope` leaves out `context` passages and reports how many |
| `max_chunks_per_thread` | int | `6`, or `limit` with `thread_id` | Passages per thread, `1` to `6` |
| `max_chars_per_chunk` | int | `1600` | Characters per passage, `1` to `1600`; a longer passage is cut and flagged `text_truncated` |
| `dedupe_attachments` | bool | `false` | Return an attachment carried by several messages of a thread once per passage, on the earliest carrier, with the others in `carried_by` ([Collapsing repeated attachments](#collapsing-repeated-attachments)) |

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
against the query the way `ask_mailbox` ranks them
([#858](https://github.com/marshalltech81/protonmail-local-ai/issues/858)):

1. the first chunk of an attachment whose filename or MIME type the
   query matches, if one does;
2. the chunk holding the query words that are rarest in that thread
   (nearest the query on a tie), unless that is the first chunk
   ([#1246](https://github.com/marshalltech81/protonmail-local-ai/issues/1246));
3. the rest: the matched attachments' chunks (strongest match first),
   then the thread's other attachment chunks, then body chunks, each
   group by vector distance. With no attachment match, by vector
   distance alone.

Each chunk's `extraction_deferred` is true for an attachment passage
whose attachment (a copy of the same bytes in the same message) the
indexer is waiting to extract again
([#1236](https://github.com/marshalltech81/protonmail-local-ai/issues/1236)):
its text is what was indexed before, kept until the refresh. The prose
adds `; retained indexed text; extraction refresh pending` to its
`Source:` line, and the timing line counts such passages (and listed
carriers) as `evidence_extraction_deferred`.

Each chunk's `selected_by` says why it qualified: `keyword_match` (its
text holds a word of the query; this wins when both apply),
`attachment_match` or `vector`. A word in every chunk of the thread
does not count toward the ranking, and only the first 16 distinct
query words are ranked (later ones still make a chunk a keyword
match). At `limit=6` the result is
the slice `ask_mailbox` gives its model for that thread. This path
bypasses RRF fusion, so `include_scores` shows per-chunk vector
distance but no lane provenance. A `thread_id` whose thread was reaped
fails with `Thread reaped from the index` rather than `Thread not
found` ([Reaped sources](#reaped-sources)).

#### Precision controls

`source`, `scope`, `max_chunks_per_thread` and `max_chars_per_chunk`
narrow the passages returned without changing which threads are
ranked or in what order
([#988](https://github.com/marshalltech81/protonmail-local-ai/issues/988)).
Left out, the output is the same as without them, so an audit of an
`ask_mailbox` answer leaves them unset. Each applies to the chunks
the existing retrieval returns:

- `source` filters each ranked thread's full ranked passage list
  before the six-per-thread cap, so a thread whose top passages are
  attachments still returns its body passages with `source=body`. On
  the mailbox-wide path a ranked thread with no passage of that source
  (a thread with no indexed passages included) is left out, and
  `threads_without_source_passages` (and a line in the prose) counts
  them. On the `thread_id` path, no passage of that source returns
  `No evidence found ... (source=...)`.
- `scope=in_scope` drops `context` passages after labelling, from the
  same full list, before the per-thread cap and the `limit` budget, so
  an in-scope passage ranked below six context passages is still
  found. Each thread reports `context_passages_left_out` (its `context`
  passages, of the chosen source), and the response its total. A thread
  left with no passage stays listed with an empty `chunks` list and the
  line `No in-scope passages: N context passage(s) left out`. The
  `thread_id` path takes no filters, so nothing there is `context`.
- `max_chunks_per_thread` keeps each thread's first passages in rank
  order; `limit` still caps the total. On the `thread_id` path the
  per-thread cap is `limit` when it is left out, as before.
- `max_chars_per_chunk` cuts each passage's `text`, as the 1,600
  default does.

A value out of range is rejected with fixed text naming the field and
its range. A call that used any control adds `evidence_filtered` to its
[timing line](#stage-timings-in-the-server-log).

#### Collapsing repeated attachments

A document attached to several messages of one thread (sent, re-sent,
forwarded) is indexed once per message, so by default each copy of a
passage is returned
([#989](https://github.com/marshalltech81/protonmail-local-ai/issues/989)).
With `dedupe_attachments=true`, attachment passages with the same
`attachment_id` (the content hash), `chunk_index` and text in one
thread are returned once, on the earliest carrying message (by delivery date, else
send date), at the rank of the best-ranked copy. That chunk's
`carried_by` lists the other carrying messages, earliest first, each
with its `claimant_id`, `sent_at`, `occurred_at`, `scope` and its own
`extraction_deferred`, at most 10 of them; `carried_by_count` counts them all. The prose adds an
`Also carried by:` line naming the same ten and `and N more`. A
different document under the same filename has a different content
hash and stays separate, and body passages are untouched. Copies of
one payload whose text differs (chunked by another extractor module,
or a message still on an older extractor version) also stay separate. Every attachment chunk carries `carried_by`
and `carried_by_count` (empty and 0 when no other message has the
passage); body chunks and calls without the flag have neither.

The collapse runs on each thread's full ranked passage list, after
`source` and `scope` and before the per-thread cap and the `limit`
budget, so the freed places go to other passages. Copies are collapsed
only within a thread: the same document in two threads is returned in
each. `attachment_copies_collapsed` (and a line in the prose) counts
the copies folded into the returned passages. Like the precision
controls, the flag adds `evidence_filtered` to the timing line, with
`evidence_attachment_copies_collapsed`. `ask_mailbox` does not
collapse attachments, so leave the flag unset for an audit.

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

Before a no-query coverage scan, disclose the filters and planned maximum
number of attachment previews; these reach the calling model, which may
be remote. Use the smallest sufficient `limit`, remain within the requested
or approved scope and ask before expanding it. There is no exact total.
`from_addr` selects threads, so previews can come from other participants;
include that conversation scope in the disclosure.

`sender` (#1056) keeps only attachments whose carrying message's From
matches, by the `sender` leaf `query_messages` uses
([Filter predicates](#filter-predicates)): a full address matches
exactly, anything else is a case-insensitive substring of the address
or display name. A carrying message the leaf cannot decide (its
`sender_ambiguous` is `true` or `null`, [Sender
attribution](#sender-attribution), or a value it does not match while
its From addresses or, for a name or fragment, its display names are
not all indexed, [unknown values](#filter-predicates)) keeps its
attachments out of the
results; right after the upgrade that added `sender_ambiguous`, that is
all mail indexed before it until the reparse drains. With a non-blank
`sender` the response counts them as `indeterminate`
([#1204](https://github.com/marshalltech81/protonmail-local-ai/issues/1204)):
the indexed attachments the query and every other filter reach, over
every lane and not limited by `limit`, whose carrying message the leaf
leaves undecided, counted as the results would list them (an
attachment both lanes reach counts once). It covers
sender uncertainty among indexed candidates only, not attachments
the query cannot reach or mail not yet indexed. The prose states it on
every call with `sender`, `0` and the empty reply included (an empty
reply with a non-zero count reads `No attachments are known to match.`);
without `sender` the structured field is `null` and the prose omits it.
When the count fails it is `null` and the prose says `indeterminate:
unavailable`, never `0`; the server logs a WARNING (the first per
exception type each minute, then a count) and the call's timing line
carries `degraded_attachment_indeterminate`. The lanes and the
count read one snapshot. It is applied in
each lane's SQL before the lane's limit, so unlike `from_addr` it does
not depend on a candidate window. Each result's `senders` is still its
thread's senders, not the carrying message's From. The response does
not say which match mode applied or how many distinct addresses
matched; `query_messages(sender=..., has_attachments=true)` does.

Check `extraction_status`: any value other than `success` (`failed`,
`unsupported`, `too_large`, `empty`, `deferred` or null) means no
extracted text is available, not absence of relevant content.
`deferred` (#1236) means the indexer will extract the attachment on a
later pass of its message; it is read from the occurrence, so the
result has no `text_snippet`, `extracted_only` leaves it out, and the
timing line counts such results as `attachments_extraction_deferred`. While
any copy of the same bytes in a message is `deferred`, an
extracted-text match on them is not returned through any copy, since
the message's stored text for them may be an older extraction. To assess coverage,
make a separate call without `query`, with the applicable structured
filters and `extracted_only=false`. A text query cannot reveal unextracted
files whose filename and MIME type do not match. There is no pagination beyond
the 50-result cap. Report limited results and unread document text as
coverage limits rather than claiming an exhaustive attachment audit.
For a complete list or an exact count, use
[`query_attachments`](#query_attachments).
With `from_addr`, sender filtering happens after a bounded candidate scan,
so even fewer than 50 results (including zero) can omit matching attachments
([#1196](https://github.com/marshalltech81/protonmail-local-ai/issues/1196)).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | none | Match against filename, MIME type, and extracted text; omit to list by filter alone |
| `content_type` | string | none | Exact MIME-type filter, e.g. `application/pdf`; blank means no filter |
| `from_addr` | string | none | Restrict to attachments on threads sent by this address or domain |
| `sender` | string | none | Restrict to attachments whose carrying message is From this address, domain or name fragment, matched as `query_messages` `sender`; blank means no filter |
| `date_from` | string | none | ISO 8601 date lower bound on the carrying message's effective time (`occurred_at`, else `sent_at`, else the time it was first indexed, [#1373](https://github.com/marshalltech81/protonmail-local-ai/issues/1373)) |
| `date_to` | string | none | ISO 8601 date upper bound. Date-only bounds are UTC days; give an offset for a local-time bound. `date_bounds` echoes the UTC instants applied, as in `search_emails` |
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
also carries quoted replies) is shown instead, headed as context and
not any one message's text, with `indexed_thread_text_scope:
"context"` in the structured output.

Responses are bounded: messages are paged (the response states the
thread's message count and the `offset` for the next page), and each
body is cut at 4,000 characters with a marker stating how many were
left out — `get_message` pages through the full body. Header content is
sender-controlled, so it is bounded the same way: at most 10 recipients
per role, 10 thread participants, and 10 References are listed (with a
"+N more" count), and any header value past 500 characters is cut with
a marker. The page is read from one database snapshot.

To examine all currently indexed messages, follow this tool's `next_offset`
until null. When a message has `body_omitted_chars > 0`, call
`get_message` with its claimant ID and follow that tool's `next_offset`
until null too. Report unread message or body pages, `reaped_messages`
and `reaped_messages_truncated` as coverage limits: paging cannot recover
removed messages. Separate page snapshots also mean concurrent indexing
can change the conversation during the run.

For a potentially exhaustive review, disclose the target thread and planned
content read before the first call, including transfer to the calling model,
which may be remote. This is not a metadata probe: even `limit=1` can return
accumulated thread context when per-message bodies are absent. State that
the initial read can include the whole conversation's indexed context.
After disclosure, use `limit=1` only if the count is not already known, to
learn `total_messages`. Before bulk thread or body paging, disclose how many
messages will be read. Stay within the requested or approved scope and ask
before expanding it. A narrower task may need only one page.

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
For reads within a filtered or exhaustive review, disclose before calling
that this content read may also return bounded parent-thread context when
the message has no indexed body, including
other messages outside the requested sender/date scope. This context
reaches the calling model, which may be remote. If that exceeds the
requested or approved scope, ask before the call: a message ID or body
offset does not prevent the context fallback.

Return one message's own headers — subject, From / To / Cc and
whether its sender is safe to attribute (`sender_ambiguous`,
[Sender attribution](#sender-attribution)), send date
and, when known, delivery date (`occurred_at`) in UTC, folder,
In-Reply-To, References, attachment flag, [read state](#read-state) — with its thread ID and
subject, and one page of its indexed body reconstructed from the
per-message chunk store (overlap between adjacent chunks is removed by
character offset). The index keeps no raw per-message body, so this is
the indexed text **after quoted-reply stripping**; it falls back to
thread context when no body chunks are indexed for the message.
In that case, `body: null` means no indexed body, and
`indexed_thread_text` is conversation context, not this message's text:
the prose heads it `Indexed thread text (context, not this message's
text)` and the structured output sets `indexed_thread_text_scope:
"context"`, the label `ask_mailbox` gives such passages.
Report the gap; do not attribute the context to the message or treat
the missing body as proof that it contained no relevant evidence.
Attachment text is not included — use `get_evidence` for passages, or
list the message's attachments with `query_attachments`
(`claimant_id`) and read one with [`get_attachment`](#get_attachment). The
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

### Pending deletion

Under mirror retention a message deleted in Proton (mbsync sets the
Maildir `T` flag) or whose file went missing is tombstoned by the
indexer and stays indexed until the grace window
(`INDEXER_DELETION_GRACE_DAYS`) passes and the reaper removes it
([Reaped sources](#reaped-sources)). Until then `get_message` and
`query_messages` still list it, and mark it with
`pending_deletion: true` (`false` for every other message). The prose
shows `Pending deletion: yes` in `get_message` and `| pending deletion`
on the `query_messages` row. A tombstone counts only while it is on
the message's current file, so under mirror retention a message
restored upstream reads as live once the indexer sees the restore. The flag is read at query
time and changes no totals or paging: an unfiltered `query_messages` count still includes these
messages, and `query_messages` has no filter on it. Archive mode
records no tombstones, so the flag is `false` there, except for
tombstones left by an earlier mirror-mode run, which archive mode
never reaps: such a message keeps `pending_deletion: true` until it is
restored upstream (the `T` flag cleared), when the indexer clears the
tombstone as it records the rename (live, or at the next startup's
rename sweep) and the message reads as live. A
leftover tombstone on a message that stays trashed is kept. `get_thread`
rows do not carry it.

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
it used in `resolved_from_addr` and how many senders matched in
`from_name_matches` ([Resolving `from_name`](#search_emails)). Resolve a name here first only when
you need a different matching contact than that one (then pass it as
`from_addr`), or for a tool that filters by address alone, such as
`get_evidence` or `search_attachments`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Name, address, or domain fragment (case-insensitive) |
| `limit` | int | `10` | Maximum contacts to return; clamped to `[1, 50]` |

The aggregator matches the query against each `message_participants`
row's canonical address or each display name the message wrote it
with, one name at a time (a message that writes one address under two
names keeps both, #1140; Unicode caseless: both sides are casefolded,
so `STRASSE` matches `Straße`; until the reparse after that upgrade
reaches a message, only its first names are stored, so `names` and
the matches cover those),
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
Enumerate **every** message matching exact criteria, with a total
count. Unlike `search_emails`, which ranks threads by relevance and
returns the top `limit`, this returns every individual message the
filters definitely match, newest effective time (`occurred_at`, else
`sent_at`, else the time it was first indexed) first (claimant ID
breaks ties),
and pages through it with a cursor. Use it for "all" and "how many"
questions.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sender` | string | none | From address (see address matching below) |
| `recipient` | string | none | To or Cc address |
| `participant` | string | none | Any role: From, To, or Cc |
| `subject` | string | none | Unicode caseless substring of the message's own subject (casefolded, so `STRASSE` matches `Straße`); a message whose stored subject was cut or is not checked yet and does not contain it is `indeterminate` |
| `text` | string | none | Every word must appear in the message's indexed body (FTS word match with stemming; words may be in different chunks). Attachment text and stripped quoted replies are not searched; at most 16 words. A message whose indexed body is not complete and lacks a word is `indeterminate` |
| `folder` | string | none | Exact folder name. Without it, messages filed in Trash are left out; pass `"Trash"` to list them ([Trash](#trash-is-left-out-by-default)) |
| `date_from` | string | none | Inclusive ISO 8601 lower bound on the message's effective time (`occurred_at`, else `sent_at`) |
| `date_to` | string | none | Inclusive upper bound; a date-only value covers the whole UTC day. Give an offset for a local-time bound; `date_bounds` echoes the UTC instants applied ([date bounds](#search_emails)) |
| `has_attachments` | bool | none | The message's own attachment flag, either way; a message with no stored attachment whose attachment list is not complete is `indeterminate` either way |
| `seen` | bool | none | `true` for messages read in Proton, `false` for unread ([read state](#read-state)) |
| `flagged` | bool | none | `true` for flagged (starred) messages, `false` for the rest |
| `replied` | bool | none | `true` for messages answered in Proton (the Maildir `R` flag), `false` for the rest |
| `size_min` | int | none | Inclusive lower bound in bytes on the message's local Maildir file size: not IMAP `RFC822.SIZE` (isync writes LF line endings, so a message is about one byte per line smaller than the server's size). A message whose size is not stored is left out. An integer from 0 to 2^63-1 (SQLite's INTEGER range, stated in the schema as `minimum` / `maximum`), checked strictly, so `"100"` or `true` is an error, not a coerced filter, logged through the rate-limited `rejected invalid argument: query_messages.size_min` warning; `size_min` above `size_max` is an error |
| `size_max` | int | none | Inclusive upper bound in bytes, likewise |
| `where` | object | none | Explicit leaves, with `any` groups and `negate`, ANDed with the other filters ([below](#where-explicit-leaves)) |
| `authority_class` | string | none | The source-authority class of the message's From sender (any author, for a multi-author From): `counsel`, `management`, `vendor`, `government`, `personal`, `other`, or `unclassified`; a message in Spam never matches, and any other whose `sender_ambiguous` is not `false`, or whose From addresses are not complete with none of the class stored, is counted as `indeterminate`, not matched ([Sender attribution](#sender-attribution)); blank is ignored, any other value is an error |
| `limit` | int | `25` | Messages per page; clamped to `[1, 100]` |
| `cursor` | string | none | `next_cursor` from the previous page of the same query |
| `fields` | list of strings | none (every field) | Row fields to return; see Field projection below |

All given filters must match; blank filters are ignored. With none,
every indexed message outside Trash is enumerated, so a count from an
unfiltered query is not a mailbox-wide total: Trash takes a separate
`folder="Trash"` query. A `date_from` later than
`date_to` is rejected, as in `search_emails`.

#### `where`: explicit leaves

`where` ([#1088](https://github.com/marshalltech81/protonmail-local-ai/issues/1088))
names [filter predicates](#filter-predicates) directly instead of
through the flat parameters' inferred mode:

```json
{"all": [
  {"leaf": "domain_is", "role": "visible_recipient", "value": "example.com", "id": "dom"},
  {"leaf": "body_words", "value": "budget approved"}
]}
```

- **Leaves:** `address_is`, `address_contains`,
  `display_name_contains`, `address_or_name_contains` and `domain_is`
  take a `role` (`from`, `to`, `cc` or `visible_recipient`);
  `body_words` takes none. `role` is required on the five address
  leaves and refused, `null` included, on `body_words`. The flat
  parameters' names (`sender`, `text`, ...) and the internal
  `visible_participant` role are not accepted. Unknown keys are
  refused.
- **Boolean form** ([#1087](https://github.com/marshalltech81/protonmail-local-ai/issues/1087)):
  an item of `all` is a leaf or an `{"any": [leaf, ...]}` group, and a
  leaf may carry `"negate": true`. Nothing nests below an `any` group,
  so this is a bounded form (an AND of ORs of possibly negated leaves),
  not arbitrary Boolean algebra. From a sender at either of two
  domains, about a budget, not yet confirmed:

  ```json
  {"all": [
    {"any": [
      {"leaf": "domain_is", "role": "from", "value": "example.com"},
      {"leaf": "domain_is", "role": "from", "value": "example.org"}
    ]},
    {"leaf": "body_words", "value": "budget"},
    {"leaf": "body_words", "value": "confirmed", "negate": true}
  ]}
  ```

- **Evaluation:** every item of `all` must hold, together with the flat
  parameters and the default Trash exclusion. Each leaf is three-valued
  (true, false or unknown) as in the [table above](#filter-predicates),
  and so is the expression: `all` is false if any item is false, else
  unknown if any is unknown; `any` is true if any leaf is true, else
  unknown if any is unknown; `negate` swaps true and false and keeps
  unknown, so a leaf that cannot decide never becomes a confident
  "no" (or "yes") by negation. A message the expression leaves unknown
  counts in `indeterminate`, and the prose names its leaves' causes.
  A one-leaf `any` group means its leaf.
- **Values:** stripped; an empty one is refused. `address_is` takes a
  full address (`Jane <Jane@Example.com>` is applied as
  `jane@example.com`); `domain_is` is lowercased with one leading `@`
  dropped, and refused when it then holds `@`, whitespace or an empty
  label; `body_words` follows `text` (at least one word, at most 16).
  A value may hold at most 320 characters (`domain_is` 255,
  `body_words` 1000).
- **Limits:** at most 16 nodes, each leaf and each `any` group
  counting one; neither `all` nor an `any` group may be empty. The schema also caps each `all` and `any` list at 16 items (`maxItems`), so a longer list is refused before its items are read. An `id` is optional, at most
  64 characters, not blank and unique within the call.
- **Rejections:** fixed text naming the leaf's path (`where.all[1]`,
  or `where.all[1].any[0]` inside a group),
  logged only as the rate-limited `rejected invalid argument:
  query_messages.where` warning; argument values are not logged.
- **Cursor:** the digest a cursor carries covers the flat leaves in
  order and the normalized `where` expression, kept apart (so `sender=`
  and `address_is` on `from` are different queries), plus a format
  version (2 since #1087). The expression is taken in canonical form:
  the items of `all` in order, the leaves of each `any` group (with
  their `negate`) in any order, so reordering a group keeps a cursor
  valid; grouping and negation change it. A cursor issued for another
  expression, or under an earlier format, is refused as "issued for
  different filters".
- **`leaf_results`:** one entry per `where` leaf (group members
  included), in request order: its `path`, its `id` (or `null`), its
  `leaf`, its `negate`, and how many of the messages the query does not
  reject (`total_matches` plus `indeterminate`, over the whole query,
  not the page) the leaf's own value, before `negate`, is `true`,
  `false` and `indeterminate` of; the three sum to
  `total_matches + indeterminate`, so a leaf's `indeterminate` shows
  which leaf left messages undecided. A negated leaf's value after
  `negate` is its `false` count as true and its `true` count as false.
  A non-negated address leaf also reports `distinct_addresses` and at
  most 10 `addresses` its own match selects on the messages the query
  returns that the leaf itself is true of (under `any` a returned
  message need not satisfy every member), most matching messages first;
  both are `null` for `body_words` and for a negated leaf, which
  reports counts only. The two populations differ on purpose:
  the counts are diagnostics that include undecided candidates, while
  the addresses are mail content and come from returned messages only.
  `address_matches` still reports the flat `sender`, `recipient` and
  `participant` filters only. The counts take one more statement,
  which evaluates each leaf once per message not rejected, and each
  non-negated address leaf takes one grouped query.

**Address matching.** A value that is a full address
(`jane@example.com`, `Jane <jane@example.com>`) matches by canonical
equality through the `message_participants(address, role)` index.
Anything else (`@example.com`, `Jane`) is a case-insensitive substring
of the address or of a display name; display names compare casefolded
(Unicode caseless). Every distinct name a message wrote the address
with in that role is matched, each on its own, so a value never
matches across two names (#1140). The response names the mode used for
each filter.

**Matched addresses.** For each `sender`, `recipient` or `participant`
filter, `address_matches` reports `distinct_addresses`, the number of
distinct canonical addresses the filter matched in its roles across the
whole result set (not just the page), and `addresses`, at most 10 of
them, most matching messages first, each cut at 500 characters. Above
1, a name or fragment matched several addresses, possibly different
people who share a display name; the text form states the count and
says so, and the addresses themselves are in the structured output
only. An exact-address filter reports 1 (0 when nothing matched)
without an extra query. A substring filter takes one grouped query over
the participant rows it matches within the result set, read in the
same snapshot as `total_matches`; on a synthetic 50,000-message corpus
(175,000 participant rows) that added about 13 ms for a name, and
about 160 ms for a fragment matching every participant row. Results,
`total_matches` and paging are unchanged. Nothing about the addresses
is logged.

When the exact address is unknown, enumerate name-substring matches in the
requested sender/recipient role and folder with `query_messages`, following
the disclosure and paging guidance below. `find_contact` is capped and
ranks across all roles/folders, so it cannot establish the complete candidate
set. Prefer the intended person's exact address once resolved. Report
truncated headers or unresolved identities as limits; ask the user if
identity remains ambiguous rather than combining unrelated namesakes.

**Response contract.** The response states the filter interpretation,
`total_matches` (over the whole set), `returned` with the match range,
and `has_more`; when more remain it includes `next_cursor`. The
structured output always carries `indeterminate`, the number of
messages the filters could neither accept nor reject ([unknown
values](#filter-predicates)); the prose states it whenever it is not
0, naming the causes the given filters can have (`no stored size` for
`size_min` / `size_max`; `sender ambiguous or not yet checked` for
`sender` / `participant` / `authority_class`; `address list incomplete (an over-long or unparseable address), or not yet checked` for
`sender` / `recipient` / `participant` / `authority_class`; `display
names not all indexed (reparse pending, or over the name budget)` for a
`sender`, `recipient` or `participant` given as a name or fragment;
`subject cut to the stored length, or not yet checked` for `subject`;
`body not fully indexed (a parse cap, indexing not finished, or reparse
pending)` for `text`; `attachment list incomplete (a parse cap), or not
yet checked` for `has_attachments`; `no delivery date and no parseable
Date header, or not yet checked` for `date_from` / `date_to`). `total_matches` counts the definite matches:
it is the complete count only when `indeterminate` is 0, so report
`indeterminate` with any count when it is not. A `sender` filter
decides only messages whose `sender_ambiguous` is `false`, and a
`participant` filter decides the others only through To or Cc; an
`authority_class` filter decides the others only when they are in Spam
([Sender attribution](#sender-attribution)). Right after the upgrade
that added `sender_ambiguous`, mail indexed before it is `null` until
its queued reparse runs, so every `sender` filter reports those
matches as `indeterminate` (and `participant` those it finds only in
From, and `authority_class` all of them outside Spam) until the reparse drains (`get_mailbox_status` `queue.reparse`);
a message whose indexing job is dead-lettered stays indeterminate
until `make requeue-dead`. Likewise, right after the upgrade that added
`message_participant_names`
([#1140](https://github.com/marshalltech81/protonmail-local-ai/issues/1140)),
every `sender`, `recipient` or `participant` filter given as a name or
fragment reports each message it does not match as `indeterminate`
until the reparse drains, since that mail's names past the first are
not stored yet. And right after the upgrade that added the per-message
completeness flags (#1086), every `subject`, `text`,
`has_attachments`, address or `authority_class` filter reports each
message it does not match as `indeterminate` until the reparse
reaches it ([stored content](#filter-predicates)). Likewise, right
after the upgrade that made an unknown send date NULL (#1080), a
`date_from` / `date_to` bound reports each message without a delivery
date as `indeterminate` until the reparse reaches it. Each
message carries its send and delivery dates (`sent_at` null when the
`Date:` header is missing or unparseable, never a substitute, with
`sent_at_status`: `parsed`, `missing`, `invalid`, or null, with
`sent_at` null too, before the reparse; the prose says which), folder, read state,
[pending deletion](#pending-deletion), attachment flag, subject,
From / To / Cc (at most 10 per role, with a count of the rest),
Message-ID, claimant ID, and Thread ID; the structured output adds
`sender_ambiguous` ([Sender attribution](#sender-attribution)), In-Reply-To and
up to 10 References. Header values are sender-controlled, so any past
500 characters is cut with a marker. The count, the page, and its participants are read in one
snapshot.

**Field projection.** A corpus-building pass that pages through
hundreds of rows rarely needs every field. `fields` lists the row
fields to return, by their structured-output names (`message_id`,
`subject`, `sent_at`, `sent_at_status`, `occurred_at`, `folder`, `has_attachments`,
`seen`, `flagged`, `replied`, `in_reply_to`, `references`,
`references_count`, `from`, `from_count`, `to`, `to_count`, `cc`,
`cc_count`, `sender_ambiguous`, `source_file`, `pending_deletion`); `claimant_id` and
`thread_id` are always included, so rows stay addressable. A usual
minimal set is `["subject", "sent_at", "from", "has_attachments"]`.
The text form shows only the projected fields it lists (the claimant
and thread IDs always). The envelope (`filters`, `address_matches`,
`date_bounds`, `total_matches`, `returned`, `offset`, `has_more`,
`next_cursor`) is unchanged, and so is the cursor: it is built from the
page's messages before projection, so a projected and an unprojected
page continue each other. An unknown name is an error that names it,
and a list of more than 23 names (one per field; repeats add nothing)
is an error; the log records only that `fields` was rejected and why,
in a warning rate-limited to one per reason per minute with a count of
the repeats. Omitting `fields`
returns every field, as before. Because rows can be projected, the
output schema requires only `claimant_id` and `thread_id` in a row.

**Paging.** Keyset pagination on `(effective_at, claimant_id)`: messages
indexed while a caller pages never shift or duplicate later pages. A
cursor is bound to the filters it was issued for; a cursor from
another query, or a malformed one, is rejected with an error rather
than silently restarting.

To examine every match, follow `next_cursor` until `has_more` is false.
Each page uses a fresh index snapshot, so the matching set can change
between pages, and a message that matches when the run ends can still be
missed: one that arrives ahead of the cursor, or one already in the set
that leaves it while its page is read (moved to another folder, for
example) and comes back afterwards. Counts do not rule this out: every
page's `total_matches`, the final total and the number of rows received
can all agree while a member was never returned ([#1218](https://github.com/marshalltech81/protonmail-local-ai/issues/1218)).
A changed `total_matches` signals churn. Scope coverage to the indexed
results observed during the run, not a point-in-time complete mailbox.

Start with narrow filters and `limit=1` to obtain the count. Before bulk
paging or reading bodies, tell the user the scope and how many messages
will be read: tool results go to the calling model, which may be remote.
Prefer the smallest sufficient sample when it answers the question;
a sample cannot establish an exhaustive content audit.

Subsequent body reads can also return parent-thread context beyond these
message filters. Include that possible context in the pre-read scope
disclosure, and ask before a content call would exceed the requested or
approved scope. Filtering the list does not restrict the context returned
by `get_message` or `get_thread`.

A count of the exact filter criteria needs only `total_matches` and
`indeterminate` (the count is complete only when `indeterminate` is 0;
otherwise report both); reading every page is necessary when
classifying or examining each message.
Exhausting a keyword query does not establish exhaustive coverage of a
topic. Consider alternate wording, read candidate messages, and keep the
counting unit explicit (messages, threads, or distinct bills/items).

**Multi-lane enumeration.** A call takes one AND of filters, so a
broad question ("every message about a committee's finances and
audits") is answered with several exact lanes and their union (#992):

1. Lanes: one `subject` lane per subject term, one `text` lane per body
   word set, one `participant` (or `sender`) lane per person or domain
   involved. Every lane carries the same `folder`, date bounds and
   other predicates the question has, so the union has one scope.
   Count, disclose and page each lane as the paragraphs above describe.
2. Union the rows by `claimant_id`, keeping the lane or lanes that
   found each row.
3. Report every row in one of three buckets, with its lane: relevant,
   dropped, or unresolved. Any reading done to sort them follows the
   disclosure and smallest-sample paragraphs above.

`get_message` and `get_thread` return no attachment text; attachment
passages come only through [`get_evidence`](#get_evidence), which
ranks and caps a whole thread's passages. A missing passage therefore
does not resolve a row, and a row with attachments whose body is not
relevant stays unresolved, never dropped.

The recipe supports this claim: the rows the lanes returned during the
run, within their shared scope, subject to the snapshot caveat above.
It does not show coverage of the topic: a message that uses none of
the lane terms or participants, or mentions the topic only in an
attachment (`text` searches bodies only), is not found, and nothing
shows it is missing (see the keyword-coverage paragraph above and
#776). Report the lanes, their counts, the union size and the three
buckets, not "all messages about X". The address and `body_words`
lanes can also run as one query, with a `where` `any` group
([#1087](#where-explicit-leaves)); subject has no `where` leaf yet.

For outstanding-item questions, look for completion, corrections,
reopening and later guidance across threads and senders before calling
an item open or closed. A message in Sent supports only that it was
transmitted. Only a message that explicitly acknowledges that
transmission supports receipt; a later reply in the same thread does
not, as it may answer something else. Neither establishes that the
action was carried out; nor does a sent request or delivered advice.
Label each finding confirmed, proposed or unverified, with message
references. The `search_emails` description carries the same guidance
([#1237](https://github.com/marshalltech81/protonmail-local-ai/issues/1237)).
State the scope and disclose
unread pages, missing indexed bodies and unavailable attachment text
instead of claiming full coverage.

#### Certifying a paged run: sizing (planned)

No tool certifies a paged run yet. The owner chose a certificate plus a
read-only reconcile tool for it
([#1218](https://github.com/marshalltech81/protonmail-local-ai/issues/1218),
Design A); this section records the measurement that sizes that design
before any protocol code exists. The protocol itself will be documented
here when it ships.

**How it was measured.** `mcp-server/scripts/reconcile_bench.py` builds
a synthetic index (generated values only, never mail) on the schema the
server reads (the tables mirror indexer schema v10, with the v9
`extraction_deferred_at` column; the figures before "Combined worst case"
below were taken on the v8 layout, which lacks it), and runs each step in a fresh child
process, on the server's own read-only connection and with the server's
own predicate compiler (unfiltered: Trash left out, so 95% of the
synthetic messages match). Records are read and serialized with the
server's own `query_messages` and `query_attachments` helpers. Every
message has a 40-word body chunk drawn from 5,000 distinct words.
Timings are plain `perf_counter` medians of three runs, without a
profiler, with the page cache warm; peak RSS is the child's `VmHWM`.
Run inside the mcp-server image (SQLite 3.46.1, Python 3.14.8; figures
below from an 18-core OrbStack VM):

```bash
docker build -t mcp-bench mcp-server
docker run --rm --entrypoint python \
  -v "$PWD/mcp-server/scripts:/app/scripts:ro" mcp-bench \
  scripts/reconcile_bench.py --messages 50000 --identity typical --wal --filtered
```

`--identity` sets the Message-ID width: `typical` (about 50 ASCII
characters), `ascii998` (998 characters, the longest the indexer
accepts, so a 1,015-byte claimant ID) or `utf8x4` (998 four-byte
characters, 4,009 bytes, the UTF-8 upper bound). `--records worst`
fills every character-clipped field of a record past its clip with
four-byte characters (subject, 11 participants per role with name and
address, 11 References entries, In-Reply-To, filename and MIME type).
`--records cardinality` gives each message the most rows a record can
carry: 10,000 participants (the parser's `MAX_MESSAGE_ADDRESSES`) and
`--references` References entries (100,000 here; the parser caps their
length, not their count). `--extracted-chars` stores that many
four-byte characters of extracted text on every extraction row.
Occurrence IDs are always 64 hex characters. `--per-message` sets the
attachment occurrences per message (3 by default), `--extras` the
uploaded hashes the server does not hold, `--filtered` adds the
filtered queries below, and `--request-shapes N` times parsing each
upload shape at N digests.

**Certificate: one read transaction.** `COUNT(*)`, then every matching
identity scanned, sorted by UTF-8 bytes and hashed with a length
prefix. "Stream" lets SQLite order the scan through the primary-key
index; "collect" fetches the identities and sorts them in Python. Both
gave the same digest in every run, and the scanned count always equalled
the `COUNT(*)` from the same transaction.

| Corpus (matching messages / occurrences) | Messages: stream / collect | Occurrences: stream / collect |
|---|---|---|
| 50,000, typical IDs (47,500 / 142,500) | 0.08 s / 0.03 s | 0.67 s / 0.34 s |
| 50,000, 998 ASCII (47,500 / 142,500) | 0.26 s / 0.12 s | 3.7 s / 2.0 s |
| 50,000, 998 four-byte (47,500 / 142,500) | 0.54 s / 0.36 s | 4.5 s / 2.4 s |
| 200,000, typical (190,000 / 570,000) | 0.29 s / 0.13 s | 3.5 s / 1.7 s |
| 50,000, 998 ASCII, 10 per message (47,500 / 475,000) | 0.27 s / 0.13 s | 15 s / 4.1 s |

Stream adds nothing to peak RSS. Collect adds what it holds: for
messages 6 MiB (typical), 49 MiB (998 ASCII) and 185 MiB (998
four-byte); for occurrences 17 MiB at 142,500, 57 MiB at 475,000 and
68 MiB at 570,000. 998-character IDs are longer than an index key
SQLite keeps in its page, so every key spills to an overflow page: the
synthetic database file is 6.1 GB with 998-ASCII IDs and 9.8 GB with
four-byte ones, against 0.33 GB with typical IDs, and the occurrence
scan, which looks up each occurrence's message, slows five- to
sevenfold. On the 11 GB index the same `COUNT(*)` took 2.4 s in one
phase and 5.9 s in another, so these figures depend on the operating
system's page cache.

**Filtered queries.** A certificate evaluates the query's own
predicate, so its cost follows that predicate, not the size of the
matching set. Each filter was timed as one production page of the same
query (`limit=1`, with its counts) and as a certificate by each scan
method:

| Filter (50,000 messages, typical IDs) | Matches | Page | Certificate: stream / collect |
|---|---|---|---|
| `participant` substring matching nothing | 0 | 0.54 s | 0.28 s / 0.25 s |
| `participant` substring of an address | 1,100 | 0.53 s | 0.29 s / 0.26 s |
| `sender` exact address | 100 | 0.04 s | 0.06 s / 0.03 s |
| `subject` substring | 10 | 0.06 s | 0.07 s / 0.03 s |
| `text`, a word in one body / in every fiftieth | 1 / 1,000 | 0.06 s / 0.05 s | 0.05 s / 0.07 s; 0.02 s / 0.04 s |
| `authority_class` | 9,500 | 0.09 s | 0.11 s / 0.08 s |
| date range (one year) | 2,500 | 0.04 s | 0.05 s / 0.02 s |
| Occurrences, `participant` substring matching nothing | 0 | 0.43 s | 0.69 s / 0.25 s |
| Occurrences, date range (one year) | 7,500 | 0.06 s | 0.44 s / 0.04 s |

With 998-ASCII IDs the participant substring took 8.7 s for a message
page and 4.3 s for its streamed certificate, and 6.0 s for an
occurrence page and 3.9 s for its collected certificate (7.4 s
streamed). At 200,000 messages: 2.3 s and 1.2 s for messages, 1.7 s
and 1.0 s for occurrences (3.2 s streamed), and an occurrence date
range 0.30 s and 0.17 s (2.3 s streamed). Across every filter and
corpus, with the scan method proposed below (stream for messages,
collect for occurrences), a certificate cost at most 1.6 times one page
of the same message query and 0.9 times one occurrence page; streaming
the occurrences instead cost up to 7.6 times a page. The set caps below
bound the scan, hash and response, not this predicate cost, which every
page of the same query already pays.

**Upload shapes.** One SHA-256 per identity the client received.
Parsing 600,000 digests: a packed upload (one JSON string of base64
over the concatenated 32-byte digests, 25.6 MB) took 0.05 s and peaked
at 136 MiB; an array of 600,000 hex strings (40.2 MB) took 0.04 s and
169 MiB. An array is unsafe under a byte cap: the same 40.2 MB holds
8,039,999 two-character strings, which `json.loads` builds before any
length check, taking 0.16 s and 530 MiB. A packed upload that holds
only the `hashes` string is one element whatever its size, and its
digest count is its decoded length over 32, known before any
per-digest object exists. That holds only while nothing else is in
the JSON body: an envelope of the same 25.6 MB with an empty `hashes`
and an ignored member of 5,119,998 two-character strings took 0.13 s
and 347 MiB, because `json.loads` builds the whole body before any
member is selected. A byte cap alone therefore does not bound a packed
request; its structure must be bounded before it is parsed (for
example by requiring the body to be exactly the fixed prefix, the
base64 and the fixed suffix, or by sending the base64 as the body), an
encoding choice for PR1 that this measurement does not make.

**Reconcile round: one read transaction.** Parse the packed upload,
then COUNT, scan, hash each identity, diff against the upload, and read
the first K missing records in the same transaction. The upload left
out 5,000 members and added 100 hashes the server does not hold.

| Corpus | Messages, K = 100 to 5,000: stream / collect | Occurrences: stream / collect |
|---|---|---|
| 50,000, typical | 0.09–0.24 s / 0.05–0.18 s | 0.75–0.84 s / 0.40–0.48 s |
| 50,000, 998 ASCII | 0.24–0.57 s / 0.16–0.48 s | 3.1–4.2 s / 1.9–2.2 s |
| 50,000, 998 four-byte | 0.59–1.14 s / 0.49–1.04 s | 4.4–4.6 s / 2.5–2.7 s |
| 200,000, typical | 0.40–0.54 s / 0.22–0.35 s | 3.9–4.4 s / 2.0–2.4 s |
| 50,000, 998 ASCII, 475,000 occurrences | 0.26–0.65 s / 0.16–0.50 s | 9.5–10.4 s / 4.0–4.5 s |

On typical records the scan dominates; worst-case records cost more
(below). The full collect round on occurrences peaked at 290 MiB at
570,000 (231 MiB streamed) and 301 MiB at 475,000 (251 MiB); on
messages with four-byte IDs at 472 MiB (449 MiB streamed).

**Response bytes per record** (UTF-8 JSON, as the query tools return
it):

| Record | Message | Occurrence |
|---|---|---|
| Typical fields, typical IDs | 1.2 KB | 1.0 KB |
| Typical fields, 998 ASCII IDs | 4.9 KB | 3.9 KB |
| Typical fields, 998 four-byte IDs | 16.9 KB | 12.8 KB |
| Four-byte fields past their clips, 998 ASCII IDs | 149.9 KB | 8.1 KB |
| Four-byte fields past their clips, 998 four-byte IDs | 158.9 KB | 17.1 KB |

A response grows by K times the record size. On the largest message
records, 100 records are 15.9 MB (read in 0.09 s, peak RSS 167 MiB),
500 are 79.5 MB (482 MiB), 1,000 are 159 MB (0.8 s, 746 MiB) and
5,000 are 795 MB (5.4 s in this run, 28 s in an earlier one; 2.8 GiB).
On the largest occurrence records, 1,000 are 17.1 MB (153 MiB
streamed, 156 MiB collected), 2,000 are 34.1 MB (207 / 210 MiB) and
5,000 are 85.3 MB (371 / 373 MiB).

**Full Maildir paths.** The benchmark's worst-case records carry the
unclipped folder and source path (nested folders of 252 bytes to about
3.5 KB), which the cap-sized run above predates: with them a worst-case
message record is 166.0 KB (158.9 KB before) and an occurrence record
24.1 KB (17.1 KB before), 7.1 KB more each, so 1,000 records are 7.1 MB
more than the cap-sized responses above. That run was not repeated.

**Rows behind a record.** The server's record readers load every
participant row and every References entry of a message before the
output clips each list to 10
([#1377](https://github.com/marshalltech81/protonmail-local-ai/issues/1377)).
With 10,000 participants and 100,000 References entries per message, a
round read 1 record in 0.013 s, 10 in 0.17 s and 100 in 1.8 s, with
peak RSS 98 MiB, 216 MiB and 1.4 GiB, while the response stayed under
200 KB. A References header near the parser's 50 MB file limit holds
far more entries than that. An occurrence record selects
`extracted_at` and `ocr_pages_skipped`, stored after `extracted_text`,
so reaching them walks the text's overflow pages
([#1381](https://github.com/marshalltech81/protonmail-local-ai/issues/1381)):
with 2,000,000 four-byte characters (the indexer's default cap, 8 MB)
on every payload, 100 records took 0.10 s, and with 10,000,000 (the
extractors' cap when the setting is off, 40 MB) 50 records took 0.22 s,
at flat RSS and with the cache warm (0.15 to 0.48 s in an earlier run).
K therefore bounds records, not the work of reading them, until #1377
and #1381 bound each record's read.

**Extras.** Hashes the client holds that are not in the snapshot come
back as hex, 67 bytes each. Packed uploads of 642,500 and 737,500
digests (27.4 MB and 31.5 MB; 600,000 of them extras) took 0.09 to
0.12 s to parse, returned 40.3 to 41.4 MB and peaked at 354 to
400 MiB.

**Combined worst case, without planner statistics.** The figures above
were taken on a synthetic index that had been `ANALYZE`d; neither the
indexer nor the server runs `ANALYZE` or `PRAGMA optimize`, so a
deployed index has no `sqlite_stat1` and the benchmark no longer builds
one (`--records mixed` and `--upload-total` build the combined shape).
On 60,000 messages with four-byte IDs (4,009 bytes) and 180,000
occurrences, one message in fifty carrying worst-case records, an
upload filled to 60,000 and 180,000 digests with extras, and the
missing members all worst-case records: a 100-record message round took
1.1 to 1.2 s and peaked at 181 MiB streamed (537 MiB collected); 1,000
message records (159 MB) took 1.8 to 5.2 s at 757 MiB; a 1,000-record
occurrence round (17.7 MB) took 10.7 s streamed (197 MiB) and 6.3 s
collected (216 MiB). That run is not a guide to larger sets, so it was repeated at the
caps
([#1395](https://github.com/marshalltech81/protonmail-local-ai/issues/1395)),
with the largest accepted upload: 200,000 messages and 600,000
occurrences built (a 44.9 GB database, 2,208 s to build), 190,000
messages and 570,000 occurrences matching (5 % are in Trash), four-byte
IDs (4,009 bytes), one message in fifty with worst-case records, and an
upload of exactly 200,000 and 600,000 digests that holds no member (all
200,000 and 600,000 are extras the server does not hold), so every
member is missing and each round returns K worst-case records
(`--all-extras`). Two runs of each step, the scan method that runs first
alternating; the figure is the median of the two, which for two is
their mean, so a cold and a warm run are blended.

| Cap-sized | Certificate (count and scan), stream / collect | Round, K = 100, stream / collect | Round, K = 1,000, stream / collect |
|---|---|---|---|
| Messages | 33.5 s, 84 MiB / 12.4 s, 823 MiB | 36.1 s, 322 MiB / 20.9 s, 1.58 GiB | 7.6 s, 903 MiB / 11.2 s, 1.58 GiB |
| Occurrences | 302.7 s, 84 MiB / 160.4 s, 128 MiB | 298.7 s, 498 MiB / 160.4 s, 521 MiB | 296.9 s, 561 MiB / 164.0 s, 548 MiB |

The responses were 29.3 MB (100 message records), 172 MB (1,000
message records), 41.9 MB (100 occurrence records) and 57.3 MB (1,000
occurrence records), each with the extras (67 bytes each as hex).
Under a concurrent writer at ten commits a second a streamed round
(48 to 62 s on messages, 287 to 296 s on occurrences) grew the WAL by
186 MB on messages and 859 MB on occurrences; unthrottled, by 5.2 GB
(16,231 commits) and 21.0 GB (65,877 commits). After each round the
truncating checkpoint returned busy 0 and left the WAL at 0 bytes.

Stream against collect, with the scan-method order balanced: collect
was faster by 1.7 to 2.7 times on messages at K = 100 and in the
certificate, and by 1.8 to 1.9 times on occurrences; at K = 1,000 on
messages stream was faster (7.6 s against 11.2 s). A cap-sized run
taken with stream always first put collect at 2.2 times on occurrence
rounds and 3.6 on the occurrence certificate; balanced they are 1.8 to
1.9 and 1.9.

**K is not compared.** In this run K ran ascending within each repeat,
so the first K (100) of each scan method was read on a colder page
cache than the later K (1,000), and the times of one K are not
comparable with another's. Each round time above is labelled with its
K. The K = 100 time is the conservative one for how long a round holds
its snapshot; nothing here claims how K changes it. The benchmark now
alternates K ascending and descending across repeats, which balances
only with `--repeat` of at least 2, and this run predates it.

What the figures show, for one round at a time: time, not memory, is
the ceiling. Memory peaked at 903 MiB with the scan method proposed
below (stream for messages, collect for occurrences; the collected
message scan, not proposed, reached 1.58 GiB). Concurrent rounds were
not measured
([#1423](https://github.com/marshalltech81/protonmail-local-ai/issues/1423)).
A round at the caps holds one read snapshot for 7.6 s (K = 1,000) to
36 s (K = 100) on messages and 160 to 164 s on occurrences with the
proposed scan method (11 to 21 s and 297 to 299 s with the other).
The indexer's
truncating checkpoint cannot finish for that long, and the WAL holds
every frame written meanwhile (0.86 GB over a 287 s streamed
occurrence round at ten commits a second). A cap of 600,000 occurrences
therefore implies a snapshot of nearly three minutes per round on this
corpus shape; whether that snapshot time is acceptable, or the caps
should be lower, is an owner decision. Nothing here measures a larger
set.

**Method notes.** Each scan method runs as its own process, but the page
cache is the host's, so a method that always ran second would inherit
the pages the first read. From this revision the benchmark alternates
which method runs first at each repeat (and in the filtered runs), and
reports in `first_runs` how often each ran first. The tables before
"Combined worst case" were taken with stream first, so their
collect-over-stream speedups may be overstated; the cap-sized run
below was taken in alternating order and is not affected.
`--all-extras` builds the accepted worst upload
(no member held, `--upload-total` digests, all extras) and returns
worst-case records first; the `extras` field of each round and of the
`request` is the count actually generated.

**WAL under a concurrent writer.** A second process committed, in a
loop, eight message updates plus a ballast blob per transaction while
one round held its snapshot; commits are counted by their time inside
the round's transaction. The WAL keeps every frame written during the
round, so its growth is the round's duration times the writer's commit
rate times the WAL bytes one commit writes. Measured per overlapping
commit: about 315 KB for a 128 KB ballast and 3.7 to 4.2 MB for 2 MB.
With the writer unthrottled that was 184 MB during a 0.25 s message
round and 3.1 GB during a 5.5 s occurrence round. At no more than ten
commits a second it stayed within 4.5 MB (the steady state is 4.2 to
4.6 MB) during message rounds, reached 5.1 MB during a 0.7 s
occurrence round and 15 to 17 MB during 4.0 to 4.6 s ones, and 34 MB
during a 0.9 s occurrence round with 2 MB commits. An indexer commit's
WAL bytes depend on its chunks, vectors and FTS rows, so these figures
size the rule, not the indexer's own rate. After each round a
`wal_checkpoint(TRUNCATE)` returned busy 0 and left the WAL at 0
bytes. The indexer's own truncating checkpoint (every 10 minutes)
cannot finish while a round holds its snapshot; it logs busy and
retries on its next pass.

The WAL windows were timed from the end of the `COUNT` scan until the
benchmark took its snapshot at the first statement; commits during that
scan were retained but not counted, so the recorded overlapping-commit
counts are slightly low (the WAL sizes are measured directly).

The writer of the cap-sized runs (above and below) also inserted one
row into a table per commit, to time the commits; it is removed. The
same small configuration (20,000 messages, an unthrottled writer, about
17,000 commits per round) with and without it left 319.1 KB (messages)
and 319.2 KB (occurrences) of WAL per overlapping commit with the
insert and 315.0 KB and 315.1 KB without, 1.3 % less: about one 4 KB
page per commit. Discount the cap-sized WAL figures by that much; they
were not repeated.

**Proposed limits, from these figures.**

- **Prerequisites:** bound each record's read (#1377, #1381) before
  the reconcile tool ships; without them no K bounds a round's work.
  Bound the packed request's structure before it is parsed (above);
  the byte cap alone does not.
- **K:** 100 message records and 1,000 occurrence records per round.
  On the largest records measured, with few extras, that is 15.9 MB and
  17.1 MB per response at a peak RSS of 167 MiB and 156 MiB; with the
  largest accepted upload (all extras, 200,000 and 600,000 of them) the
  cap-sized run measured 29.3 MB and 322 MiB for 100 message records
  and 57.3 MB and 548 MiB for 1,000 occurrence records. A typical
  response is 0.1 MB and 1.0 MB. A larger K would need a byte budget
  per response alongside it to keep the worst case bounded, which the
  approved design does not include.
- **Retry bound:** at most ceil(M / K) + 3 rounds per run, where M is
  the missing count of the run's first round: ceil(M / K) rounds that
  each return up to K records, plus three for churn. When they run out,
  the verdict is not certified with a fixed reason. After a paged run
  that missed a few members it is one round; a repair from nothing at
  47,500 messages is 475 rounds (under a minute streamed with typical
  IDs, about 5 minutes with four-byte ones), and at 142,500 occurrences
  143 rounds (about a minute collected).
- **Request size:** a packed upload as above, 43 bytes per digest. At
  most 200,000 messages (8.5 MB) and 600,000 occurrences (25.6 MB) per
  certified set, checked against the round's `COUNT(*)` before the
  scan; a larger set is not certified with a fixed reason (narrow the
  filters). A request body over the cap's packed size plus 64 KiB is
  refused before it is parsed. Extras number at most the uploaded
  digests, so they add at most 67 bytes each as hex (43 packed) to a
  response.
- **Scan method:** stream for messages (flat RSS whatever the ID width)
  and collect for occurrences, whose IDs are a fixed 64 bytes: the full
  collect round was 1.6 to 2.4 times faster on occurrences, for at most
  23 MiB more peak RSS at the cap-sized run (1.8 to 1.9 times
  faster there, order balanced), and a collected
  certificate cost at most 1.06 times one page in the balanced filtered
  runs, where a streamed one reached 2.8 times (7.6 times in the earlier
  runs, stream first).
- **Filtered queries:** no bound is proposed or claimed. The work of
  a filtered predicate per message is not bounded by the message-count
  cap (a message can carry the parser's maximum of chunks, participant
  rows and name rows, and a `where` expression can combine up to 16
  such leaves), and it was not measured at cap size
  ([#1396](https://github.com/marshalltech81/protonmail-local-ai/issues/1422)).
  What was measured, with two repeats that put the page first once and
  last once (the benchmark now rotates page, stream and collect over
  repeats, balanced when the count is a multiple of three), without
  planner statistics: on 50,000 messages
  (142,500 occurrences) a certificate cost 0.14 to 1.56 times one page
  of the same query streamed and 0.14 to 1.06 times collected; on the
  `query_attachments` clauses (filename, MIME type, extraction status,
  thread) streamed it reached 2.83 times. On 300 messages that each
  carry 10,000 participant rows and 11,000 display-name rows (3.0 and
  3.3 million rows), a participant predicate took a certificate
  4.5 s against a page of 9.0 to 11.9 s (messages) and 6.8 s
  (occurrences), and a `where` expression 0.6 to 1.3 s against 18.4 to
  18.6 s. On 15,000 messages of 20 chunks of 1,000 words (300,000
  chunks), each message committed on its own as the indexer does
  (`--commit-each`), a 16-term `text` or `body_words` value took a
  certificate 2.5 to 2.6 s against a page of 5.2 to 6.4 s (FTS rowids in insertion order). Every other
  corpus here was built in transactions of 2,000 messages (10 for the
  cardinality corpus), which lays out FTS5 segments differently from
  per-message commits, so their `text` and `body_words` timings are not
  claimed for a production-built index; the reconcile rounds read no
  FTS table.

### `aggregate_messages`
Count the messages [`query_messages`](#query_messages) would match,
grouped by one dimension, without listing them
([#823](https://github.com/marshalltech81/protonmail-local-ai/issues/823)).
Use it for volume questions (top senders, mail per month, mail per
folder) instead of paging `query_messages` and counting on the client.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `group_by` | string | required | `sender_address`, `sender_domain`, `folder`, `year`, `month` or `authority_class` |
| every `query_messages` filter | | none | `sender`, `recipient`, `participant`, `subject`, `text`, `folder`, `date_from`, `date_to`, `has_attachments`, `authority_class`, `seen`, `flagged`, `replied`, `size_min`, `size_max` and `where`, with the same schema and meaning; Trash is left out unless `folder="Trash"` ([Trash](#trash-is-left-out-by-default)) |
| `limit` | int | `25` | Groups per page; clamped to `[1, 100]` |
| `cursor` | string | none | `next_cursor` from the previous page of the same call |

The filters compile to the same expression as `query_messages`
([Filter predicates](#filter-predicates)), so `total_matches` and
`indeterminate` are the counts `query_messages` returns for the same
filters, and a group's `messages` is the `total_matches` of
`query_messages` for the same filters plus the group's own:

| `group_by` | Group value | The group's own filter |
|---|---|---|
| `sender_address` | a From address | a `where` `address_is` leaf, role `from` |
| `sender_domain` | the domain of a From address | a `where` `domain_is` leaf, role `from` |
| `folder` | the folder | `folder` |
| `year` | `YYYY` of the effective time (`occurred_at`, else `sent_at`), UTC; no value without either (or with a send date not yet checked), never the time a message was first indexed | `date_from` / `date_to` spanning that UTC year |
| `month` | `YYYY-MM` of the effective time, UTC; no value likewise | `date_from` / `date_to` spanning that UTC month |
| `authority_class` | the class of a From address | `authority_class` |

Per group: `messages`, `threads` (distinct threads of those
messages), `first_at` / `last_at` (their earliest and latest effective
time, from the messages with a delivery or checked send date),
`with_attachments` (those with an attachment), `indeterminate`
(messages in the group the filters could not decide, never counted in
`messages`) and, for `sender_address`, `display_name` (the display name
on the group's latest match that has one, cut at 500 characters with a
marker). A group whose messages are all undecided is listed with
`messages` 0. The group value is never cut. `where` takes an address
of at most 320 characters and a domain of at most 255; a longer
sender address can be passed as `sender` instead, which matches a full
address exactly.

The group with value `null` holds the messages with no value on the
dimension. For the sender dimensions: a message whose
`sender_ambiguous` is not `false`, or with no stored From address
([Sender attribution](#sender-attribution)). For `authority_class`,
also a message in Spam or one whose From addresses have no class. A
message with several From addresses counts in the group of each, so
those groups can sum to more than `total_matches`; `folder`, `year`
and `month` have one value per message, so their groups sum to
`total_matches` and their `indeterminate` values to the call's. A
group's `indeterminate` covers the supplied filters only, not the
group's own value: a message whose membership in a sender group cannot
be decided is not in it.

`incomplete_from_messages` (for `sender_address`, `sender_domain` and
`authority_class`; absent for the other dimensions) counts the
messages the filters do not reject (matched or undecided) whose sender
is attributable (`sender_ambiguous` is `false`) but whose stored From
list is not known to be complete: a From address was lost to a parse
cap or could not be parsed, or the message is not reparsed yet. Each
is counted once over the whole result, the same on every page. For
these messages further sender groups or memberships may be missing;
the groups shown, `total_matches` and `indeterminate` are unchanged,
and a group's `messages` still equals `query_messages`' count for
`sender=<value>`. It counts messages, not missing addresses, and
includes a message with no stored From address, which is also in the
`null` group. For `authority_class` it leaves out Spam, where no class
matches. The prose states it when not 0, and the timing line carries
it.

Groups come most messages first, then by value, with the `null` group
last among ties. `total_groups` counts them all. Group values and
display names go to the calling model, which may be remote, so the
tool description asks the client to tell the user how many groups it
will read before paging past the first page, and to prefer the
smallest page that answers (the top groups for a "top N" question).
To page, call again with the same filters and `group_by` plus `cursor`.
The cursor is bound to the filters and the dimension, as
`query_messages`' is. Each page recounts, so mail indexed between
pages can move a group to another page. One SQL statement answers a
call: it evaluates the filters once per message to select them and
once more per message they do not reject, then groups those rows;
`limit` caps the groups returned, not the messages grouped.

### `query_attachments`
Enumerate **every** attachment occurrence matching exact criteria, with
a total count
([#796](https://github.com/marshalltech81/protonmail-local-ai/issues/796)).
An occurrence is one attachment on one message: the same bytes attached
to two messages, or twice to one, are separate rows that share an
`attachment_id`. Rows are not ranked; the newest carrying message comes
first by effective time (`occurred_at`, else `sent_at`, else the time
it was first indexed), and claimant ID then occurrence ID break ties. Use it for "list every attachment" and
"how many" questions; [`search_attachments`](#search_attachments) ranks
by filename and extracted text and stops at 50 results.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `sender` | string | none | From of the carrying message, matched as `query_messages` `sender` |
| `recipient` | string | none | To or Cc of the carrying message, likewise |
| `participant` | string | none | Any role of the carrying message, likewise |
| `folder` | string | none | Exact folder name of the carrying message. Without it, attachments on messages in Trash are left out; pass `"Trash"` to list them ([Trash](#trash-is-left-out-by-default)) |
| `date_from` | string | none | Inclusive ISO 8601 lower bound on the carrying message's effective time |
| `date_to` | string | none | Inclusive upper bound; a date-only value covers the whole UTC day. `date_bounds` echoes the UTC instants applied |
| `filename` | string | none | Unicode caseless substring of the filename, taken literally (`%` and `_` are ordinary characters) |
| `content_type` | string | none | Exact MIME type, e.g. `application/pdf` |
| `extraction_status` | string | none | `success`, `empty`, `unsupported`, `too_large`, `failed`, `deferred` (extraction waits for a later indexer pass, #1236), or `none` (no extraction recorded); blank is ignored, any other value is an error |
| `claimant_id` | string | none | Exact claimant ID of the carrying message |
| `thread_id` | string | none | Exact thread ID |
| `limit` | int | `20` | Attachments per page; clamped to `[1, 50]` |
| `cursor` | string | none | `next_cursor` from the previous page of the same query |

All given filters must match; blank filters are ignored. The message
filters (`sender`, `recipient`, `participant`, `folder`, the dates) are
the [predicate leaves](#filter-predicates) `query_messages` builds,
decided on the message carrying each attachment, so they match the same
messages. There is no `date_basis`: the clock is effective time, as in
`query_messages`
([#1150](https://github.com/marshalltech81/protonmail-local-ai/issues/1150)).

**Unknown values.** A filter can leave an occurrence undecided: a
message filter for the reasons it leaves a message undecided in
`query_messages` (a carrying message whose sender is ambiguous or not
yet checked, whose stored addresses for the filtered role were cut by a
parse limit or are not checked yet, [stored
content](#filter-predicates), whose display names are not all
indexed, or, under a date bound, with no delivery date and no checked
send date; right after the upgrade that added the completeness flags,
#1086, every address filter reports each attachment on mail it does not
match as `indeterminate` until the reparse reaches that mail), and an
`extraction_status` other than `none` on an occurrence with no
extraction recorded for its payload and extractor module (not run yet,
or extraction off), since it may still be extracted with that status.
`none` decides exactly those occurrences. `deferred` is read from the
occurrence's own mark, so it is always decided, and a deferred
occurrence matches no other status, whatever its payload's extraction
row says. The filters conjoin with
SQL's three-valued AND, as in `query_messages`: an occurrence one filter
rejects is rejected even when another cannot decide it. Undecided
occurrences are in neither `total_matches` nor the pages; the response
counts them in `indeterminate`, and the prose states it with its causes
whenever it is not 0 (an empty first page then ends "No attachments are
known to match.").

**Response contract.** The response states the filter interpretation,
`total_matches` (over the whole match, not the page), `indeterminate`,
and `status_counts`: `total_matches` split by extraction status, with
`deferred` for occurrences the indexer will extract on a later pass
(#1236) and `none` for occurrences that have no extraction recorded. Anything but
`success` means no extracted text is available, not that the file says
nothing relevant. Each row carries the occurrence ID, the payload's
`attachment_id`, its `extractor_module` (`''` when its label selects no
extractor), the claimant, Message-ID and thread IDs, the filename and
MIME type (each cut at 500 characters, with `filename_clipped` /
`content_type_clipped` set when the stored value is longer), the size,
the carrying message's folder, `sent_at`, `sent_at_status`,
`occurred_at` and `source_file`, and the extraction's status, extractor, time and
`ocr_pages_skipped` (all null when none is recorded; for a `deferred`
occurrence the status is `deferred` and the other three are null). The
counts and the page are read in one snapshot.

It returns no attachment text. To read a listed attachment's stored
text, pass its `attachment_occurrence_id` to
[`get_attachment`](#get_attachment); [`get_evidence`](#get_evidence)
and [`ask_mailbox`](#ask_mailbox) return ranked, capped passages chosen
by a query, which can leave the listed attachment out or show another
copy of the same bytes. Report unread attachment text as a coverage
limit.

Rows carry private mail metadata (filenames, IDs, folders) and go to
the calling model, which may be remote. Start with narrow filters and
`limit=1` to obtain the count; before paging, tell the user the scope
and how many rows will be paged, and prefer the smallest sample that
answers the question.

**Paging.** Keyset pagination on descending `(effective_at,
claimant_id, attachment_occurrence_id)`. A cursor is bound to the tool,
the filters and the ordering, not to `limit`, so the page size may
change between pages; a cursor from other filters, from
`query_messages`, or a malformed one is an error. Each page uses a fresh
index snapshot, with the caveats of [`query_messages`](#query_messages)
paging: an attachment indexed or moved (with its message) while paging can be
missed, and a changed `total_matches` signals churn but matching totals
do not prove nothing was missed
([#1218](https://github.com/marshalltech81/protonmail-local-ai/issues/1218)).

**Cost.** Each page runs one grouped count over the match, a second
count only when a filter can be undecided, and one keyset query that
orders keys only and reads the row columns (the filename among them) of
at most `limit + 1` occurrences. On a synthetic index of 75,000
occurrences a page took about 60 ms unfiltered. The log records only
`extraction_status`, `limit` and valid ISO dates; filenames, MIME types, IDs,
addresses, folders and cursors are withheld.

### `get_attachment`
Read one attachment occurrence's whole stored extracted text, one page
at a time
([#796](https://github.com/marshalltech81/protonmail-local-ai/issues/796)).
Pass an `attachment_occurrence_id` from
[`query_attachments`](#query_attachments). The occurrence is read
whatever its message's folder, Trash included, as the other tools that
read one named item are.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `attachment_occurrence_id` | string | required | One attachment on one message, from `query_attachments` |
| `offset` | int | `0` | Text character to start the page at; pass the previous response's `next_offset` |

**Response contract.** `attachment` is the occurrence as
`query_attachments` lists it (IDs, filename and MIME type cut at 500
characters with flags, size, the carrying message's folder and dates,
`source_file`, and the extraction's status, extractor, time and
`ocr_pages_skipped`). The text comes in pages of 20,000 characters, as
`get_message` pages a body: `text` is the page, `text_offset` its first
character, `text_total_chars` the whole stored text's length and
`next_offset` the next page's offset (null at the end). Offsets count
characters (code points), and a page never ends inside a combining
sequence or zero-width-joined pair; both tools cut pages with one
helper, so they end in the same places. Paging from 0 through each
`next_offset` returns the stored text exactly, NUL characters included.
An offset past the end is an error.

"Whole" means the stored extraction, not the original file. The
indexer's extraction cap (`INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`) and
extractor work caps may have cut it, and the index does not record
whether they did, so `truncated` is always null
([#1261](https://github.com/marshalltech81/protonmail-local-ai/issues/1261)).
`ocr_pages_skipped` counts scanned PDF pages the OCR page cap left
unread.

| Extraction | `text` | `unavailable_reason` |
|---|---|---|
| `success` | the stored text | null |
| `empty` | `""` | null |
| `failed` | null | `extraction failed` (the stored error is never returned) |
| `unsupported` | null | `no extractor could read this file: its type has no extractor, the extractor is missing from this image, or it declined the file (for example an encrypted PDF or a work cap)`, or, when OCR was off, `the file needs OCR, which is off (INDEXER_OCR_ENABLED=false)` |
| `too_large` | null | the file is over the indexer's attachment size limit |
| `deferred` | null | the indexer deferred this attachment's extraction to a later pass (its per-message extraction budget was reached); its text is not indexed yet |
| none recorded | null | no extraction is recorded yet (not run yet, or extraction off) |

Report null text as unread text, not as an attachment that says
nothing relevant. An unknown `attachment_occurrence_id` is an error.

The text goes to the calling model, which may be remote. Before
reading more than one attachment, or every page of a long one, tell
the user which attachments and roughly how much text will be read
(each page states the total), and prefer the smallest sample that
answers the question.

**Cost.** One read transaction looks the occurrence up and opens its
extraction row with a read-only incremental blob read (`blobopen`); no
query selects the text column, so a long row is never loaded whole,
even with the extraction cap off. The reader decodes 64 KiB blocks of
UTF-8 from the start only until the page is complete, then counts the
rest's characters block by block without keeping them, so memory is
one block plus one page and each call reads the whole stored text once
(about 1 ms for the default 2,000,000-character cap, about 30 ms for
50 MB, in the image). SQLite's own `length` and `substr` would stop at
an embedded NUL, which plain-text extraction keeps. The log records
only `offset`; the occurrence ID is withheld. An unknown ID logs
the fixed `get_attachment failed: not_found` WARNING through a rate
limiter: the first in each 60-second window, the rest counted.

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
  the counts (never any content). The server also returns that note in
  the tool's text `content` and structured `coverage_note`, separately
  from the model's answer and its citation checks, stating that the
  answer may be incomplete. The model
  is instructed not to repeat prompt-budget caveats in its answer.
  When the model window rather than the per-thread budget cut the
  evidence, or a reply stopped at `INFERENCE_MAX_TOKENS`, the server
  log also gets one
  [`token limit hit`](troubleshooting.md#the-log-shows-token-limit-hit)
  warning for the call.
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
`INFERENCE_CONTEXT_TOKENS` less `INFERENCE_MAX_TOKENS` (defaults 32768 and 1024, or
48000 and 16000 in `INFERENCE_MODE=anthropic`, where current Claude models
count their thinking against the reply limit)
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
room one does not need to the other, and returns a coverage note when
the window left out or cut short passages its own cap would show.
`extract_from_emails` adds a
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

### Structured outputs

`extract_from_emails`, `brief_issue` and `check_conclusion` ask the model
for JSON. With `INFERENCE_MODE=anthropic` and
`INFERENCE_STRUCTURED_OUTPUT=true` (the default), each of their calls
also sends the reply's JSON schema as an Anthropic structured-output
format (`output_config.format`), so the API returns JSON matching it
rather than JSON the server has to find in prose (#808).
`brief_issue` and `check_conclusion` send constant schemas matching
their reply shapes; `extract_from_emails` builds one from the caller's
schema (see [its section](#extract_from_emails)). The `brief_issue` and
`check_conclusion` prompts are unchanged; `extract_from_emails`' prompt
asks for the `{"records": [...]}` wrapper instead of an object or
`null` (see its section). The checks on each reply are unchanged.

Anthropic adds a system prompt describing the format, billed as input
tokens, so when a schema is sent the prompt budget keeps room for it:
one token per character of the schema (measured 2026-10-05 at 394
tokens for a 456-character schema and 2,075 for a 2,589-character one,
always below that). Anthropic also processes prompts and replies as
usual but caches the schema itself for up to 24 hours since its last
use, apart from them. The constant `brief_issue` and `check_conclusion`
schemas carry no request data. `extract_from_emails` sends no field
name of yours: its schema names fields `f1`, `f2`, … in declaration
order, the prompt lists which is which (`Record keys: {"f1": "vendor"}`),
and the reply is mapped back to your names before any check.

A model or gateway without structured outputs rejects the request with
status 400 (check Anthropic's structured-output compatibility list;
`claude-sonnet-4-6` and the current Sonnet and Opus models have them). The tool call then fails with a
fixed-text error that lists the common causes of a 400 in order (account
credit or billing, an invalid or retired `INFERENCE_MODEL`, a prompt too
large for the model's window, then a model or gateway without structured
outputs) and names `INFERENCE_STRUCTURED_OUTPUT=false` for the last one
only. The request is not retried without the format, and only the
error type and status are logged. Set `INFERENCE_STRUCTURED_OUTPUT=false` for such a
model to get the plain-JSON requests. Any value other than `true` or
`false` (in any case; unset or empty is `true`) fails startup.
`INFERENCE_MODE=openai` ignores the setting and never sends a format
(#807).

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
   small-capital letters, ligatures, accents, zero-width characters,
   any other Unicode default-ignorable code point such as the blank
   Hangul fillers (#680), and a closed list of Cyrillic, Greek and Armenian look-alikes for the
   tag's letters (a Cyrillic `е`, a Greek `ο`) do not hide it. This is
   a fixed list for the tag names, not a general confusables table.

These are defense-in-depth measures — they do not guarantee immunity.
Operators running `INFERENCE_MODE=anthropic` should still treat retrieved email
content as potentially hostile.

### `ask_mailbox`
Ask a natural language question about your email.
Retrieves relevant threads and synthesizes an answer.

The answer prompt limits facts to the supplied excerpts and asks for
only the requested facts, with a citation on
the opening answer sentence as well as each subsequent statement.
For a current or final fact, earlier versions are included only when
the question asks how it changed; unresolved conflicts still cite both
sides. When the supplied passages do not answer the question, the model
is asked for one sentence beginning `Not found in the provided emails`,
then to stop without citations or related claims. These are prompt
instructions, not guarantees of model behavior; the citation checks
and one-repair limit remain unchanged. Prompt-budget omissions are
reported by the server in text `content` and the additive structured
`coverage_note` field, outside `answer` and its checked statements.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `question` | string | required | Your question in plain English |
| `from_addr` | string | none | Scope to a sender address or domain |
| `from_name` | string | none | Scope to mail from a named person or role; resolved through `find_contact` exactly as in [`search_emails`](#search_emails). `from_addr` wins if both are given |
| `participant` | string | none | Scope to threads where this person appears in **any** role — From, To, or Cc, as in `search_emails`. Accepts an address, a domain (`@example.com`), or a name fragment |
| `date_from` | string | none | Date lower bound |
| `date_to` | string | none | Date upper bound |
| `folders` | list | all but Trash | Scope to threads with a message in any of these folders (the membership `list_threads` uses). Without it, threads filed only in Trash are left out; name `"Trash"` to include them ([Trash](#trash-is-left-out-by-default)) |
| `max_threads` | int | `5` | Context threads to use |

**Questions about a person
([#696](https://github.com/marshalltech81/protonmail-local-ai/issues/696)).**
Retrieval ranks by text. A person named only in the From / To / Cc
headers of their threads matches only the thread keyword lane, and
threads that mention the name in their bodies outrank them in hybrid
fusion. The keyword slot
([#701](https://github.com/marshalltech81/protonmail-local-ai/issues/701))
keeps only the single best such thread in the top three, so "who is
Dana Example?" can still miss the person's other threads. The tool
description tells the calling model to resolve the person with
`find_contact` and pass the address as `participant`, which keeps the
evidence to the threads the person is on, including those they only
received. `find_contact` ranks contacts across every folder, Trash
included, so with `folders` set its top address can have no thread in
scope; the description tells the model to try the next matching contact
or pass the name itself as `participant`, which then matches within the
folder scope. `from_name` is sender-only, like `from_addr`. An unmatched
`from_name` returns an empty answer naming it, with no model call,
rather than searching without the filter. A blank `participant` or
`from_name` (`""` or whitespace, as some clients send unset optionals)
is treated as absent, and a padded one is stripped, here and in
`search_emails`, `extract_from_emails` and `get_evidence`.

**Late resolutions in long threads
([#974](https://github.com/marshalltech81/protonmail-local-ai/issues/974)).**
Each thread gives at most six passages (`PROMPT_EVIDENCE_CHUNKS_PER_THREAD`),
then cut to the per-thread prompt budget, ordered by similarity to the
question, not by position. When the question matches one of the
thread's attachments by filename or MIME type, that attachment's first
chunk comes first. The chunk holding the question's rarest words in
that thread comes next, unless it is the first one (#858, #1246). The rest
follow: that attachment's chunks, then the thread's other attachment
chunks, then body chunks, each group by similarity, so attachments can
fill every slot but the keyword one before a body message. The budget
can cut the second passage short or leave it out, and the evidence
note discloses it. A thread with no indexed chunks shows its
indexed text instead. So in a long thread the passages can stop before
a late resolution: the message that settles the matter is often worded
nothing like the question. The `ask_mailbox` and `get_evidence`
descriptions tell the calling model so: for status or closure, re-ask
about the resolution without the attachment's filename or file-type
words such as "PDF" (the match covers MIME types too, so either would
pull the same attachment passages back in), or read the thread's later
messages with `get_thread` or `get_message`. `get_evidence` orders
passages the same way, with six per thread mailbox-wide and `limit`
with `thread_id`, but has no indexed-text fallback: its description
says a chunkless thread is listed with an empty `chunks` list (with
`max_threads`) or left out, to be read with `get_thread`.

`max_threads` is clamped to `[1, 10]` at the tool boundary so an
inflated caller-supplied value cannot expand into an oversized prompt
that blows past the model's context window.

#### Evidence scope: in scope or context

Filters select whole threads: a thread qualifies when any of its
messages meets the sender, participant, date or folder condition, and
its passages can come from any of its messages, so the evidence can
hold another sender's reply, a message outside the date range or a
copy filed in Trash. Whole threads are kept on purpose, since replies
and later corrections are often what a correct answer needs (owner
decision, [#755](https://github.com/marshalltech81/protonmail-local-ai/issues/755)).
Each passage is instead labelled from its own message's indexed
metadata, at query time (`Database.message_scope`, no schema change):

- **`in scope`**: the message meets every message-level filter of the
  request, with `query_messages`' per-message predicates: `from_addr`
  (or the address `from_name` resolved to) in its From line;
  `participant` in its From, To or Cc; its effective time
  (`occurred_at`, else `sent_at`) within `date_from` / `date_to`; and
  the folder it is filed in within `folders`, or, without `folders`,
  any folder but Trash. A message `query_messages` would count as
  `indeterminate` (a `from_addr` filter, or a `participant` filter not
  met through To or Cc, on a message
  whose `sender_ambiguous` is not `false`, [Sender
  attribution](#sender-attribution); a value that matches nothing on a
  message whose addresses in those roles, or for a name or fragment its
  display names, are not all indexed) is not in scope, so right after
  the upgrade that added `sender_ambiguous`, a `from_addr` request
  labels the passages of mail indexed before it `context` until the
  reparse drains.
- **`context`**: any other message of a qualifying thread. A passage
  of a thread's combined text (a thread with no indexed chunks) is in
  scope only when every message of the thread is.
- When the same passage text appears in several messages of a thread
  (a quoted reply), one copy is shown; an in-scope copy is kept over a
  context copy ranked above it.

The labels change nothing about which threads are retrieved or how
they rank. When a filter is given, or a retrieved thread holds a
message outside the default scope (filed in Trash), the prompt shows
them: each passage header carries `in scope` or `context` after the
sent date, and a scope block between the evidence and the question
states the filters and the rule. The block holds only the caller's
own arguments, never a value the server read from mail: with
`from_name` it names that name and not the address it resolved to.
Address and name values (`from_addr`, `from_name`, `participant`) can
still be copies of sender-controlled headers, for example a
`find_contact` result passed on as `participant`, so the filter lines
only name those filters and their values follow inside an
`<untrusted_email>` fence. Each value is cut at 500 characters, has
delimiter tags escaped and is written as one JSON string on its own
line; the date bounds are the server's normalized UTC instants and the
folder names are the operator's own, so they stay in the filter lines. The
block names the `from_name` and the `participant`
([#779](https://github.com/marshalltech81/protonmail-local-ai/issues/779)),
so "what did this person say" no longer leaves the model guessing who
is meant. The rule: answer from in-scope passages, use context
passages only to interpret them or to report a later correction, and
say so; if no in-scope passage answers the question, the excerpts do
not answer it. With no filter and every retrieved message in the
default scope, every passage is in scope and the prompt is unchanged.
Each citation's `scope` is `in_scope` or `context` either way, and
`get_evidence` returns the same label per chunk.

`extract_from_emails`, `brief_issue` and `check_conclusion` label their
passages the same way
([#895](https://github.com/marshalltech81/protonmail-local-ai/issues/895)),
with the same scope block (fenced the same way) and the same condition
for showing it; `extract_from_emails` decides per thread, since each
thread is its own prompt. The block sits after the mail block and
before the reply format in `extract_from_emails`, before the topic in
`brief_issue` and before the conclusion block in `check_conclusion`.
Each tool's rule says how its task uses the labels:
`extract_from_emails` takes values from in-scope passages and leaves
out data that only context passages state; `brief_issue` and
`check_conclusion` build the brief or findings from in-scope passages,
use context only to interpret them or report a later correction or
change, and say so in the entry or finding. With no scope applying,
their prompts are unchanged as well. Citations carry `scope`, and an
extracted field whose cited passages are all context gets a
`context_only_fields` problem (see `extract_from_emails`).

How the ambiguous cases are labelled:

- **Long recipient lists.** `participant` counts every From, To and Cc
  entry the index records, which is every address the message lists
  (the indexer caps entity writes per message, not participant rows).
  A message where the person is one of forty Cc recipients is in
  scope, as it is for `query_messages(participant=...)`; the label
  says the message meets the filter, not that it is about the person.
  A bare name or domain matches by substring, as the thread filter
  does.
- **Folder moves.** The label uses the folder a message is filed in
  now, as indexed: a message moved from INBOX to Archive is in scope
  for `folders=["Archive"]` and context for `folders=["INBOX"]`. Two
  copies of one Message-ID in different folders are separate claimants
  and are labelled separately.
- **Mixed Trash and INBOX threads.** Without `folders`, a Trash
  message's passages are context, so a stale copy in Trash cannot be
  the answer to an unfiltered question. Naming `"Trash"` in `folders`
  makes them in scope.

**Citations (#284).** Each passage in the prompt starts with a header
line holding a server-assigned evidence label and the passage's own
message: `[E3 | message <claimant ID> | from <sender> | sent
<date> | chunk N chars X-Y]` (attachment passages also name the file
and MIME type; a thread shown by its indexed text, because it had no
matching chunks, gets `[E4 | thread text]`; when the prompt shows
[scope labels](#evidence-scope-in-scope-or-context), `in scope |` or
`context |` follows the sent date and `thread text` is followed by
`| in scope` or `| context`). The sender is the
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
a header never crowds its passage out of a thread's share. A sender
note ([Sender attribution](#sender-attribution)) is fixed text inside
the sender's 96 characters: the name is cut further, never the note. The sender
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
- **Scope.** An answer whose every valid label is a `context` passage
  (and that does not open with the not-found phrase) is a
  `context_only_citations` problem naming those labels: it rests on no
  message that meets the request's filters. Without a filter this
  happens only when every cited passage is from a Trash message or
  combined thread text of a thread holding one.
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
| `coverage_note` | Server-written notice of prompt-budget omissions/truncation and possible incompleteness; `null` when nothing was left out or cut to fit. This is separate from model prose and citation validation |
| `citations` | One entry per valid cited label, in first-cited order: `label`, `chunk_id`, `claimant_id`, `message_id`, `thread_id`, `sender`, `sender_ambiguous` ([Sender attribution](#sender-attribution); null for `thread`), `sent_at`, `occurred_at`, `source` (`body`, `attachment` or `thread`), `attachment_id`, `attachment_filename`, `char_start`, `char_end` (end of the part shown to the model), `scope` (`in_scope` or `context`, [Evidence scope](#evidence-scope-in-scope-or-context)), `extraction_deferred` (the passage is retained indexed text of an attachment whose re-extraction is pending, #1236; its prompt header and prose citation end with `retained indexed text; extraction refresh pending`, and the timing line counts such passages shown as `evidence_extraction_deferred`) |
| `statements` | The answer cut into statements: `text`, `labels` (the supplied passages it cites) and `status` (`cited`, `unsupported`, `uncertain`, `uncited`, `invalid` for only unknown labels, or `not_checked`) |
| `quotes` | Each quotation: `text` (cut at 1,000 characters), `statement` (index into `statements`), `status` (`verified`, `misattributed`, `unmatched`, `uncited`, `not_checked`) and `found_in` (labels of the passages it was found in) |
| `citation_problems` | `[]` when the check passed, else entries `{kind, labels, statements, quotes}`, `kind` one of `unknown_labels`, `no_citations`, `uncited_statements`, `unmatched_quotes`, `misattributed_quotes`, `context_only_citations`; `statements` and `quotes` are indexes into those lists, and `labels` holds the unknown labels or, for `misattributed_quotes`, the passages the quotes were found in, or, for `context_only_citations`, the cited labels |
| `repair_attempted` | Whether the one repair call was made |
| `resolved_from_addr`, `from_name_matches` | The `from_name` lookup: the address filtered by and how many senders matched, up to 10 ([Resolving `from_name`](#search_emails)); `null` without `from_name` |
| `threads` | The threads searched, best match first (the `search_emails` thread shape) |

The prose in `content` is the answer, a `Citations:` list, any
citation-check lines (fixed text with counts and labels, never the
model's words), a `Quote check:` count when the answer quotes, any
server-written `coverage_note`, and
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

An answer the model stopped writing early is returned with a closing
`[Answer cut off …]` notice rather than as if complete;
`summarize_thread` does the same. The notice names the setting to
change: `INFERENCE_MAX_TOKENS` when the reply reached it,
`INFERENCE_CONTEXT_TOKENS` (lower it to the model's real window or
below, or use a model with a larger window) when the
model's own context window filled first (Anthropic's
`model_context_window_exceeded` stop). A reply cut before any text is
an error with the same distinction.

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

**Context trimmed by the window (#949).** When
`INFERENCE_CONTEXT_TOKENS` is too small for the context the tool's own
character caps would show, the context is cut to fit and the result
says so in `coverage_note`, written by the server and outside the
summary: the `ask_mailbox` evidence note with counts only (passages
left out and cut short, compared with what the caps would show), then
`<n> of <m> passages are shown. The summary may be incomplete.` The
note is `null` when the window trimmed nothing. Trimming by the caps
themselves is not reported.

Structured output: `summary`, `coverage_note` (above), `style` (the
style used; an unknown style is summarized as `brief`), `thread` (the
`search_emails` thread shape), and `citations`, `statements`, `quotes`,
`citation_problems` and `repair_attempted` as in
[`ask_mailbox`](#ask_mailbox). The prose in `content` is the summary
under its `Summary (<style>) — <subject>:` heading, then the
`Citations:` list, any citation-check lines (fixed text, counts and
labels) and the coverage note, if any. An `E1` citation has source `thread` and no chunk; read it
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
| `from_name` | string | none | Scope to mail from a named person or role; resolved through `find_contact` as in [`search_emails`](#search_emails). An unmatched name returns no records and makes no model call |
| `participant` | string | none | Scope to threads where this person appears in **any** role — From, To, or Cc, as in `search_emails` |

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

**Building a population
([#976](https://github.com/marshalltech81/protonmail-local-ai/issues/976)).**
The threads searched are the top `limit` hits for `query`, not every
match, and nothing names the threads left out. For every occurrence
backed by an attachment (every invoice line for one material code from
one vendor, say; a body-only population such as RSVPs has nothing to
enumerate and uses `extract_from_emails` alone), follow this recipe.
It covers an unscoped, non-Trash population only: `search_attachments`
has no folder filter and always leaves out Trash, so it cannot
reconcile a run scoped with `folders` or one that includes Trash;
reconcile those by hand.
The tool description says only that the top `limit` threads are
searched, points here, and asks the model to tell the user how much
mail a population run will read; the steps are not sent to clients:

1. Before the first call, say the date windows, the most attachment
   previews and the most threads the run will read:
   `search_attachments` already returns extracted-text previews to
   the calling model, and each extracted thread is one model call
   whose passages reach the inference provider.
2. Enumerate the attachments with `search_attachments`, a lexical
   match on the material code or its description, plus `sender` (the
   vendor's address, matched on the message carrying each attachment;
   `from_addr` would take every attachment in the vendor's threads)
   and `content_type` where they help, and `limit=50` (the default,
   20, would look like a window under the cap). There is no
   pagination, so split the period into `date_from` / `date_to`
   windows narrow enough that each returns fewer than 50 results.
   Before narrowing a window that returns 50, tell the user the added
   windows and previews, as in step 1. Every hit is dated by its
   carrying message, so 50 or more matching attachments with one
   effective time (one message carrying them all, or several messages
   with the same timestamp) cannot be separated by any window: when a
   window at the smallest interval the bounds can express still
   returns 50, report the population as truncated rather than
   narrowing further.
3. Run `extract_from_emails` per window, with `participant` set to
   the vendor's address (the tool has no `from_addr`) and a schema
   that declares its own `invoice_date` and `invoice_number`. The
   windows do not select the same set:
   `search_attachments` dates the message carrying the attachment,
   while `extract_from_emails` takes any thread whose span overlaps the
   window, so a January invoice in a thread with a June reply is listed
   for January but can be extracted (and take one of the `limit`
   slots) in June. Set `limit` to at least the window's thread count.
   `participant` selects whole threads, so attachments carried by
   other people's messages in the vendor's threads are extracted too,
   though step 2's `sender` leaves them out of the enumeration: check
   a record's sender from its citation's `sender` before counting it.
   That `sender`, like the `sender` and `participant` filters, is the
   claimed From address:
   the index does not authenticate senders and Spam stays searchable
   (`docs/architecture.md`, "Known limitation"), so a forged From
   matches too. Counting a record as the vendor's mail needs
   provenance these results do not give (the vendor's own records, or
   checking the message by hand). An occurrence with no record has no
   sender in these results; report it as unverified rather than
   reading each message with `get_message`.
4. Reconcile across all windows, not per window. A record links to
   its source through its `_evidence` labels: look each label up by
   `label` in that call's top-level `citations` list, whose entries
   carry `thread_id`, `claimant_id`, `attachment_id`, `sent_at` and
   `occurred_at`. Key each occurrence by `claimant_id` and
   `attachment_id` together: `attachment_id` is the payload's content
   hash, shared by every message carrying the same bytes, and
   `claimant_id` names the message. Count a record once, against the
   enumerated occurrence its citations name; drop duplicates, set
   aside for review a record that cites no enumerated occurrence, and
   report each enumerated occurrence with no record. Copies of the
   same bytes attached twice to one message share both IDs, so they
   cannot be reconciled one by one.

Limits the recipe does not remove:

- `search_attachments` leaves out Trash and attachments whose text
  extraction did not succeed and whose filename and MIME type do not
  match; its `from_addr` filter (not `sender`) runs after a bounded
  candidate scan, so a window under 50 filtered by it can miss matches.
- Each thread's passages are chosen by similarity to `query` (with
  the exceptions under `ask_mailbox`) and cut to a budget, so an invoice page whose line is not near the query
  can be missing from a searched thread
  ([#974](https://github.com/marshalltech81/protonmail-local-ai/issues/974),
  [#858](https://github.com/marshalltech81/protonmail-local-ai/issues/858)).
- `_date` is the thread's last message date, not the invoice date.
- A citation that covers an attachment does not prove that every
  requested line inside it was extracted.
- Records are model output. The server checks their shape, their
  citation labels and whether string values appear in the cited
  passages (`value_check`, below), not whether a value is right.

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

A thread whose answer was cut off (at `INFERENCE_MAX_TOKENS` or at the
model's context window), or was not
a JSON object, array of objects, or `null` / `[]`, or held a record that
failed the schema check, is counted as failed, never as having no data.
A failing record is dropped; the thread's other records are kept. An answer the provider stopped with a
content filter or refusal is an error. When any
thread fails, the records come back as the first content item and a
second item says how many of the searched threads could not be
extracted and why (a cut reply's count names the setting for its stop:
`INFERENCE_MAX_TOKENS`, or `INFERENCE_CONTEXT_TOKENS` for the context
window); if none were extracted the response says so rather
than "No structured data … found", which is reserved for every thread
answering `null` or `[]`.

**Structured outputs (#808).** With
[structured outputs](#structured-outputs) on, each call sends a strict
schema (with neutral keys `f1`, `f2`, …; see
[structured outputs](#structured-outputs)) and the model answers
`{"records": [<record>, ...]}`, mapped back to your field names; an empty
`records` list is its "no relevant data", in place of `null` / `[]`,
and a reply of any other shape counts as failed. The record schema is
built from yours in one pass over the declared fields, without walking
nested schemas:

- `string`, `number`, `integer` and `boolean` fields (or a list of these
  and `null`) take that type or `null`;
- a descriptive type (`"dollar amount"`), a property with no `type`, or
  a `required` name with no property takes a string or `null`;
- an `array` field takes a list of scalars (strings, numbers, booleans,
  `null`) or `null`;
- every declared field is required (`null` when the thread has no
  value), no other field is allowed, and `_evidence` is a closed object
  with one list of labels per field (empty when no passage gave it).

A schema that declares an `object` field, an array whose `items` is not
a scalar type, a property shaped only by `properties`, `items`, a
combinator (`anyOf`, `oneOf`, `allOf`) or `$ref`, a shorthand field
given as an object or other non-type value, a type list naming a
non-JSON type, or no fields at all cannot be expressed this way. Nor
can a type list mixing `array` with another type, or a schema of more
than 8 fields (counting `required` names with no property): Anthropic
refuses schemas over 16 union-typed parameters and schemas whose
compiled grammar is too large, a limit that depends on the shape
(measured 2026-10-05, every mix of up to 8 fields was accepted). Those
calls are sent
without the format, the reply is read as above, and the response adds
a fixed-text note saying so. The records then go through the same
schema check and citation checks as without structured outputs. In the
JSON Schema form a `null` in a property your schema does not require
stands for the omitted field and is dropped before the check; a `null`
in a required property still fails it. Shorthand records keep their
`null` fields.

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
- **Scope.** When the thread's passages are labelled
  ([Evidence scope](#evidence-scope-in-scope-or-context)), a field
  whose valid labels all name `context` passages gets a
  `context_only_fields` problem listing those fields and labels: its
  value rests on no message that meets the request's filters. The
  content line says so in fixed text with counts.

There is no repair call: each thread still drives exactly one model
call, and a record with problems is kept and reported. Only counts are
logged.

Structured output:

| Field | Description |
|---|---|
| `records` | The records, as in the first content item, each with `_source_thread`, `_date` and `_evidence` |
| `citations` | One entry per valid label any record cites, in first-cited order, with the `ask_mailbox` citation fields, `extraction_deferred` included |
| `fields` | One entry per field with a value: `record` (index into `records`), `field`, `labels`, `status` (`cited`, `uncited`, `invalid`), `value_check` (`verified`, `misattributed`, `unmatched`, `uncited`, `not_checked`) and `found_in` |
| `citation_problems` | `[]` when every field cites a supplied passage, else entries `{record, kind, labels, fields}`, `kind` one of `unknown_labels`, `uncited_fields`, `misattributed_values`, `context_only_fields` |
| `notice` | The incomplete-extraction or evidence note in `content`, or `null` |
| `resolved_from_addr`, `from_name_matches` | The `from_name` lookup: the address filtered by and how many senders matched, up to 10 ([Resolving `from_name`](#search_emails)); `null` without `from_name` |
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
`status: "invalid_json"`. A reply cut off before it finished is not
repaired and comes back with `status: "truncated"`, the stop in
`truncation_reason`, and the prose before the raw reply says where it
was cut and which setting to change, in the same words as
`extract_from_emails`' `Incomplete:` line: `max_tokens` when the reply reached `INFERENCE_MAX_TOKENS` (a brief
needs more output than an `ask_mailbox` answer, so raise it, for
example to 4096; anthropic mode defaults to 16000), `context_window`
when the model's own window filled first (lower
`INFERENCE_CONTEXT_TOKENS` to the model's real window or below, or use
a model with a larger one). Only counts are logged; a call that hit a
token limit (a cut reply, or evidence cut to fit the window) logs one
[`token limit hit`](troubleshooting.md#the-log-shows-token-limit-hit)
WARNING, as `ask_mailbox` does.

Structured output:

| Field | Description |
|---|---|
| `experimental` | Always `true` |
| `status` | `ok`, `invalid_json` or `truncated` |
| `truncation_reason` | When `truncated`: `max_tokens` (raise `INFERENCE_MAX_TOKENS`) or `context_window` (lower `INFERENCE_CONTEXT_TOKENS` to the model's real window or below, or use a model with a larger one); else `null` |
| `brief` | When `ok`: `chronology` (`date`, `date_source`: `sent` / `mentioned` / `unknown`, `actor`, `event`, `labels`; sorted oldest first, undated last), `positions` (`actor`, `position`, `labels`), `decisions` (`decision`, `labels`), `open_questions` (`question`, `labels`), `conflicts` (`description`, `labels`), `insufficient_evidence`; else `null` |
| `raw_text` | The unparsed reply when `status` is not `ok`, else `null` |
| `as_of` | Latest sent date (`YYYY-MM-DD`) among the passages supplied; the brief describes the evidence up to then |
| `citations` | Each valid cited label, first-cited order, in the `ask_mailbox` citation shape (claimant, sender, own sent date, chunk, `extraction_deferred`) |
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
reply cut off before it finished comes back with `status:
"truncated"`, its stop in `truncation_reason` and no repair, as for
`brief_issue`. Only counts are logged, and a token limit is logged as
for `brief_issue`.

The server attaches a `sources` entry to each finding for every valid
label it cites: the `ask_mailbox` citation fields (claimant, sender,
own sent date, chunk, `extraction_deferred`, whose prose note the
`Findings:` lines repeat) plus `excerpt`, the first 300 characters of the
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
| `truncation_reason` | As in `brief_issue` |
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
Reports the deployed MCP server version, whether the local index is current,
and what it holds. Call this when asked which version or build is running.
**Call this first** before answering questions about email content.

| Field | Meaning |
|---|---|
| `server_version` | MCP server image's source commit, including `-dirty` for local changes; `unknown` when build identity is unavailable. This identifies the server code, not the MCP protocol or SQLite schema version |
| `current` | `true` only when all three hold: mbsync completed a sync within three sync intervals (never less than 5 minutes), the indexer reported within 10 minutes, and no message is pending, retrying or deferred. A sync or indexer timestamp more than 2 minutes ahead of the server clock also makes it `false` |
| `not_current_reasons` | One line per failed condition; empty when `current` is `true` |
| `last_sync_at` / `sync_interval_secs` | mbsync's last successful sync from Bridge, and how often it syncs |
| `indexer_last_seen_at` | When the indexer last reported (at most every 30 s with its health heartbeat, including during the initial index) |
| `queue` | `pending` (found, not yet failed), `retrying` (failed at least once; will retry), `deferred` (postponed by the indexer without a failure of its own: a file it cannot read yet, an embedder outage or configuration error, or a job waiting for a rename; retried without spending attempts), `extraction_deferred` (messages already indexed whose attachment extraction reached the indexer's per-message budget: the rest of their attachments are extracted on later passes, #1236), `parked_trashed` (trashed files already indexed, waiting for the reaper to remove them or for the file to be restored, #1165), `dead` (failed permanently and incompletely indexed: missing from search, or found only by keyword, until `make requeue-dead`), `reparse` (of the pending, retrying, deferred and extraction_deferred jobs, those re-reading a message already indexed after an upgrade, #1078: searchable meanwhile, but data the upgrade adds is missing until it runs). `pending`, `retrying`, `deferred` and `extraction_deferred` make `current` false; `parked_trashed` and `dead` do not. A job that had already failed before an embedder outage deferred it counts as `retrying` |
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
on. Nor do parked trashed files (mirror mode only): they are already
indexed and wait only for the reaper, after
`INDEXER_DELETION_GRACE_DAYS`. `current` cannot see mail that reached Proton after the last sync,
or a delivery whose filesystem event the indexer missed (the periodic
Maildir rescan picks that up within `INDEXER_RECOVERY_SWEEP_INTERVAL_SECS`).

mcp-server never talks to Bridge. mbsync writes a stamp at the Maildir
root after each successful sync. The indexer acknowledges a sync only
once every message it delivered is queued, and records it with its own
liveness in the `ingestion_state` table that this tool reads (see
"Index currency" in `docs/architecture.md`).

The same helper powers ``make status`` on the host: the Makefile target
invokes the module-level ``get_mailbox_status`` directly against the
shared SQLite index, so it reports what MCP clients see. On failure the
helper reports only the exception type, as the tool does, and
``make status`` exits non-zero when mcp-server is not running, when the
check cannot run (its error output is shown), or when the helper reports
``status: error``; an index that is merely not current still exits zero.

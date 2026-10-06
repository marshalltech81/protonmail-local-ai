# Retrieval eval harness

A tiny opt-in test set that runs real queries against your real index
and reports Hit@10, MRR and evidence recall@10. The intent is to settle "is this knob
change actually helping?" arguments with measurements instead of
arguments.

## When to use this

- Before changing chunking, `THREAD_BODY_TEXT_MAX_TOKENS`, the
  embedding model, or the RRF fusion in `hybrid_search`.
- Before merging a search-layer change.
- When debugging "why did the LLM give the wrong answer?" — if the
  expected thread isn't in the top-10 retrieval, no LLM can save you.

## When NOT to use this

- Inside the regular CI suite. The eval is opt-in (see Run below).
  Default `pytest` runs never collect it, because there is no
  plausible mailbox in CI to evaluate against.

## Setup

1. Copy the template and fill in real thread ids from your index:

   ```bash
   cp tests/eval/queries.example.json tests/eval/queries.json
   ```

2. Find real `thread_id` values by calling `search_emails` (each result
   lists its `Thread ID`) or `list_threads` (each row lists its `ID`)
   through your MCP client against the running server. `make status`
   and `get_mailbox_status` report index health only, not thread ids.

3. Edit `queries.json` — each entry needs an `id`, the `search_query`
   to run, and its expected evidence from your actual index, in one of
   two forms:
   - `expected_thread_ids`: a list of alternatives, any one of which
     answers the question (usually a single thread id).
   - `required_evidence`: a list of groups for a question that needs
     several sources. Every group is required; any one thread id inside
     a group satisfies it. `[["A"], ["B", "C"]]` needs thread A plus
     either B or C. `expected_thread_ids: ["A", "B"]` is the same as
     `required_evidence: [["A", "B"]]`.

   Give one form per entry, not both. Other keys (such as `notes`) are
   ignored. Keep the file in `.gitignore` if your queries or
   thread ids are sensitive (the example file is tracked, your real
   queries file is not).

## Metrics

- **Hit@10** — the fraction of queries with at least one expected
  thread (from any group) in the top 10. Earlier versions of this
  harness printed it as "Recall@10".
- **MRR** — mean reciprocal rank of that first expected thread.
- **Evidence recall@10** — for each query, the fraction of its required
  groups with a thread in the top 10, averaged over queries. It equals
  Hit@10 when every query has one group; for a multi-source question a
  hit can still leave evidence missing, and only this number shows it.

The definitions live in `tests/retrieval_metrics.py`, shared with the
deterministic baseline in `tests/baseline/`. The per-query keyword and
hybrid tests pass only when every required group is in the top 10.

## Run

```bash
cd mcp-server
MCP_EVAL_DB=/path/to/mail.db uv run pytest -o addopts= -m eval tests/eval -s
```

`-o addopts=` drops the default options from `pyproject.toml`, which
exclude `tests/eval` (`--ignore=tests/eval`) and enforce the coverage
floor; with them, a plain `pytest -m eval` selects no tests. `-s` keeps
pytest from capturing the summary block printed by `test_eval_summary`.
Without `MCP_EVAL_DB`, every test skips.

## Comparing two configurations

The summary block ends with a per-query table of first-hit rank and
required groups found. To compare
"current" vs "after changing the RRF fusion in `hybrid_search`":

1. Run the eval, save the summary.
2. Apply the change and re-run. Changes to the search code itself
   (fusion, lane oversampling) take effect on the next run against the
   same index. Changes to what gets indexed (chunking,
   `THREAD_BODY_TEXT_MAX_TOKENS`, the embedding model) need a re-index
   first, and the eval run must use the same embedder as the index.
3. Diff the two summaries. Look at:
   - Aggregate Hit@10 — did it move at all?
   - MRR — did the right answer move closer to rank 1?
   - Evidence recall@10 — did multi-source questions gain or lose a
     source?
   - Per-query — did anything regress (rank got worse) while
     averages improved?

A change that improves an aggregate score but regresses any individual
query is suspicious — chase the regression before celebrating.

## Agent-level scoring (synthetic mailbox, in CI)

The harness above scores search. `tests/agent_metrics.py` scores what
an agent does with the tools, from a trace of its calls, and runs in
the default suite because it needs no mailbox or provider.

- `agent_scenarios.json` holds questions about the synthetic baseline
  mailbox (`indexer/tests/baseline/corpus.py`). Each names the tools an
  agent should reach for first, a call budget, and one golden question
  in `tests/baseline/golden.json` whose evidence, filters or
  enumeration answer it inherits; `make baseline` checks those answers
  against a real index. An unanswerable scenario names a golden
  `unanswerable` question instead, whose `absent_terms` `make baseline`
  checks occur nowhere in the synthetic Maildir.
- `agent_reference_traces.json` holds one scripted trace per scenario:
  the calls a good agent makes, each call's `structuredContent`
  (trimmed to the ID and paging fields), and the IDs its answer cites.
  `tests/test_agent_eval.py` requires each to score with no failures,
  and checks that scenario tools, expected arguments and trace
  arguments exist in the real tool signatures and that the fields the
  scorers read exist in the output models.

Per trace it reports tool selection (the first call used an expected
tool), argument accuracy (one call to an expected tool carried every
expected argument), evidence recall (required evidence groups with a
thread in any tool result), citation validity (cited IDs some tool
returned, including passage `chunk_id`s), citation recall (required
groups cited; a cited message or passage covers its thread), enumeration completeness (expected messages
listed by one `query_messages` cursor chain over exactly the expected
filters as the tool normalizes them (strings stripped, blank ones
absent, dates as UTC bounds), any page size, and whether that chain's last page said
`has_more: false`), message citation recall (see below), abstention
(see below), the counting metrics (see below), and calls over budget or
repeated. `summarize` prints
every aggregate, and the clean rate, separately for the dev and
held-out splits, then the failing scenarios by category with held-out
ones tagged. Failure cases for each scorer are
in `tests/test_agent_metrics.py`; `tests/test_agent_eval.py` also
mutates reference traces into the failures the new categories exist
to catch and checks each is caught.

Five categories need more than thread-level scoring (counting and
outstanding items have their own sections below):

- **Corrections** (`correction`): a later reply corrects an earlier
  message (the recital date in `t24`, the revised salary offer in
  `t17`). The scenario lists the correcting message in
  `required_citations`, and *message citation recall* requires the
  answer to cite it: a cited passage (`chunk_id`) or `claimant_id`
  counts as the message the tool result returned it with. Citing only
  the superseded message passes thread-level citation recall but fails
  here. Citing both is fine. `make baseline` checks that every
  `required_citations` ref is an indexed message in its thread.
- **Conflicting sources** (`conflicting_sources`): two messages
  disagree and neither supersedes the other (`t29.1` and `t30.2` give
  different block-party dates). `required_citations` lists one group per
  side, so the answer must cite both.
- **Unanswerable** (`unanswerable`): the mailbox holds no answer. The
  trace's answer marks abstention with `"abstained": true`, a
  structural flag recorded with the trace, not read from the prose. The
  scenario passes when three things hold. Some call's string argument
  contains one of the golden question's `absent_terms` (the agent
  asked for the missing fact; refusing after an unrelated search does
  not count). The answer abstains. It cites nothing, since a citation
  would present a near-miss source as support. Every other scenario
  fails if its answer abstains. Only a JSON `true` counts. List
  synonyms in `absent_terms` (matching is a case-insensitive
  substring), because a lookup that uses none of them fails.

### Counting (`counting`)

A "how many, and what were they" question, where the obvious lookups
also match messages that do not belong in the answer. It regresses a
real failure on an operator's mailbox (no content from it is in the
repository): asked about "TOFU" mail (one-time codes and email
verifications), an assistant counted keyword matches as relevant
messages (food mentioning tofu, repeated document-signing boilerplate,
general security advice), counted a message twice because it matched
two searches, read only the first page of a long body (`get_message`
returns 20,000-character pages with `next_offset`) and stated its
estimate too confidently.

`tofu-count` asks "How many TOFU emails did I get, and what were they
for?" over corpus threads 38-45 (`indexer/tests/baseline/corpus.py`):
four genuine messages (two one-time sign-in PINs, an email verification
that matches both a "PIN" and a "verification" lookup, and a long terms
notice whose PIN is on the second body page) and four decoys (a tofu
cooking class, two signing notices sharing access-PIN boilerplate, a
security newsletter). The corpus says "PIN" because "code" is reserved
for a vector-only golden question. A counting scenario names no golden
question; instead it lists:

- `expected_answer_messages`: message refs the answer must cite
  **exactly**: a decoy cited or a message missing fails *answer set
  exact*. A cited ID counts as the message a result returned it with, so
  a message cited twice (or by two IDs) counts once. The answer also
  records `"count"`, a JSON integer like `abstained`, which must equal
  the set's size (*answer count correct*); a missing, string, float or
  boolean count fails.
- `full_read_messages` (a subset of the above): for each, the trace's
  `get_message` results must cover the body from offset 0 through each
  `next_offset` to a page with none (*full-read recall*). Pages are read
  from the results, not the arguments, and a skipped page breaks the
  chain. Every call precedes the answer in a trace, so "before citing
  it" holds whenever the chain exists.
- `forbidden_answer_text`: strings (the PINs and the verification link)
  `answer.text` must not contain, case-insensitively (*forbidden text
  absent*). A value reformatted with spaces or dashes is not caught.

`make baseline` checks every ref is an indexed message, that each
full-read message holds a forbidden value only past its first
`get_message` page (read with the real tool), that every forbidden value
is in an expected message's body, and that `query_messages(text=...)`
for each of "tofu", "PIN" and "verification" lists at least one decoy
while the three together reach every genuine message.
`tests/test_agent_eval.py` mutates the reference trace into each
observed mistake (keyword matches counted, a decoy cited, a message
counted twice, a long body read to page 1 only, a wrong or missing
count, a PIN or the link repeated) and checks each is caught. The
`ask_mailbox` case `ask-tofu-summary` (below) covers summary accuracy
separately.

**What this case does not prove.**

- The reference trace is scripted. It shows the scorers catch these
  mistakes, not that a live agent avoids them; no recorder for a live
  client's trace exists yet (#283).
- Whether the answer explains its reading of "TOFU", and how confident
  it sounds, are prose, which the agent scorers do not grade. The
  answer-quality judge grades only `ask_mailbox` answers.
- Incomplete indexing cannot be exercised on the fully built baseline
  index, so whether an answer discloses it is untested.

### Outstanding items (`outstanding_items`, #798)

"What outstanding items do Avery Cole or Blair Reed owe me, including
anything that needs follow-up, since January 1, 2026?", asked on
2026-10-05 (America/New_York) about corpus threads 46-65: fourteen
matters of a fictional owners' association (a letter sent but
compliance unchecked, an invoice paid but its allocation open, half of
a two-part question answered, a closure in another thread from
management, a reopened matter, answers below a signature delimiter, an
attachment-only due date, a disputed status, a revised due date, a
phone call with no recorded outcome, an identity decoy, a prompt
injection) plus the paging, cap, extraction and date boundaries. The
held-out variant `marina-counsel-follow-ups` (threads 66-74) asks the
same about Sasha Ortiz and Emery Vance with different names, wording,
thread structure and evidence placement.

The ground truth is in `outstanding_items.json`, beside the scenarios
and never given to an answering agent (only the scenario's `question`
is). It is scored at the next-action level: each action has an owner,
a status (`open`, `waiting`, `disputed`, `unknown`, or `closed` to be
excluded), a due date only where the mail supports one, the messages a
conclusion must cite, superseded evidence, and `known_loss` where the
tools cannot return its evidence. The trace's answer records, like
`count` and `abstained`, structured fields: `items` (outstanding
actions, each naming its ground-truth `action` with `owner`, `status`,
`due` and `cited`), `excluded` (actions found closed, with `cited`),
`complete` (a claim that nothing was left unread) and `limitations`
(messages it could not read). Matching a live answer's prose to action
IDs is a labelling step this harness does not automate.

Scores (`tests/agent_metrics.py`): action recall and precision (a
duplicate item for one action, an item for a closed action or one
outside the truth is a false item; a `known_loss` action counts as
found when listed or when one of its sources is named in
`limitations`), owner and status accuracy, closures supported (an
outstanding action excluded or marked closed fails), deadlines
supported (a due date other than the truth's, a superseded one
included, fails; leaving one out does not), conclusion citation
support (each conclusion cites a required source of its action that
the trace read, and a superseded source is not one), required evidence
coverage (every required source the tools can return was read),
forbidden sources avoided (the decoy, the injection, mail outside the
window), full reads (as for counting), and completeness-claim
truthfulness (`complete: true` fails while a completeness blocker
exists, coverage is short or a full read is unfinished; otherwise every
blocker must be named in `limitations`).

A source counts as read only when a result returned its content, never
because a listing (`query_messages`, `list_threads`, `find_contact`,
`search_emails`) named it: its whole body through `get_message` paged
from offset 0 to the end, or a `get_thread` row whose body came back
uncut (`body_omitted_chars` 0; evidence past a cut needs `get_message`).
A source whose decisive text is in an attachment (an `attachment` item
in the truth's `evidence`) is read only through a `get_evidence`
passage with `source: attachment`: no tool returns a whole attachment
(#796), and a `search_attachments` snippet is a preview the scorer
cannot check holds the evidence, since traces carry IDs, not text.

The answer's top-level `cited` and its conclusions' own `cited` lists
must be the same set (*citations consistent*), and citation validity
and forbidden sources are scored over their union, so a citation left
out of either list, or a decoy cited in only one, still fails.

`tests/test_agent_eval.py` mutates the reference trace into each
failure the scenario exists to catch: a skipped page, a superseded due
date cited, a wrong owner, a closure because the letter went out, the
injection followed, the decoy merged, a quoted request counted twice,
the adopted policy counted as outstanding, completeness claimed despite
the failed extraction, a long message read to page 1, and citations
kept after the body reads or the attachment passage are dropped. Each
is caught.

Layer A, `tests/baseline/test_outstanding_items_baseline.py`, runs in
`make baseline` and checks on the built index which layers hold each
decisive passage (see `tests/baseline/README.md`).

**What this case does not prove.** The reference traces are scripted
from the real tools' output on the built index; they show the scorers
catch these mistakes, not that any agent avoids them. Two planted facts
are unreachable through every tool today, and the traces disclose them
as limitations instead of finding them: Blair's answers below the
`-- ` signature delimiter (t54.2) and Sasha's update inside a forward
(t67.1), both dropped from the indexed body (#795). The corpus cannot
express a delayed delivery (`occurred_at`): the answer-evaluation
runner's synthetic-index check requires every message's `occurred_at`
to be null, so the date boundary is tested on `sent_at` only.

### Held-out split

About one scenario in four is held out: `is_held_out` in
`tests/agent_metrics.py` takes the SHA-256 of the scenario ID modulo
4, so membership is fixed when a scenario is written and adding
scenarios never moves an existing one. Each row repeats its membership
as `held_out` (the loader rejects a row that disagrees), and
`test_held_out_split_is_stable` pins the current held-out IDs. Tune
prompts, tool descriptions and any future pass thresholds against the
dev split only. Report the held-out clean rate as the check on that
tuning, and do not change a held-out scenario because it fails. The
rule ignores category, so a category can sit entirely in one split
(today the only conflicting-sources scenario is held out).

These checks are deterministic and do not grade the answer: a valid
citation shows the agent saw the source, not that the source supports
the statement. `ask_mailbox` answers on the synthetic corpus are graded
by the answer-quality evaluation below; answers on your own mailbox stay
a manual grade (your `eval-queries.md`, which `.gitignore` keeps out of
the repository). No recorder for a live client's trace exists yet, so
the reference traces are the only traces scored today.

### Live agent runs (layer C): not implemented

No live tool-using agent harness exists (#283, #775, #798). Every
scored trace is scripted, so nothing here measures what a real agent
does with the tools; `make eval-answers` runs only `ask_mailbox`'s own
model, not an agent choosing tools.

The smallest extension, proposed and not built:

- A recorder: a pass-through MCP server in front of an mcp-server
  instance serving the synthetic baseline index (`make baseline`'s
  build, never a real mailbox). For each `tools/call` it appends the
  tool name, arguments and the result's `structuredContent` to a trace
  in the format above, and nothing else.
- An opt-in make target (for example `make eval-agent`) that builds the
  index, starts the server and recorder on loopback, and lets the
  operator point a tool-using client at it with a scenario's
  `question`. Provider calls happen only under that target, through the
  client's own configured model; CI never runs it.
- The answer's structured fields (`items`, `excluded`, `complete`,
  `limitations`, `count`, `abstained`) are labelled by hand after the
  run (or by a later structured-output step), then the trace is scored
  by `score_trace`. Recorded traces stay git-ignored, like
  `.answer-eval/`.

## Answer-quality evaluation (`ask_mailbox`, synthetic corpus)

`tests/answer_eval/` runs the real `ask_mailbox` handler, captures what
its model actually received, and grades the answer twice: deterministic
checks first, then an optional, separately configured AI judge. It is an
offline development tool (#604): it changes nothing in the server, the
containers or the tool outputs.

**Synthetic data only.** The runner refuses any index that is not the
committed synthetic corpus: its claimant IDs must be exactly those
computed from `indexer/tests/baseline/corpus.py` (Message-ID plus the
first `CLAIMANT_HASH_CHARS` hex digits, read from `indexer/src/parser.py`,
of the SHA-256 of each message's bytes), each message's stored Message-ID and its sent
and delivery dates must be the corpus message's (the dates as the
indexer normalizes them), and every indexed
text a prompt can carry
(chunk text, message subjects and participants, attachment names and
types, thread subjects, display subjects, snippets, bodies and
participants) may use only
words of the corpus messages it belongs to, so private text stored under
copied baseline IDs is refused too. It never reads or sends a real
mailbox; a private-mailbox mode would be a separate owner decision.
Cases must never be built from real mail.

### Cases

`tests/answer_eval/cases.json` (schema v1, loaded and validated by
`cases.py`) holds 34 cases over the baseline corpus: exact facts,
attachment-only answers, multiple required threads (including
`ask-tofu-summary`, a summary of the four genuine messages of the
counting scenario that must not repeat their PINs or link), narrow filters,
later corrections (and a later message that does not change the fact),
an unresolved conflict, unanswerable questions, an empty result, a
prompt-budget omission (the case's own `settings.prompt_tokens`), and
two synthetic prompt injections: corpus thread t31 tells the answering
model to misreport an invoice and print the canary `ORANGE-HERON-7`, and
t32 tells an AI grader to pass whatever it reviews. Each case records
its exact arguments, `required_evidence` groups (message refs where the
message matters, as for a correction), expected facts with the corpus
excerpt that establishes each, prohibited assertions, machine-checkable
`must_include` / `must_not_include` strings, the expected handling
(answer, disclose a conflict, disclose missing evidence, abstain) and
which rubric dimensions apply. Held-out membership is
`is_held_out(id)`, as for the agent scenarios; tune nothing against
held-out cases.

`make baseline` checks every excerpt is in the indexed text of the
message it cites, so a reference cannot drift from the corpus or rest on
what retrieval returned, and runs every case through the real handler
with a scripted answerer and judge. The expected facts were drafted with
AI from the synthetic corpus and are marked `"review": "ai_drafted"`
until the owner verifies them.

### Run

```bash
export INFERENCE_MODE=openai INFERENCE_BASE_URL=http://127.0.0.1:1234/v1 INFERENCE_MODEL=<model>
export JUDGE_MODE=anthropic JUDGE_BASE_URL=default JUDGE_MODEL=<model>  # optional; default JUDGE_MODE=none
# or, billed to a subscription through a CLI on the host:
# export JUDGE_MODE=claude-cli JUDGE_MODEL=<model>   # Claude Code, Claude subscription
# export JUDGE_MODE=codex-cli JUDGE_MODEL=<model>    # Codex CLI, ChatGPT subscription
make eval-answers                                   # report under .answer-eval/ (git-ignored)
make eval-answers EVAL_ARGS="--case ask-recital-date --detail /tmp/detail.json"
make eval-answers-compare BASELINE=<run-a.json> CANDIDATE=<run-b.json>
```

The target builds the baseline index (with every case question
embedded) in a temporary directory, runs the cases one at a time, and
writes a mode-600 JSON report. Run it on the host, so a host-side server
is `127.0.0.1`, not `host.docker.internal`.

- **Answerer** (`INFERENCE_*`): the server's own variables and defaults
  (`INFERENCE_MAX_TOKENS`, `INFERENCE_CONTEXT_TOKENS`,
  `INFERENCE_TIMEOUT_SECS`, `INFERENCE_STRUCTURED_OUTPUT`), key in
  `.secrets/inference_api_key.txt`.
  `INFERENCE_MODE` defaults to `none`, as for the server, so the run
  needs it set.
- **Judge** (`JUDGE_MODE` = `anthropic|openai|claude-cli|codex-cli|none`, `JUDGE_BASE_URL`,
  `JUDGE_MODEL`, key in `.secrets/judge_api_key.txt`, mode 600, or
  `JUDGE_API_KEY` for local development only). Same contract as the
  server's layers: an enabled judge needs a model and a non-empty key (a
  placeholder for an unauthenticated host-side server), and a base URL:
  the endpoint, or `default` for the SDK default (a remote provider);
  an empty one is refused (#750). It never reads the
  answerer's variables or key. Bounds: `JUDGE_TIMEOUT_SECS` (120),
  `JUDGE_MAX_TOKENS` (2048), `JUDGE_MAX_INPUT_CHARS` (60,000), one call
  per case, no retries, one case at a time.
- **Subscription judges** (`JUDGE_MODE=claude-cli` or `codex-cli`, #806):
  each judge call runs a vendor CLI once, so it uses the subscription's
  limits, which interactive use shares, instead of API credit. They run
  on macOS or Linux only (the timeout kills the CLI's process group).
  Both
  need `JUDGE_MODEL` and the CLI on `PATH`, read no key, take no base
  URL (unset or `default`), send the prompt on stdin, run in a fresh
  empty directory, and are killed with every process they started (the
  CLI leads its own process group) when `JUDGE_TIMEOUT_SECS` runs out.
  Each CLI inherits only an allowlist of the caller's environment (path,
  home, user, locale, temporary directory, proxy and certificate
  variables, including `NODE_EXTRA_CA_CERTS` for a TLS-inspecting proxy;
  Claude also keeps `CLAUDE_CONFIG_DIR`; path values are made absolute),
  so API keys,
  provider switches, reasoning settings such as
  `CLAUDE_CODE_EFFORT_LEVEL` and telemetry exporters (`OTEL_*`) never
  reach a judge call. A
  logged-out CLI and a usage-limit stop are the judge errors
  `judge_cli_logged_out` and `judge_cli_usage_limit`; a CLI missing from
  `PATH` or not logged in with a subscription is a configuration error.
  The judge identity records `cli` and `cli_version` (and, for Claude,
  the `served_models` the CLI reports), so a different CLI, version or
  model is a different judge to `compare`. Running the same stored
  answers through an API judge and both CLIs shows where judges
  disagree (#655).
- **`claude-cli`** runs `claude -p` (Claude Code, logged in with a Claude
  subscription) with no tools (`--tools ""`), no settings, `CLAUDE.md`
  files, hooks or plugins (`--setting-sources ""`), no MCP servers
  (`--strict-mcp-config`), no saved session, and the judge system
  prompt. `--bare` is not used
  because it accepts only an API key. A set `ANTHROPIC_API_KEY` would
  take the call off the subscription (the allowlist keeps it out), and
  `DISABLE_AUTOUPDATER=1` keeps one CLI version for the whole run.
  Claude Code before 2.1.211 is refused, since its `--setting-sources ""`
  still loaded nested `.claude/rules` files, and a relative
  `CLAUDE_CONFIG_DIR` is made absolute before the calls change directory. Before any case runs, `claude auth
  status` must report a claude.ai (subscription) login, not a Console
  (API-billed) one. A machine with an enterprise `managed-mcp.json` is
  refused, since Claude Code exits on `--strict-mcp-config` there, and so
  is one with an organization-wide managed `CLAUDE.md` or any
  `managed-settings.json` (its `claudeMd`, hooks and other settings
  apply to every session whatever the flags). Managed settings delivered by MDM or from Anthropic's servers
  cannot be seen locally, so on a managed machine the judge may still
  carry organization instructions.
  `JUDGE_MAX_TOKENS` becomes `CLAUDE_CODE_MAX_OUTPUT_TOKENS`: when a
  reply reaches it, the CLI makes its own continuation attempts (up to
  about four times the cap) before failing, which counts as
  `judge_truncated`. `ANSWER_EVAL_LIVE_CLAUDE=1 uv run pytest
  tests/test_answer_eval_cli_judge.py -k live_canaries` makes one real
  call to check that planted `CLAUDE.md`/`AGENTS.md` files and an
  `ANTHROPIC_API_KEY` never reach it.
- **`codex-cli`** runs `codex exec` (Codex CLI, logged in with ChatGPT).
  The shell and every tool or extension that could read the disk or
  reach the network are turned off (`--disable shell_tool`,
  `unified_exec`, browser, computer use, apps, plugins, hooks, images,
  sub-agents including `multi_agent_v2`, bundled skills; web search
  disabled). Codex older than 0.160.1, the version these flags were
  checked against, is refused at startup, as is a prerelease of 0.160.1
  itself (likewise Claude Code below 2.1.211). `apply_patch` cannot be removed, but
  it only writes and `-s read-only` refuses the write. Each call gets a
  private mode-700 `CODEX_HOME` holding only a symbolic link to the
  operator's `auth.json` (the login is linked, never copied), because
  `--ignore-user-config` still loads the global `~/.codex/AGENTS.md`;
  the real home's config, hooks, plugins and skills stay out, and
  Codex's own logs of the call are removed with that directory. The
  working directory's `AGENTS.md` is off (`project_doc_max_bytes=0`), as
  are rules, session files and the update check; credentials come only
  from the linked file (`cli_auth_credentials_store="file"`), and the
  judge system
  prompt replaces Codex's coding-agent instructions
  (`model_instructions_file`), with its permissions, collaboration-mode
  and environment-context blocks and the `request_user_input` tool off. Managed and system Codex config (`/etc/codex/managed_config.toml`,
  `/etc/codex/config.toml`, macOS managed preferences) is refused, since
  it applies whatever the flags say and can add MCP servers; a
  cloud-managed enterprise layer cannot be seen locally. Before any case
  runs, `codex login status`, run in the same kind of private home with
  the same file store, must report a ChatGPT login: an API-key login bills API usage, and a logged-out CLI
  still sends the prompt before the server refuses it. A login kept in
  the keyring instead of `auth.json` is refused, since it cannot be
  linked. Codex has no output-token setting, so `JUDGE_MAX_TOKENS` is not
  read (the identity records `max_tokens: null`) and the timeout
  bounds the call. `ANSWER_EVAL_LIVE_CODEX=1 uv run pytest
  tests/test_answer_eval_cli_judge.py -k live_codex` makes one real call
  to check that planted `AGENTS.md` files, the global `AGENTS.md` and an
  `OPENAI_API_KEY` never reach it and that it cannot read a file.
- Either layer with the base URL `default` is refused while the SDK's own
  endpoint variable (`OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`) is set, so
  the report's `sdk-default` label is never a custom endpoint.
- Each case runs under `--case-timeout-secs` (900) and the whole run
  under `--max-runtime-secs` (3600): every answer and judge call is
  capped by what is left of it, and cases past it are `skipped`. Both
  must be finite numbers greater than 0 (a configuration error
  otherwise).

Retrieval uses the baseline's hashed embedder (query vectors precomputed
at build time) and no reranker, so a run measures prompt assembly,
inference and the judge on a frozen corpus and index. The hashed
embedder has no semantics: one question (`ask-lisbon-dates`) misses
its thread, which the report attributes to retrieval. A real-model synthetic index is a follow-up.

### What is captured and graded

Two narrow wrappers capture each run in memory: the inference client
(every request and reply, so the evidence is the prompt actually sent
after truncation, deduplication, fallback thread text and budgeting; a
repair call resends that prompt with a fixed instruction) and
`_build_evidence`'s label map (each label's thread, message, claimant
and chunk). A check confirms every captured label is in the prompt the
model received.

Deterministic checks (`graders.py`), never overridden by the judge:
answer not cut off, capture consistent, cited labels resolve to
supplied passages, the tool's citation and quote checks pass, every
required evidence group cited, `must_include` present as a whole value
(`4,860` does not match `14,860` or `4,860,000`; an ordinal suffix or
`.00` may follow a number), `must_not_include` absent anywhere, and
abstention exactly when the case is unanswerable (citing
nothing). Each evidence group is also scored as retrieved, supplied to
the prompt and cited, so a failure is attributed to `retrieval`,
`prompt_assembly`, `synthesis`, `evaluator_infrastructure` or
`answer_infrastructure` (several may apply; `unknown` otherwise).

The judge (`judge.py`, rubric `ask-rubric-3`) receives the question,
expected handling, reference facts, prohibited assertions, which
dimensions apply, every passage the answerer received, the answer and
the answer's structured `statements` numbered from 1; the passages, the
answer and each statement sit in `<untrusted_evidence>` /
`<untrusted_answer>` blocks they cannot close, under a system prompt
that tells the judge to ignore instructions inside them. It returns a
JSON verdict: per claim the number of the statement it comes from
(`statement`), the labels that statement cites (`cited`) and
`supported | contradicted | insufficient_evidence` judged only against
the cited passages (**groundedness**), per reference fact covered or not and per
prohibited assertion asserted or not (**correctness**), and `pass |
fail | not_applicable` for factual correctness, citation support,
completeness, temporal reasoning, conflict/uncertainty and relevance. A
claim that matches the reference but not its citations is still
unsupported. The verdict is validated: a claim whose `statement` is not
the number of one of the answer's statements, or whose labels that
statement does not all cite (supplied passages only), so the judge
cannot support one statement with a passage cited for another, missing
facts or dimensions, an applicable dimension marked not applicable, no
claims for a non-abstaining answer, malformed output, a timeout, a
cut-off reply, a provider failure or input over the limit are explicit
judge errors, never passes. The injection cases test this hardening;
they do not prove immunity.

### Reports and privacy

The report holds opaque case IDs, categories, check results, fixed
error categories, counts, rates per split and category, timings and
safe identity labels: source commit, case-file and index hashes, schema
and rubric versions, provider mode/model and whether each endpoint is
host-local, remote or the SDK default, plus a 12-character hash of a
configured base URL so two endpoints are told apart (never a URL or
key). Every rate's denominator is the selected cases (for evidence
coverage, those that need evidence; for dimension and missing-fact
rates, every applicable dimension and expected fact), so errors, skips
and unjudged answers count as failures and never improve a score. Token usage is not exposed by the inference client and no cost is
computed. `--detail` writes a separate mode-600 artifact with the
content (answers, passages, prompts, judge claims and explanations);
both refuse a path inside the repository other than `.answer-eval/`.
Delete old runs with `rm -r .answer-eval`. Never upload either.

Exit codes: `run` 0 complete, 2 incomplete (any error, skip or judge
error), 3 configuration error; `compare` 0, 1 on a per-case regression
with `--fail-on-regression`, 2 when the runs differ in case file, case
selection, index, rubric or judge (not comparable) unless `--allow-incompatible`,
3 when a report is unreadable or malformed. Scores
are advisory: no quality threshold is calibrated yet, so a low score
never fails a run. CI runs only the scripted path (`make baseline` and
`tests/test_answer_eval.py`), with no provider or credential.

Not yet covered (follow-ups): judge calibration against human labels
and repeated runs to measure variation, quality thresholds, other
intelligence tools, a real-model synthetic index, and token usage.

## What this harness does NOT do

- The retrieval harness above does not run `ask_mailbox` or grade LLM
  answers; the answer-quality evaluation does, on the synthetic corpus
  only. Prompt-side settings such as `PER_THREAD_CHAR_BUDGET` only
  shape the context sent to the model after retrieval, so retrieval
  scores cannot measure them.
  Retrieval-only is the load-bearing piece — if the right thread shows
  up in the top-K, the LLM has the material it needs.
- It does not auto-discover queries from your mailbox. The point is
  for *you* to curate questions you have actually asked or expect
  to ask, with known correct answers.
- It does not enforce a passing threshold. Per-query tests pass when
  every required group is in the top 10 and fail otherwise; the
  aggregate summary always passes. CI does not enforce a floor on any
  of the three scores because the right floor is mailbox-dependent.

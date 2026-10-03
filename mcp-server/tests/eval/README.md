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
(see below), and calls over budget or repeated. `summarize` prints
every aggregate, and the clean rate, separately for the dev and
held-out splits, then the failing scenarios by category with held-out
ones tagged. Failure cases for each scorer are
in `tests/test_agent_metrics.py`; `tests/test_agent_eval.py` also
mutates reference traces into the failures the new categories exist
to catch and checks each is caught.

Three categories need more than thread-level scoring:

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

## Answer-quality evaluation (`ask_mailbox`, synthetic corpus)

`tests/answer_eval/` runs the real `ask_mailbox` handler, captures what
its model actually received, and grades the answer twice: deterministic
checks first, then an optional, separately configured AI judge. It is an
offline development tool (#604): it changes nothing in the server, the
containers or the tool outputs.

**Synthetic data only.** The runner refuses any index whose messages are
not the synthetic baseline's (`@baseline.example`), so it never reads or
sends a real mailbox; a private-mailbox mode would be a separate owner
decision. Cases must never be built from real mail.

### Cases

`tests/answer_eval/cases.json` (schema v1, loaded and validated by
`cases.py`) holds 33 cases over the baseline corpus: exact facts,
attachment-only answers, multiple required threads, narrow filters,
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
export JUDGE_MODE=anthropic JUDGE_MODEL=<model>     # optional; default JUDGE_MODE=none
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
  `INFERENCE_TIMEOUT_SECS`), key in `.secrets/inference_api_key.txt`.
- **Judge** (`JUDGE_MODE` = `anthropic|openai|none`, `JUDGE_BASE_URL`,
  `JUDGE_MODEL`, key in `.secrets/judge_api_key.txt`, mode 600, or
  `JUDGE_API_KEY` for local development only). Same contract as the
  server's layers: an enabled judge needs a model and a non-empty key (a
  placeholder for an unauthenticated host-side server), and an empty
  base URL means the SDK default (a remote provider). It never reads the
  answerer's variables or key. Bounds: `JUDGE_TIMEOUT_SECS` (120),
  `JUDGE_MAX_TOKENS` (2048), `JUDGE_MAX_INPUT_CHARS` (60,000), one call
  per case, no retries, one case at a time.
- Each case runs under `--case-timeout-secs` (900) and the whole run
  under `--max-runtime-secs` (3600); cases past it are `skipped`.

Retrieval uses the baseline's hashed embedder (query vectors precomputed
at build time) and no reranker, so a run measures prompt assembly,
inference and the judge on a frozen corpus and index. The hashed
embedder has no semantics: two questions (`ask-hotel-checkin`,
`ask-lisbon-dates`) miss their thread, which the report attributes to
retrieval. A real-model synthetic index is a follow-up.

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
required evidence group cited, `must_include` present, `must_not_include`
absent, and abstention exactly when the case is unanswerable (citing
nothing). Each evidence group is also scored as retrieved, supplied to
the prompt and cited, so a failure is attributed to `retrieval`,
`prompt_assembly`, `synthesis`, `evaluator_infrastructure` or
`answer_infrastructure` (several may apply; `unknown` otherwise).

The judge (`judge.py`, rubric `ask-rubric-1`) receives the question,
expected handling, reference facts, prohibited assertions, which
dimensions apply, every passage the answerer received and the answer;
the passages and the answer sit in `<untrusted_evidence>` /
`<untrusted_answer>` blocks they cannot close, under a system prompt
that tells the judge to ignore instructions inside them. It returns a
JSON verdict: per claim `supported | contradicted |
insufficient_evidence` judged only against the cited passages
(**groundedness**), per reference fact covered or not and per
prohibited assertion asserted or not (**correctness**), and `pass |
fail | not_applicable` for factual correctness, citation support,
completeness, temporal reasoning, conflict/uncertainty and relevance. A
claim that matches the reference but not its citations is still
unsupported. The verdict is validated: unknown evidence labels, missing
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
host-local, remote or the SDK default (never a URL or key). Every rate's
denominator is the selected cases, so errors and skips never improve a
score. Token usage is not exposed by the inference client and no cost is
computed. `--detail` writes a separate mode-600 artifact with the
content (answers, passages, prompts, judge claims and explanations);
both refuse a path inside the repository other than `.answer-eval/`.
Delete old runs with `rm -r .answer-eval`. Never upload either.

Exit codes: `run` 0 complete, 2 incomplete (any error, skip or judge
error), 3 configuration error; `compare` 0, 1 on a per-case regression
with `--fail-on-regression`, 2 when the runs differ in case file, index,
rubric or judge (not comparable) unless `--allow-incompatible`. Scores
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

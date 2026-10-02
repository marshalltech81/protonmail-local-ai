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
(see below), and calls over budget or repeated. `summarize` prints the
aggregates, the clean rate of each split, and the failing scenarios by
category with held-out ones tagged. Failure cases for each scorer are
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
  scenario passes when the answer abstains and cites nothing, since a
  citation would present a near-miss source as support. Every other
  scenario fails if its answer abstains. Only a JSON `true` counts.

The agent should still search before abstaining, so tool selection
applies to unanswerable scenarios as usual.

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
the statement. Answer quality stays a manual grade (your
`eval-queries.md`, which `.gitignore` keeps out of the repository). No
recorder for a live client's trace exists yet, so the reference traces
are the only traces scored today.

## What this harness does NOT do

- It does not run `ask_mailbox` end-to-end or grade LLM answers.
  That requires a live inference call per query and a way to judge
  answer quality, which is a separate problem. Prompt-side settings
  such as `PER_THREAD_CHAR_BUDGET` only shape the context sent to the
  model after retrieval, so this harness cannot measure them.
  Retrieval-only is the load-bearing piece — if the right thread shows
  up in the top-K, the LLM has the material it needs.
- It does not auto-discover queries from your mailbox. The point is
  for *you* to curate questions you have actually asked or expect
  to ask, with known correct answers.
- It does not enforce a passing threshold. Per-query tests pass when
  every required group is in the top 10 and fail otherwise; the
  aggregate summary always passes. CI does not enforce a floor on any
  of the three scores because the right floor is mailbox-dependent.

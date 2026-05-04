# Retrieval eval harness

A tiny opt-in test set that runs real queries against your real index
and reports Recall@10 and MRR. The intent is to settle "is this knob
change actually helping?" arguments with measurements instead of
arguments.

## When to use this

- Before changing `PER_THREAD_CHAR_BUDGET`, `THREAD_BODY_TEXT_MAX_CHARS`,
  the embedding model, or RRF weights.
- Before merging a search-layer change.
- When debugging "why did the LLM give the wrong answer?" — if the
  expected thread isn't in the top-10 retrieval, no LLM can save you.

## When NOT to use this

- Inside the regular CI suite. The eval is opt-in via
  `pytest -m eval`. Default `pytest` runs skip it because there is no
  plausible mailbox in CI to evaluate against.

## Setup

1. Copy the template and fill in real thread ids from your index:

   ```bash
   cp tests/eval/queries.example.json tests/eval/queries.json
   ```

2. Find real `thread_id` values via `make status` or by calling
   `get_index_status` / `search_emails` against your running MCP server.

3. Edit `queries.json` — each entry needs `expected_thread_ids` from
   your actual index. Keep the file in `.gitignore` if your queries or
   thread ids are sensitive (the example file is tracked, your real
   queries file is not).

## Run

```bash
cd mcp-server
MCP_EVAL_DB=/path/to/mail.db uv run pytest -m eval -s
```

`-s` keeps pytest from capturing the summary block printed by
`test_eval_summary`. Without `MCP_EVAL_DB`, every test skips.

## Comparing two configurations

The summary block ends with a per-query rank table. To compare
"current" vs "after raising PER_THREAD_CHAR_BUDGET to 6000":

1. Run the eval, save the summary.
2. Apply the change, re-index (or wait for re-embed if your change
   only affects search-time params), re-run.
3. Diff the two summaries. Look at:
   - Aggregate Recall@10 — did it move at all?
   - MRR — did the right answer move closer to rank 1?
   - Per-query — did anything regress (rank got worse) while
     averages improved?

A change that improves aggregate Recall but regresses any individual
query is suspicious — chase the regression before celebrating.

## What this harness does NOT do

- It does not run `ask_mailbox` end-to-end or grade LLM answers.
  That requires a live mlx-lm-server (or Claude) call per query and a way to
  judge answer quality, which is a separate problem. Retrieval-only
  is the load-bearing piece — if the right thread shows up in the
  top-K, the LLM has the material it needs.
- It does not auto-discover queries from your mailbox. The point is
  for *you* to curate questions you have actually asked or expect
  to ask, with known correct answers.
- It does not enforce a passing threshold. Per-query tests pass when
  the expected thread is in top-10 and fail otherwise; the aggregate
  summary always passes. CI does not enforce a Recall@10 floor
  because the right floor is mailbox-dependent.

# Retrieval regression baseline

A deterministic check that indexing and retrieval still behave the
same. It needs no network, no API keys and no real mail.

```bash
make baseline            # build + check
make baseline UPDATE=1   # rewrite snapshot.json after an intended ranking change
```

## How it works

1. **Build** (`indexer/tests/baseline/build.py`, indexer environment).
   The script writes the synthetic Maildir from
   `indexer/tests/baseline/corpus.py` and indexes it with the real
   `initial_index`, using a hashed embedder (words plus character
   trigrams). It writes `mail.db` and `query_vectors.json`.
2. **Check** (`test_retrieval_baseline.py`, mcp-server environment,
   `BASELINE_DIR` set). This step runs every question in `golden.json`
   through `hybrid_search` (no reranker) and `query_messages`:
   - **Golden checks, for correctness.** For each question, the
     expected thread must be at or above `max_rank`. Questions with an
     evidence substring, and vector-only questions, add their own
     checks. MRR must meet `floors.mrr`. Enumeration questions must
     return the exact message set.
   - **Rank snapshot, for unchanged behaviour.** The top-10 order of
     every search question must match `snapshot.json`.

The hashed embedder has no sense of meaning, so the baseline catches
broken plumbing (ingestion, schema, lanes, fusion, filters). It does
not measure semantic quality.

## Editing

- **Corpus.** Use invented names and reserved `.example` domains only.
  The repository is public, so never copy real mailbox content. Keep
  the words named in `corpus.py`'s docstring unique to their threads.
  Vector-only questions depend on them.
- **Vector-only questions** need a query that porter stemming does not
  map back to a corpus word. "maintenence" does not qualify, because
  it stems to the same form as "maintenance".
- **After changing the corpus or golden set**, run `make baseline
  UPDATE=1` and commit the regenerated `snapshot.json` together with
  the change.
- **Unexpected snapshot diff.** If a refactor PR produces a snapshot
  diff, treat it as a behaviour change and explain it in the PR. Do
  not regenerate the snapshot just to make the test pass.

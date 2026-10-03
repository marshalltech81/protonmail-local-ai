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
   - **Golden checks, for correctness.** Each question lists its
     `required_evidence` as groups: every group is required, and any
     one thread in a group satisfies it (`[["t21"], ["t22"]]` needs
     both threads; `[["t27", "t28"]]` accepts either). The first hit
     from any group must be at or above `max_rank`. Questions with an
     evidence substring, and vector-only questions, add their own
     checks. MRR must meet `floors.mrr`. Evidence recall@10, the
     fraction of a question's groups found in the top 10, must meet
     `floors.evidence_recall_at_10` over all questions and
     `floors.multi_source_evidence_recall_at_10` over the questions
     with more than one group. Enumeration questions must return the
     exact message set. Unanswerable questions list `absent_terms`
     that must occur in no message's subject, body or attachment; the
     abstention scenarios in `tests/eval/agent_scenarios.json` rest on
     them.
   - **Rank snapshot, for unchanged behaviour.** The top-10 order of
     every search question must match `snapshot.json`.
   - **Answer-evaluation cases** (`test_answer_eval_cases.py`). The
     build also embeds every question in
     `tests/answer_eval/cases.json`. Each case's fact excerpts must be
     in the indexed text of the messages they cite, and every case runs
     through the real `ask_mailbox` handler with a scripted answerer and
     judge (no network); see `tests/eval/README.md`.

The hashed embedder has no sense of meaning, so the baseline catches
broken plumbing (ingestion, schema, lanes, fusion, filters). It does
not measure semantic quality.

The recall floors are the values the hashed embedder reaches, rounded
down, so losing any one required source fails them. They are wiring
checks, not quality targets: `multi-lisbon-trip` finds the flight
thread but not the hotel thread, which has only "Lisbon" in common
with the query, and the multi-source floor records that. Hit rate,
MRR and evidence recall share their definitions with the opt-in eval
harness in `tests/eval/` (`tests/retrieval_metrics.py`).

## Editing

- **Corpus.** Use invented names and reserved `.example` domains only.
  The repository is public, so never copy real mailbox content. Keep
  the words named in `corpus.py`'s docstring unique to their threads.
  Vector-only questions depend on them.
- **Multi-source questions** (threads 21-28) cover a fact split across
  two threads, the same fact in either of two threads, an answer only
  in an attachment and a correction in a later reply. Threads 29-30
  are conflicting sources (two messages give different dates, neither
  superseding the other). Threads 31-32 carry synthetic prompt
  injections for the answer-quality evaluation (one aimed at the
  answering model, one at an AI grader). Adding a thread can lower a recall floor's
  measured value; re-measure and explain it rather than lowering the
  floor silently.
- **Unanswerable questions** need terms that appear nowhere in the
  corpus. A new thread must not use them. They double as the words an
  abstaining agent must have asked for, so list the common synonyms.
- **Vector-only questions** need a query that porter stemming does not
  map back to a corpus word. "maintenence" does not qualify, because
  it stems to the same form as "maintenance".
- **Agent scenarios** in `tests/eval/agent_scenarios.json` name golden
  questions by `id` and inherit their evidence, filters and enumeration
  answers. Renaming or removing a golden question fails
  `tests/test_agent_eval.py` until the scenario is updated.
- **After changing the corpus or golden set**, run `make baseline
  UPDATE=1` and commit the regenerated `snapshot.json` together with
  the change.
- **Unexpected snapshot diff.** If a refactor PR produces a snapshot
  diff, treat it as a behaviour change and explain it in the PR. Do
  not regenerate the snapshot just to make the test pass.

# Retrieval regression baseline

A deterministic check that indexing and retrieval still behave the
same. It needs no network, no API keys and no real mail.

```bash
make baseline            # build + check
make baseline UPDATE=1   # rewrite snapshot.json after an intended ranking change
```

The build runs OCR on three of the corpus's attachments (threads 90-92,
#908, #1113), so it needs Tesseract and Poppler on `PATH`: `brew install
tesseract poppler` on macOS (CI installs `tesseract-ocr poppler-utils`).
Without them the build stops with a message naming the missing binary
(`tesseract`, `pdftoppm` or `pdfinfo`) rather than recording the shapes
as failed extractions.

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
   - **Agent scenario references.** Every message a scenario names must
     be indexed in its thread. For a counting scenario, each full-read
     message must hold a forbidden value only past its first
     `get_message` body page (read with the real tool), every forbidden
     value must be in an expected answer message, and the obvious
     `query_messages(text=...)` lookups must list at least one decoy
     while reaching every expected message.
   - **Outstanding-items reachability**
     (`test_outstanding_items_baseline.py`, #798). For each decisive
     passage in `tests/eval/outstanding_items.json`, the layers that
     hold it on the built index must match the recorded matrix: the
     `.eml`, the thread text (`threads.body_text`: 2,000 characters per
     message, 4,000 tokens per thread), the message's chunks,
     `get_message` (all pages), `get_thread` (all pages, bodies cut at
     4,000 characters) and, for attachments, `get_evidence` and
     `search_attachments`. The file's `corpus_evidence` holds passages
     of corpus shapes outside any scenario, checked the same way (#907:
     t89's sentence past the build's extracted-characters cap). A
     passage no tool returns is a strict xfail naming its issue (#795,
     or #907 for that sentence), so a fix flips it. Boundary checks:
     a Blair Reed participant lookup of more than 100 messages with
     required evidence on page 2, a 56-message thread with the decisive
     message past the first `get_thread` page and past the thread text,
     a report past `get_thread`'s body cut, a message past the first
     `get_message` page, a quoted request that a text lookup lists only
     where it was written, an address lookup that keeps an identity
     decoy apart while a display-name lookup merges it, a December 31
     New York send that a date-only bound counts as 2026 (UTC) and a
     New York-midnight bound does not, a failed PDF extraction
     (listed, no text), and an attachment-only due date the body tools
     never show. `golden.json`'s `evidence_queries` are the
     `get_evidence` queries it embeds at build time.
   - **Rank snapshot, for unchanged behaviour.** The top-10 order of
     every search question must match `snapshot.json`, and must not
     change when any two adjacent vector distances closer than 1e-6
     are swapped, inside a lane or across its `k` cutoff: sqlite-vec's
     float32 rounding differs between macOS and Linux, so an order that
     rests on such a tie passes locally and fails in CI. If a corpus edit trips this, reword the new text
     until the snapshot no longer depends on the tie.
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
checks, not quality targets. Both are 1.0 since the keyword slot
(#701): before it, `multi-lisbon-trip` found the flight thread but not
the hotel thread, which has only "Lisbon" in common with the query.
Hit rate,
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
  answering model, one at an AI grader). Thread 33 names its topic
  only in its subject (#687). Thread 34 names its sender only in the
  From header, and threads 35-37 mention that first name once in their
  bodies (#701). Threads 38-45 back the counting scenario `tofu-count`
  (#283): four genuine one-time PIN and email verification messages
  (t41's PIN is past the first 20,000-character `get_message` page)
  and four decoys sharing their obvious words (tofu, access-PIN
  boilerplate, security advice). Threads 46-74 back the
  outstanding-items scenarios (#798): an owners' association's
  correspondence with its attorneys in 2026 (dev, 46-65) and a marina
  co-op's (held out, 66-74). Because they add 2026 sent mail and
  attachments, the `folder-sent` and `has-attachments` enumerations
  are bounded to before 2026. Threads 75-77 back the answer
  evaluation's evidence-scope decoys (#755): in each, a filter selects
  the thread and another of its messages holds a different answer
  (another sender's reply, a later month's schedule, a stale list in
  Trash). Threads 78-81 pin three attachment shapes (#906): a real
  digital PDF whose fact is only in the attachment, one payload under
  two filenames in two threads (one `attachment_id`, one extraction
  row, a cache hit), and a PDF under a `.txt` filename that is
  extracted by MIME type (`test_attachment_shapes_baseline.py`).
  Threads 82-87 pin six more (#909): a DOCX and an XLSX (its fact on
  the second sheet), a JSON attachment no extractor reads
  (`unsupported`, found by filename), a whitespace-only attachment
  (`empty`), an attached email carrying its own attachment, and an
  RFC 2231 non-ASCII filename (`test_attachment_formats_baseline.py`).
  Threads 88-89 pin two capped attachments (#907): one over the size
  cap (`too_large`, found by filename, never evidence) and one cut at
  the extracted-characters cap, whose last sentence is a known loss in
  `tests/eval/outstanding_items.json` `corpus_evidence`
  (`test_capped_attachments_baseline.py`). The build lowers both caps,
  `INDEXER_ATTACHMENT_MAX_BYTES` to 64 KiB and
  `INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS` to 20,000 (production: 32 MB
  and 2,000,000), so these results are not production behaviour; the
  build fails if the lowered caps cut any other attachment.
  Threads 90-92 pin three OCR shapes (#908, #1113), their attachments
  the committed images under `indexer/tests/baseline/fixtures/`
  (regenerated by `fixtures/generate.py`, which renders one large line
  of text per image with Pillow's bundled font and writes no metadata;
  `test_baseline_build.py` checks the PNG has no text chunks, the PDF
  no Info dictionary and the TIFF only structural tags): a PNG read by
  the image OCR extractor (t90), a scanned PDF with no text layer and
  three image pages (t91), one more than the OCR page cap the build
  lowers to 2 (`INDEXER_OCR_MAX_PAGES`; production: 20), so its last
  page is a known loss in `corpus_evidence`
  (`test_ocr_shapes_baseline.py`), and a multipage TIFF of three frames
  (t92), read frame by frame by the image OCR extractor up to the same
  cap, so its last frame is a known loss the same way and the build
  logs the `image OCR capped` WARNING (#885) with `ocr_capped_images=1`
  in the attachments aggregate.
  Three Tesseract versions are in play (Homebrew, Ubuntu, the image),
  so the OCR'd words stay out of every golden search query and every
  `unanswerable` question's `absent_terms`, and the checks match them
  case-insensitively with whitespace normalised: an OCR difference
  fails those checks, not the keyword ranks. The thread vectors do
  include the OCR'd text (the thread vector is the mean of its chunk
  vectors), so a difference could still move t90, t91 or t92 within
  another question's top 10 in the snapshot; treat that as an OCR
  difference, not a retrieval change.
  Adding a thread can lower a recall floor's
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
  the change. A corpus change also regenerates the parser pin
  (`PARSER_PIN_UPDATE=1 uv run pytest tests/test_parser_pin.py` in
  `indexer/`), which catalogues every corpus message; its diff must
  be only the added or removed records.
- **Unexpected snapshot diff.** If a refactor PR produces a snapshot
  diff, treat it as a behaviour change and explain it in the PR. Do
  not regenerate the snapshot just to make the test pass.

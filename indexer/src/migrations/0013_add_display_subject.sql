-- Add ``display_subject`` to ``threads`` so retrieval tools can return
-- the original message subject (case preserved, ``Re:``/``Fwd:`` intact)
-- instead of the normalized matching key stored in ``subject``.
--
-- The ``subject`` column remains the threader's grouping key
-- (lowercased, prefix-stripped) so subject-based thread merging keeps
-- working unchanged. Only retrieval output changes.
--
-- Existing threads receive ``NULL`` for ``display_subject``. The
-- retrieval layer coalesces with ``subject`` so old threads continue
-- to render with the legacy lowercased value until a future indexer
-- pass refreshes them. New threads (and any thread that gains a
-- message after this migration) get the original subject populated by
-- ``Database.upsert_thread``.

ALTER TABLE threads ADD COLUMN display_subject TEXT;

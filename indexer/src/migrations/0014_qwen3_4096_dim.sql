-- !!! DESTRUCTIVE MIGRATION !!!
--
-- This migration drops + recreates the ``threads_vec`` and
-- ``message_chunks_vec`` tables and clears ``message_chunks``,
-- ``message_chunks_fts``, ``indexed_files``, and ``indexing_jobs``.
-- Every existing chunk + vector + scan-state row is discarded and the
-- next indexer scan re-embeds the entire mailbox from Maildir — hours
-- of work on a populated install.
--
-- The runner refuses to apply this migration on a populated v13
-- database unless the operator sets ``INDEXER_MIGRATION_V14_FORCE=true``.
-- See ``Database._guard_destructive_migrations`` and the .env.example
-- entry. Empty databases skip the gate (nothing to lose).
--
-- Resize the vector tables from 768-dim to 4096-dim for the new
-- MLX-served Qwen3-Embedding-8B default. The legacy Ollama
-- ``nomic-embed-text`` was 768-dim; Qwen3-Embedding-8B is 4096.
--
-- ``vec0`` virtual tables store the dimension as part of their schema
-- and do not support ``ALTER`` to change it — drop-and-recreate is the
-- only path. Every existing 768-dim vector is discarded by this
-- migration; an existing install MUST reindex the mailbox from Maildir
-- after this runs (the indexer's normal scan path will refill
-- ``threads_vec`` and ``message_chunks_vec`` as messages are
-- re-embedded against the new model).
--
-- The companion deletes below force that reindex. Without them, a
-- v13→v14 upgrade silently leaves the vector tables empty:
--
-- * ``message_chunks`` rows act as the indexer's "this chunk is
--   already embedded" cache. Leaving them would skip re-embedding
--   for every existing chunk on the next scan.
-- * ``message_chunks_fts`` is the contentless FTS5 mirror of
--   ``message_chunks``; clearing it keeps the keyword lane in sync
--   with the cleared chunk rows.
-- * ``indexed_files`` is the "I've seen this Maildir path" cache.
--   ``initial_index()`` skips any path already present, so leaving it
--   would prevent every existing file from being re-enqueued.
-- * ``indexing_jobs`` carries pending + dead-letter state from the
--   v13 install. Dead-lettered rows would NOT be re-enqueued by
--   ``initial_index()`` after ``indexed_files`` clears, so v13
--   poison-pill attempts would stay parked under v14. Clearing the
--   queue gives every file a fresh attempt under the new embedder.
--
-- ``threads`` rows are kept: thread metadata (subject, dates, snippet,
-- participants) is dimension-independent. Their thread vectors return
-- on the next chunk write, computed as mean-of-chunks at that time.
-- ``attachments``, ``attachment_extractions``, and ``pending_deletions``
-- are kept for the same reason — pure metadata / text / tombstones, not
-- affected by the embedding dim.

DROP TABLE IF EXISTS threads_vec;
CREATE VIRTUAL TABLE threads_vec USING vec0(
    thread_id TEXT PRIMARY KEY,
    embedding FLOAT[4096]
);

DROP TABLE IF EXISTS message_chunks_vec;
CREATE VIRTUAL TABLE message_chunks_vec USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding FLOAT[4096]
);

DELETE FROM message_chunks;
DELETE FROM message_chunks_fts;
DELETE FROM indexed_files;
DELETE FROM indexing_jobs;

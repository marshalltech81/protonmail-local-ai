-- v17: backfill the unit-norm storage invariant on existing vector rows.
--
-- The PR that introduced this migration normalizes every vector at the
-- DB write boundary so cosine similarity equals dot product downstream
-- and ranking does not depend on per-row magnitude. New writes go
-- through ``l2_normalize`` in ``upsert_thread`` /
-- ``replace_thread_vector`` / ``_rewrite_thread_row`` /
-- ``replace_message_chunks``; this migration covers the pre-existing
-- rows that were written before that change.
--
-- ``threads_vec`` is the primary concern: thread vectors were written
-- as ``mean_vector(chunk_embs)`` of unit chunk vectors, which has norm
-- < 1 in the general case. Without this backfill an upgraded install
-- would mix newly-normalized thread vectors (touched threads) with
-- non-unit older rows (untouched threads) and ranking would depend on
-- which threads happened to be re-embedded since the upgrade.
--
-- ``message_chunks_vec`` is mostly a safety net: Qwen3-Embedding-8B
-- emits unit-norm vectors per its model card so chunks written through
-- the production embedder are already within float32 round-trip noise
-- of unit-norm. But operators on alternate OpenAI-compatible providers
-- (DeepInfra, OpenRouter, vLLM, TEI) may have indexed against models
-- that do not normalize, so we backfill here too.
--
-- The ``WHERE vec_distance_l2(embedding, zeroblob(16384)) > 1e-9``
-- predicate skips genuine zero placeholders. ``vec_normalize`` divides
-- by the magnitude and produces NaN on a zero vector, which would
-- NaN-poison every later cosine query that touched the row. The
-- indexer's seed-vector logic intentionally writes a zero placeholder
-- for genuinely-new threads (Phase 1 seed before Phase 2c lands the
-- real chunk-mean vector) — preserving it through this backfill keeps
-- the three-case priority chain in ``main.py`` working unchanged. The
-- 16384-byte zero blob is 4096 float32 zeros, matching the pinned
-- ``EMBEDDING_DIM`` for the Qwen3-Embedding-8B-era schema (set in
-- ``0014_qwen3_4096_dim.sql``); if the embedding dim ever changes
-- again, the new dim's migration will re-establish whichever invariant
-- is right for the new model.
--
-- Idempotent: re-applying ``vec_normalize`` to an already-unit vector
-- is a no-op within float32 tolerance, so a partial-failure retry
-- (e.g. crash mid-migration) is safe.

UPDATE threads_vec
SET embedding = vec_normalize(embedding)
WHERE vec_distance_l2(embedding, zeroblob(16384)) > 1e-9;

UPDATE message_chunks_vec
SET embedding = vec_normalize(embedding)
WHERE vec_distance_l2(embedding, zeroblob(16384)) > 1e-9;

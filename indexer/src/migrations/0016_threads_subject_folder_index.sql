-- 0016_threads_subject_folder_index.sql
--
-- Add a covering B-tree index on ``threads(subject, folder, date_last)`` so
-- the threader's subject-fallback lookup
-- (``find_threads_by_subject(normalized_subject, folder)``) becomes a single
-- index seek instead of a full table scan. The query shape is
-- ``WHERE subject = ? AND folder = ? ORDER BY date_last DESC LIMIT ?``;
-- a composite index in that order lets SQLite both filter and sort without
-- a separate temp-b-tree pass.
--
-- The threader hits this query for every incoming message that misses on
-- In-Reply-To and References, which on a mature mailbox is a substantial
-- fraction of new arrivals. Without the index, an initial-scan sweep over
-- 50k messages issued 50k full ``threads`` scans serialized through the
-- ``_synchronized`` lock — blocking the watchdog observer and the
-- reconciler for the duration. The index is small (a few MB on a 100k-
-- thread mailbox) and removes the contention.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent if an operator
-- pre-created the index out of band. ``ANALYZE`` refreshes
-- ``sqlite_stat1`` so the planner picks the new index immediately
-- instead of waiting for stat refresh on first hit.

CREATE INDEX IF NOT EXISTS idx_threads_subject_folder
    ON threads(subject, folder, date_last);

-- Narrow ANALYZE to the new index only. A bare ``ANALYZE;`` rebuilds
-- ``sqlite_stat1`` for every table + index in the database, which on a
-- 100k+ thread mailbox can take tens of seconds at startup. ``ANALYZE
-- <index_name>`` updates only the rows that describe the just-created
-- index — which is all the planner needs to start picking it up.
ANALYZE idx_threads_subject_folder;

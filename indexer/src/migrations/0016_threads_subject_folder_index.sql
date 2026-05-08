-- 0016_threads_subject_folder_index.sql
--
-- Add a covering B-tree index on
-- ``threads(subject, folder, date_last, thread_id)`` so the threader's
-- subject-fallback lookup
-- (``find_threads_by_subject(normalized_subject, folder)``) becomes
-- index-only instead of a full table scan. The query shape is
-- ``SELECT thread_id FROM threads WHERE subject = ? AND folder = ?
-- ORDER BY date_last DESC LIMIT ?``. The first three columns satisfy
-- the WHERE filter + ORDER BY in one B-tree walk; including
-- ``thread_id`` as the fourth key column makes it an actually covering
-- index — SQLite serves the projected ``thread_id`` directly from the
-- index without a table seek per match.
--
-- The threader hits this query for every incoming message that misses
-- on In-Reply-To and References, which on a mature mailbox is a
-- substantial fraction of new arrivals. Without the index, an
-- initial-scan sweep over 50k messages issued 50k full ``threads``
-- scans serialized through the ``_synchronized`` lock — blocking the
-- watchdog observer and the reconciler for the duration. The index is
-- small (a few MB on a 100k-thread mailbox) and removes the contention.
--
-- ``DROP INDEX IF EXISTS`` + unconditional ``CREATE INDEX`` is
-- deliberate. A bare ``CREATE INDEX IF NOT EXISTS`` would silently
-- accept any same-named pre-existing index — including a hand-rolled
-- three-column variant from before the covering shape was decided.
-- That would stamp ``schema_version=16`` while leaving the database
-- with a non-covering index, defeating the migration's
-- behavioral contract. Dropping first guarantees the index is built
-- with the canonical four-column shape regardless of pre-state. The
-- DROP runs in the same per-migration ``BEGIN IMMEDIATE`` /
-- ``COMMIT`` as the CREATE, so a crash between them leaves the
-- database stamped at v15 with no index — the next startup retries
-- the migration cleanly.
--
-- ``ANALYZE`` refreshes ``sqlite_stat1`` so the planner picks the
-- new index immediately instead of waiting for stat refresh on
-- first hit.

DROP INDEX IF EXISTS idx_threads_subject_folder;

CREATE INDEX idx_threads_subject_folder
    ON threads(subject, folder, date_last, thread_id);

-- Narrow ANALYZE to the new index only. A bare ``ANALYZE;`` rebuilds
-- ``sqlite_stat1`` for every table + index in the database, which on a
-- 100k+ thread mailbox can take tens of seconds at startup. ``ANALYZE
-- <index_name>`` updates only the rows that describe the just-created
-- index — which is all the planner needs to start picking it up.
ANALYZE idx_threads_subject_folder;

-- v11 -> v12 (#1416): record on a cached extraction how its payload's
-- container was certified. An OLE2 or ZIP payload is now identified by
-- its directory before dispatch, whatever its label, so a row cached
-- for one before this version may hold the wrong extractor's result.
-- ``container@<version>`` = the identification version that ran; '' =
-- the payload starts with neither signature, so none applies; NULL =
-- unknown (every row cached before this version, and ``too_large``).

-- No default: every existing row starts NULL. The bytes are not in the
-- database, so the migration cannot tell; the reparse below sends every
-- occurrence through the cache lookup once, which stamps '' on a row
-- whose payload has neither signature (its result kept) and identifies
-- and re-extracts the rest. Unchanged chunks keep their vectors; text a
-- re-identified container now yields is embedded as new chunks. A
-- container sent under a message/* label is kept as sent from this
-- version, so its occurrence gets a new attachment ID: the reparse
-- removes the old occurrence, its FTS row, chunks and vectors. The startup sweep
-- queues the same rows and clears their occurrences' text completeness
-- first, so a message with an extraction continuation already queued
-- (which the reparse leaves as it is) refreshes them too.
ALTER TABLE attachment_extractions ADD COLUMN identifier TEXT;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

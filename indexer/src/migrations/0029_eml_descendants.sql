-- v28 -> v29 (#1310): discover attached-email file descendants in the child.
-- Existing chunks remain until normal reparse diff-writes the expanded occurrences.
-- The old manifest never assessed email-file descendants. Unknown until reparse,
-- including dead-lettered sources, which retain their existing queue state.
UPDATE messages SET attachments_manifest_complete = NULL;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

-- v14 -> v15 (#1338): current attachment occurrence inventory evidence.
-- NULL until phase 2c commits a reparse; unchanged chunks keep their vectors.
ALTER TABLE messages ADD COLUMN attachment_rows_current INTEGER
    CHECK (attachment_rows_current IN (0, 1));

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

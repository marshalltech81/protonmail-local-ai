-- v12 -> v13 (#1079): bounded top-level header evidence. Existing rows
-- are unknown until the reparse; it keeps unchanged chunks and vectors.
ALTER TABLE messages ADD COLUMN headers_complete INTEGER CHECK (headers_complete IN (0, 1));
CREATE TABLE message_headers (
    claimant_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    decoded_value TEXT,
    PRIMARY KEY (claimant_id, ordinal),
    FOREIGN KEY (claimant_id) REFERENCES messages(claimant_id) ON DELETE CASCADE
);
CREATE INDEX idx_message_headers_name_claimant ON message_headers(name_normalized, claimant_id);

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

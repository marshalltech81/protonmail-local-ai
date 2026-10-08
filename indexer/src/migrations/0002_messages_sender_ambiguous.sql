-- v1 -> v2 (#1144): record whether a message's sender attribution is
-- safe. 0 = one From header; 1 = unsafe (a repeated From, or the
-- parser's header-field scan stopped before a second From could be
-- ruled out); NULL = not yet assessed. No default: every existing row
-- starts NULL, and mcp-server reads NULL as "can't tell", so such a
-- message does not qualify for source authority until the reparse
-- below re-reads its file. A dead-lettered job is left as it is, so its
-- message stays NULL until ``make requeue-dead``.

ALTER TABLE messages ADD COLUMN sender_ambiguous INTEGER
    CHECK (sender_ambiguous IN (0, 1));

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

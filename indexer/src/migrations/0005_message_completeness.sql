-- v4 -> v5 (#1086): record per message whether the stored content each
-- message filter reads is complete, and which parse caps fired.
-- A filter that finds nothing in a message's stored data can answer
-- "no" only when that data is complete; mcp-server answers "can't
-- tell" (``indeterminate`` in ``query_messages``) otherwise.

-- 1 = complete within the documented indexing semantics, 0 = a parse
-- cap or a rejected element lost part of it, NULL = not yet assessed.
-- No defaults: every existing row starts NULL, and mcp-server reads
-- NULL as "can't tell", so a subject, text, address, attachment or
-- authority filter that does not match a message answers indeterminate
-- (not false) for it until the reparse below re-reads its file. A
-- dead-lettered job is left as it is, so its message stays NULL until
-- ``make requeue-dead``. ``body_complete`` is written with the body
-- chunks in phase 2c, so it stays NULL for a message whose chunks are
-- not committed. The existing flags (``sender_ambiguous``,
-- ``participant_names_complete``) record other facts and are kept.
ALTER TABLE messages ADD COLUMN subject_complete INTEGER
    CHECK (subject_complete IN (0, 1));
ALTER TABLE messages ADD COLUMN from_addresses_complete INTEGER
    CHECK (from_addresses_complete IN (0, 1));
ALTER TABLE messages ADD COLUMN to_addresses_complete INTEGER
    CHECK (to_addresses_complete IN (0, 1));
ALTER TABLE messages ADD COLUMN cc_addresses_complete INTEGER
    CHECK (cc_addresses_complete IN (0, 1));
ALTER TABLE messages ADD COLUMN attachments_manifest_complete INTEGER
    CHECK (attachments_manifest_complete IN (0, 1));
ALTER TABLE messages ADD COLUMN body_complete INTEGER
    CHECK (body_complete IN (0, 1));

-- The parse's nonzero ``PARSE_CAPS`` counts: a JSON object of fixed
-- cap names to integers, never content. NULL until the reparse.
ALTER TABLE messages ADD COLUMN caps_json TEXT;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

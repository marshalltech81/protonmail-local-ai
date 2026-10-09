-- v7 -> v8 (#1080): a missing or unparseable Date header is stored as
-- an unknown send date (``sent_at`` NULL) with its reason
-- (``sent_at_status``), never as the time the indexer first read the
-- file. That time moves to ``first_indexed_at``, the last fallback of
-- ``effective_at``: an ordering position, not evidence of a date.
--
-- SQLite cannot drop a NOT NULL constraint or change a generated
-- column in place, so ``messages`` is rebuilt. ``PRAGMA foreign_keys``
-- cannot be switched off inside the migration's transaction, and
-- dropping ``messages`` with it on deletes every
-- ``message_participants`` row (and through it every
-- ``message_participant_names`` row) by cascade; both are copied to
-- temporary tables first and written back once the new ``messages``
-- table is in place, all in this one transaction.

CREATE TEMP TABLE v8_message_participants AS SELECT * FROM message_participants;
CREATE TEMP TABLE v8_message_participant_names AS SELECT * FROM message_participant_names;

CREATE TABLE messages_v8 (
    claimant_id     TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    filepath        TEXT NOT NULL,
    folder          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    sent_at         TEXT,
    sent_at_status  TEXT CHECK (sent_at_status IN ('parsed', 'missing', 'invalid')),
    occurred_at     TEXT,
    effective_at    TEXT GENERATED ALWAYS AS
                    (COALESCE(occurred_at, sent_at, first_indexed_at)) VIRTUAL,
    in_reply_to     TEXT,
    references_json TEXT NOT NULL,
    has_attachments INTEGER NOT NULL,
    size_bytes      INTEGER,
    content_hash    TEXT,
    indexed_at      TEXT NOT NULL,
    first_indexed_at TEXT NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    replied         INTEGER NOT NULL DEFAULT 0,
    sender_ambiguous INTEGER CHECK (sender_ambiguous IN (0, 1)),
    participant_names_complete INTEGER
        CHECK (participant_names_complete IN (0, 1)),
    subject_complete INTEGER CHECK (subject_complete IN (0, 1)),
    from_addresses_complete INTEGER CHECK (from_addresses_complete IN (0, 1)),
    to_addresses_complete INTEGER CHECK (to_addresses_complete IN (0, 1)),
    cc_addresses_complete INTEGER CHECK (cc_addresses_complete IN (0, 1)),
    attachments_manifest_complete INTEGER
        CHECK (attachments_manifest_complete IN (0, 1)),
    body_complete INTEGER CHECK (body_complete IN (0, 1)),
    caps_json TEXT,
    CHECK ((sent_at_status = 'parsed') = (sent_at IS NOT NULL)),
    CHECK (sent_at_status IS NOT NULL OR sent_at IS NOT NULL),
    FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
        ON DELETE CASCADE
);

-- Every existing row keeps its ``sent_at``, which may be a fallback the
-- old parser made up, so its status is NULL: not yet assessed. Nothing
-- reads it as a send date until the reparse below sets the status
-- (mcp-server answers "can't tell"). ``first_indexed_at`` is not known
-- for these rows; the last time each was indexed stands in, and the
-- reparse of an undated message replaces it with the old fallback in
-- ``sent_at``, which was the time it was first indexed
-- (``Database._write_message_record``). ``effective_at`` is unchanged
-- for every row until then.
INSERT INTO messages_v8
    (claimant_id, message_id, thread_id, filepath, folder, subject, sent_at,
     sent_at_status, occurred_at, in_reply_to, references_json, has_attachments,
     size_bytes, content_hash, indexed_at, first_indexed_at, seen, flagged, replied,
     sender_ambiguous, participant_names_complete, subject_complete,
     from_addresses_complete, to_addresses_complete, cc_addresses_complete,
     attachments_manifest_complete, body_complete, caps_json)
SELECT claimant_id, message_id, thread_id, filepath, folder, subject, sent_at,
       NULL, occurred_at, in_reply_to, references_json, has_attachments,
       size_bytes, content_hash, indexed_at, indexed_at, seen, flagged, replied,
       sender_ambiguous, participant_names_complete, subject_complete,
       from_addresses_complete, to_addresses_complete, cc_addresses_complete,
       attachments_manifest_complete, body_complete, caps_json
FROM messages;

DROP TABLE messages;
ALTER TABLE messages_v8 RENAME TO messages;

CREATE INDEX idx_messages_message ON messages(message_id, claimant_id);
CREATE INDEX idx_messages_message_effective
    ON messages(message_id, effective_at, claimant_id);
CREATE INDEX idx_messages_thread_effective ON messages(thread_id, effective_at);
CREATE INDEX idx_messages_folder_effective ON messages(folder, effective_at);
CREATE INDEX idx_messages_effective ON messages(effective_at);
CREATE INDEX idx_messages_filepath ON messages(filepath);

INSERT INTO message_participants SELECT * FROM v8_message_participants;
INSERT INTO message_participant_names SELECT * FROM v8_message_participant_names;
DROP TABLE v8_message_participants;
DROP TABLE v8_message_participant_names;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

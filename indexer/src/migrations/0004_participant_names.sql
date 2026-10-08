-- v3 -> v4 (#1140): keep every distinct display name a participant was
-- written with in one message, not only the first.
-- ``message_participants.name`` stays the first name (display and
-- chunk attribution); name matching, find_contact and entity aliases
-- read this table. Each existing participant's stored first name is
-- copied in, so name matching keeps what it saw before the upgrade;
-- the reparse below re-reads each file and adds the other names. Until
-- it reaches a message, that message's names past the first are not
-- stored, as before the upgrade. A dead-lettered job is left as it is,
-- so its message keeps the first name only until ``make requeue-dead``.

CREATE TABLE message_participant_names (
    claimant_id TEXT NOT NULL,
    role        TEXT NOT NULL,
    address     TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (claimant_id, role, address, name),
    FOREIGN KEY (claimant_id, role, address)
        REFERENCES message_participants(claimant_id, role, address)
        ON DELETE CASCADE
);

INSERT INTO message_participant_names (claimant_id, role, address, name)
SELECT claimant_id, role, address, name FROM message_participants
WHERE name IS NOT NULL;

-- The alias prune now asks the names table (#464).
CREATE INDEX idx_message_participant_names_address_name
    ON message_participant_names(address, name);

DROP INDEX idx_message_participants_address_name;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

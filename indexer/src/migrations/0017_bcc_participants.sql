-- v16 -> v17 (#1090): retain observed Bcc under the shared address budget.
-- This nullable flag certifies parse capture only, never original-list exhaustiveness.
ALTER TABLE messages ADD COLUMN bcc_addresses_complete INTEGER
    CHECK (bcc_addresses_complete IN (0, 1));

CREATE TEMP TABLE v17_participants AS
    SELECT * FROM message_participants ORDER BY rowid;
CREATE TEMP TABLE v17_names AS SELECT * FROM message_participant_names;
DROP TABLE message_participant_names;
DROP TABLE message_participants;

CREATE TABLE message_participants (
    claimant_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc', 'bcc')),
    address TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (claimant_id, role, address),
    FOREIGN KEY (claimant_id) REFERENCES messages(claimant_id) ON DELETE CASCADE
);
CREATE INDEX idx_message_participants_address ON message_participants(address, role);
INSERT INTO message_participants SELECT * FROM v17_participants ORDER BY rowid;
CREATE TABLE message_participant_names (
    claimant_id TEXT NOT NULL,
    role TEXT NOT NULL,
    address TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (claimant_id, role, address, name),
    FOREIGN KEY (claimant_id, role, address)
        REFERENCES message_participants(claimant_id, role, address) ON DELETE CASCADE
);
CREATE INDEX idx_message_participant_names_address_name
    ON message_participant_names(address, name);
INSERT INTO message_participant_names SELECT * FROM v17_names;
DROP TABLE v17_participants;
DROP TABLE v17_names;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

-- v20 -> v21 (#1598, owner decision 2026-10-23, option A): provider
-- identity candidates are assessed per occurrence under a versioned
-- policy, and dated label-membership observations get their own tables.
-- A candidate is read from the stored ``message_headers`` rows, so no
-- reparse is queued and nothing is re-embedded; the tables start empty
-- (the indexer fills assessments in bounded batches after startup) and
-- the policy selector is seeded with the shipped version, which the
-- indexer moves at its next start if the code's version differs. Nothing
-- is accepted: a trigger refuses the ``accepted`` outcome. Same DDL as
-- ``database.IDENTITY_SCHEMA_STATEMENTS``.
CREATE UNIQUE INDEX idx_message_occurrences_id_account ON message_occurrences(id, account_id);
CREATE TABLE identity_policy_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    policy_version TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64)
);
CREATE TABLE provider_identity_assessments (
    occurrence_id         INTEGER NOT NULL,
    account_id            INTEGER NOT NULL REFERENCES accounts(id),
    policy_version        TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    outcome               TEXT NOT NULL CHECK (outcome IN ('accepted', 'unresolved', 'rejected')),
    reason                TEXT NOT NULL CHECK (reason IN ('recovered_folder', 'headers_unknown',
                              'headers_incomplete', 'no_identity_header',
                              'duplicate_identity_header', 'malformed_identity',
                              'no_acceptance_rule')),
    candidate             TEXT CHECK (candidate IS NULL OR (length(candidate) BETWEEN 1 AND 256 AND candidate NOT GLOB '*[^!-~]*')),
    identity_header_count INTEGER NOT NULL CHECK (identity_header_count >= 0),
    headers_complete      INTEGER CHECK (headers_complete IN (0, 1)),
    state                 TEXT NOT NULL CHECK (state IN ('current', 'invalidated')),
    assessed_at           TEXT NOT NULL,
    CHECK (candidate IS NULL OR reason = 'no_acceptance_rule'),
    PRIMARY KEY (occurrence_id, policy_version),
    FOREIGN KEY (occurrence_id, account_id)
        REFERENCES message_occurrences(id, account_id) ON DELETE CASCADE
);
CREATE TRIGGER provider_identity_no_acceptance_insert
BEFORE INSERT ON provider_identity_assessments
WHEN NEW.outcome = 'accepted'
BEGIN
    SELECT RAISE(ABORT, 'no identity acceptance rule is approved');
END;
CREATE TRIGGER provider_identity_no_acceptance_update
BEFORE UPDATE OF outcome ON provider_identity_assessments
WHEN NEW.outcome = 'accepted'
BEGIN
    SELECT RAISE(ABORT, 'no identity acceptance rule is approved');
END;
CREATE TRIGGER message_headers_invalidate_assessments_insert
AFTER INSERT ON message_headers
WHEN NEW.name_normalized = 'x-pm-internal-id'
BEGIN
    UPDATE provider_identity_assessments SET state = 'invalidated'
    WHERE state = 'current' AND occurrence_id IN (
        SELECT id FROM message_occurrences WHERE claimant_id = NEW.claimant_id);
END;
CREATE TRIGGER message_headers_invalidate_assessments_update
AFTER UPDATE ON message_headers
WHEN OLD.name_normalized = 'x-pm-internal-id' OR NEW.name_normalized = 'x-pm-internal-id'
BEGIN
    UPDATE provider_identity_assessments SET state = 'invalidated'
    WHERE state = 'current' AND occurrence_id IN (
        SELECT id FROM message_occurrences
        WHERE claimant_id IN (OLD.claimant_id, NEW.claimant_id));
END;
CREATE TRIGGER message_headers_invalidate_assessments_delete
AFTER DELETE ON message_headers
WHEN OLD.name_normalized = 'x-pm-internal-id'
BEGIN
    UPDATE provider_identity_assessments SET state = 'invalidated'
    WHERE state = 'current' AND occurrence_id IN (
        SELECT id FROM message_occurrences WHERE claimant_id = OLD.claimant_id);
END;
CREATE TRIGGER messages_invalidate_assessments
AFTER UPDATE OF headers_complete ON messages
WHEN OLD.headers_complete IS NOT NEW.headers_complete
BEGIN
    UPDATE provider_identity_assessments SET state = 'invalidated'
    WHERE state = 'current' AND occurrence_id IN (
        SELECT id FROM message_occurrences WHERE claimant_id = NEW.claimant_id);
END;
CREATE TRIGGER message_occurrences_invalidate_assessments
AFTER UPDATE OF folder, state ON message_occurrences
WHEN OLD.folder IS NOT NEW.folder OR OLD.state IS NOT NEW.state
BEGIN
    UPDATE provider_identity_assessments SET state = 'invalidated'
    WHERE state = 'current' AND occurrence_id = NEW.id;
END;
CREATE VIEW effective_accepted_occurrences AS
SELECT a.occurrence_id, a.account_id, a.policy_version, a.candidate
FROM provider_identity_assessments a
JOIN identity_policy_state p ON p.policy_version = a.policy_version
JOIN message_occurrences o ON o.id = a.occurrence_id AND o.state = 'active'
WHERE a.state = 'current' AND a.outcome = 'accepted' AND a.candidate IS NOT NULL;
CREATE TABLE label_scan_generations (
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    generation  INTEGER NOT NULL CHECK (generation >= 1),
    status      TEXT NOT NULL CHECK (status IN ('in_progress', 'complete', 'partial',
                    'failed', 'interrupted')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    CHECK ((status = 'in_progress') = (finished_at IS NULL)),
    PRIMARY KEY (account_id, generation)
);
CREATE UNIQUE INDEX idx_label_scan_generations_open
    ON label_scan_generations(account_id) WHERE status = 'in_progress';
CREATE TABLE label_membership_observations (
    account_id         INTEGER NOT NULL,
    provider_id        TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 256 AND provider_id NOT GLOB '*[^!-~]*'),
    label              TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 512),
    first_generation   INTEGER NOT NULL,
    last_generation    INTEGER NOT NULL,
    observed_at        TEXT NOT NULL,
    retired_generation INTEGER,
    retired_at         TEXT,
    CHECK ((retired_generation IS NULL) = (retired_at IS NULL)),
    CHECK (last_generation >= first_generation),
    CHECK (retired_generation IS NULL OR retired_generation > last_generation),
    PRIMARY KEY (account_id, provider_id, label),
    FOREIGN KEY (account_id, first_generation)
        REFERENCES label_scan_generations(account_id, generation),
    FOREIGN KEY (account_id, last_generation)
        REFERENCES label_scan_generations(account_id, generation),
    FOREIGN KEY (account_id, retired_generation)
        REFERENCES label_scan_generations(account_id, generation)
);
CREATE TRIGGER label_observations_retire_guard_insert
BEFORE INSERT ON label_membership_observations
WHEN NEW.retired_generation IS NOT NULL AND (
    SELECT status FROM label_scan_generations
    WHERE account_id = NEW.account_id AND generation = NEW.retired_generation
) IS NOT 'complete'
BEGIN
    SELECT RAISE(ABORT, 'only a complete generation retires a membership');
END;
CREATE TRIGGER label_observations_retire_guard_update
BEFORE UPDATE OF retired_generation ON label_membership_observations
WHEN NEW.retired_generation IS NOT NULL AND (
    SELECT status FROM label_scan_generations
    WHERE account_id = NEW.account_id AND generation = NEW.retired_generation
) IS NOT 'complete'
BEGIN
    SELECT RAISE(ABORT, 'only a complete generation retires a membership');
END;
INSERT INTO identity_policy_state (id, policy_version) VALUES (1, 'proton-bridge-v1');

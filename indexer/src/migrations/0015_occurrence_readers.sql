-- v14 -> v15 (#1567, #1568, #1569): the readers move to mailbox
-- occurrences, in one transaction with the version stamp. Nothing the
-- parser produces is added, so no reparse is queued; claimant, chunk
-- and thread IDs and vectors are unchanged.

-- An occurrence the reconciliation scan bound records provenance
-- ``scan``; the CHECK is part of the table, so the table is rebuilt
-- with its rows and ids unchanged.
CREATE TABLE message_occurrences_v15 (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    folder            TEXT NOT NULL,
    uniq              TEXT NOT NULL,
    claimant_id       TEXT NOT NULL REFERENCES messages(claimant_id) ON DELETE CASCADE,
    relative_path     TEXT NOT NULL,
    seen              INTEGER NOT NULL CHECK (seen IN (0, 1)),
    flagged           INTEGER NOT NULL CHECK (flagged IN (0, 1)),
    replied           INTEGER NOT NULL CHECK (replied IN (0, 1)),
    trashed           INTEGER NOT NULL CHECK (trashed IN (0, 1)),
    first_observed_at TEXT,
    last_observed_at  TEXT,
    internal_at       TEXT,
    state             TEXT NOT NULL CHECK (state IN ('active', 'retired')),
    retired_at        TEXT,
    provenance        TEXT NOT NULL
                      CHECK (provenance IN ('parse', 'rename', 'scan', 'migration')),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);
INSERT INTO message_occurrences_v15
    (id, account_id, folder, uniq, claimant_id, relative_path, seen, flagged, replied,
     trashed, first_observed_at, last_observed_at, internal_at, state, retired_at, provenance)
SELECT id, account_id, folder, uniq, claimant_id, relative_path, seen, flagged, replied,
       trashed, first_observed_at, last_observed_at, internal_at, state, retired_at, provenance
FROM message_occurrences;
DROP TABLE message_occurrences;
ALTER TABLE message_occurrences_v15 RENAME TO message_occurrences;
CREATE UNIQUE INDEX idx_message_occurrences_key
    ON message_occurrences(account_id, folder, uniq, claimant_id) WHERE state = 'active';
CREATE UNIQUE INDEX idx_message_occurrences_path
    ON message_occurrences(account_id, relative_path) WHERE state = 'active';
CREATE INDEX idx_message_occurrences_claimant ON message_occurrences(claimant_id);

-- Every message keeps at least one occurrence once ``messages.filepath``
-- is gone. A message the v14 seed could not bind (its path held other
-- bytes) gets a retired one at its stored path: no file was verified to
-- hold its bytes, so it is not a live copy, and the reconciler treats it
-- like any message that lost its last live copy. Its observation dates
-- stay NULL. A path outside the configured root fails the migration, as
-- in v14 (``occurrences.SeedFunctions``).
INSERT INTO message_occurrences
    (account_id, folder, uniq, claimant_id, relative_path, seen, flagged, replied,
     trashed, state, retired_at, provenance)
SELECT a.id, pmai_occurrence_folder(m.filepath), pmai_occurrence_uniq(m.filepath),
       m.claimant_id, pmai_occurrence_path(m.filepath), m.seen, m.flagged, m.replied,
       pmai_occurrence_trashed(m.filepath), 'retired',
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'migration'
FROM messages m
JOIN accounts a ON a.scope_uuid = pmai_account_scope()
WHERE NOT EXISTS (SELECT 1 FROM message_occurrences o WHERE o.claimant_id = m.claimant_id)
ORDER BY m.claimant_id;

-- Tombstones are keyed by occurrence (#1567). A message's old tombstones
-- become one row on its occurrence (an active one first, a trashed one
-- first among those) with their latest mark, so a grace period never
-- starts earlier than before. The reconciler's next sweep clears the
-- rows of a message that still has a live occurrence and marks every
-- message that has none.
CREATE TABLE pending_deletions_v15 (
    occurrence_id INTEGER PRIMARY KEY
                  REFERENCES message_occurrences(id) ON DELETE CASCADE,
    claimant_id   TEXT NOT NULL,
    marked_at     TEXT NOT NULL
);
INSERT INTO pending_deletions_v15 (occurrence_id, claimant_id, marked_at)
SELECT occurrence_id, claimant_id, marked_at FROM (
    SELECT p.claimant_id, MAX(p.marked_at) AS marked_at,
           (SELECT o.id FROM message_occurrences o WHERE o.claimant_id = p.claimant_id
            ORDER BY o.state = 'active' DESC, o.trashed DESC, o.id LIMIT 1) AS occurrence_id
    FROM pending_deletions p
    GROUP BY p.claimant_id
)
WHERE occurrence_id IS NOT NULL;
DROP TABLE pending_deletions;
ALTER TABLE pending_deletions_v15 RENAME TO pending_deletions;
CREATE INDEX idx_pending_deletions_claimant ON pending_deletions(claimant_id, marked_at);

-- The reconciliation scan binds a found copy to the message holding its
-- bytes by content hash.
CREATE INDEX idx_messages_content_hash ON messages(content_hash);

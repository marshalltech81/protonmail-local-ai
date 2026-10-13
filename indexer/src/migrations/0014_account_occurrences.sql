-- v13 -> v14 (#1596, #1566): the account registry and mailbox
-- occurrences, in one transaction with the version stamp.
--
-- The registry holds one row per account the index serves, today
-- exactly the configured one: ``ACCOUNT_SCOPE`` and its ``SOURCE_MODE``
-- provider, read through the connection-local functions ``Database``
-- registers before the runner starts
-- (``account.register_account_functions``). Rows are never changed or
-- removed.
CREATE TABLE accounts (
    id         INTEGER PRIMARY KEY,
    scope_uuid TEXT NOT NULL UNIQUE,
    provider   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER accounts_never_updated BEFORE UPDATE ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts rows are never changed');
END;
CREATE TRIGGER accounts_never_deleted BEFORE DELETE ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts rows are never removed');
END;

INSERT INTO accounts (scope_uuid, provider, created_at)
VALUES (pmai_account_scope(), pmai_account_provider(),
        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

-- Each Maildir copy of a message is its own occurrence (#1566), keyed by
-- the account, its account-relative folder, the file's Maildir uniq and
-- the claimant ID, with its path relative to the account's Maildir root
-- (``occurrences.locate``). Dormant in this release: ingest writes
-- occurrences beside the ``messages`` / ``message_thread_map`` columns,
-- and nothing reads them yet.
CREATE TABLE message_occurrences (
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
    provenance        TEXT NOT NULL CHECK (provenance IN ('parse', 'rename', 'migration')),
    CHECK ((state = 'retired') = (retired_at IS NOT NULL))
);
CREATE UNIQUE INDEX idx_message_occurrences_key
    ON message_occurrences(account_id, folder, uniq, claimant_id) WHERE state = 'active';
CREATE UNIQUE INDEX idx_message_occurrences_path
    ON message_occurrences(account_id, relative_path) WHERE state = 'active';
CREATE INDEX idx_message_occurrences_claimant ON message_occurrences(claimant_id);

-- Every stored path must lie inside the configured Maildir root: the
-- aggregate relativizes each row, and one outside it fails the whole
-- migration (``occurrences.SeedFunctions``; ``Database`` reports it with
-- fixed text).
SELECT count(pmai_occurrence_path(filepath)) FROM messages;

-- One occurrence per message whose file record carries the message's
-- content hash, the hash its parse verified over the file's bytes. The
-- migration derives it, so nothing is observed: the observation dates
-- stay NULL until a parse sees the file. A message whose path now holds
-- other bytes gets none here; the reparse below binds whatever its file
-- holds.
INSERT INTO message_occurrences
    (account_id, folder, uniq, claimant_id, relative_path, seen, flagged, replied,
     trashed, state, provenance)
SELECT a.id, pmai_occurrence_folder(m.filepath), pmai_occurrence_uniq(m.filepath),
       m.claimant_id, pmai_occurrence_path(m.filepath), m.seen, m.flagged, m.replied,
       pmai_occurrence_trashed(m.filepath), 'active', 'migration'
FROM messages m
JOIN indexed_files f ON f.filepath = m.filepath AND f.content_hash = m.content_hash
JOIN accounts a ON a.scope_uuid = pmai_account_scope()
ORDER BY m.claimant_id;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

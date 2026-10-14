-- v17 -> v18 (#1592): mbsync's latest collection outcome, one row per
-- account, which the indexer fills from mbsync's collection status file
-- at its next read (startup, each ingestion-state heartbeat, and each
-- rename of the file). The table starts empty: get_mailbox_status reads
-- no row as an unknown outcome. Nothing the parser produces is added, so
-- no reparse is queued. Same DDL as
-- ``database.COLLECTION_STATE_SCHEMA_STATEMENTS``.
CREATE TABLE account_collection_state (
    account_id                     INTEGER PRIMARY KEY REFERENCES accounts(id),
    status                         TEXT NOT NULL CHECK (status IN ('known', 'unknown')),
    unknown_reason                 TEXT CHECK (unknown_reason IN ('missing', 'legacy',
                                       'malformed', 'unsupported', 'account_mismatch',
                                       'unreadable')),
    run_deadline_secs              INTEGER,
    sync_interval_secs             INTEGER,
    attempt_id                     TEXT,
    attempt_started_at             TEXT,
    attempt_outcome                TEXT CHECK (attempt_outcome IN ('in_progress', 'succeeded',
                                       'succeeded_with_withheld_folders', 'failed')),
    terminal_attempt_id            TEXT,
    terminal_completed_at          TEXT,
    terminal_outcome               TEXT CHECK (terminal_outcome IN ('succeeded',
                                       'succeeded_with_withheld_folders', 'failed')),
    terminal_withheld_folders      INTEGER CHECK (terminal_withheld_folders >= 0),
    terminal_failure_class         TEXT CHECK (terminal_failure_class IN ('deadline', 'killed',
                                       'error', 'permission_repair', 'interrupted')),
    success_attempt_id             TEXT,
    success_completed_at           TEXT,
    success_delivered              INTEGER CHECK (success_delivered IN (0, 1)),
    unrecovered_failure_attempt_id TEXT,
    observed_at                    TEXT NOT NULL,
    CHECK ((status = 'unknown') = (unknown_reason IS NOT NULL))
);

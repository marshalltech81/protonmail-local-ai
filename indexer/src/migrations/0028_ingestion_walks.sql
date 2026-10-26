-- Completed enumeration only; existing indexes have no historical walk evidence.
CREATE TABLE ingestion_walks (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    root TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    sync_completed_at TEXT,
    sync_interval_secs INTEGER,
    sync_attempt_id TEXT,
    sync_generation INTEGER CHECK (sync_generation >= 1),
    files_seen INTEGER NOT NULL CHECK (files_seen >= 0),
    unreadable_directories INTEGER NOT NULL CHECK (unreadable_directories >= 0),
    enqueued INTEGER NOT NULL CHECK (enqueued >= 0 AND enqueued <= files_seen)
);
CREATE INDEX idx_ingestion_walks_account ON ingestion_walks(account_id, id);

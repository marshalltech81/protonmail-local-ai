-- v21: one-row table holding mbsync's last successful sync (as the
-- indexer read it from the Maildir stamp) and the indexer's last report,
-- for mcp-server's get_mailbox_status.
CREATE TABLE ingestion_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    sync_completed_at  TEXT,
    sync_interval_secs INTEGER,
    indexer_seen_at    TEXT NOT NULL
);

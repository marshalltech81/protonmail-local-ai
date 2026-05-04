-- 0015_fts_rowid_indexes.sql
--
-- Add B-tree indexes on the ``fts_rowid`` columns of ``threads``,
-- ``message_chunks``, and ``attachments``. The mcp-server hybrid-search
-- lanes JOIN their FTS5 virtual tables back to these base tables on
-- ``<table>.fts_rowid = <fts>.rowid`` to recover full-row data and apply
-- post-fusion filters. Without an index on the join column, SQLite
-- planned ``SCAN <base>`` for every FTS5 hit — turning the JOIN into
-- O(N_fts × N_base). On a populated mailbox (15k+ FTS5 hits, 73k chunk
-- rows) that produced ~1.1B row comparisons per query and made
-- ``search_emails`` block for 5–7 minutes, blowing the 4-min MCP
-- request timeout. Indexes are tiny (one INTEGER per row) and the
-- post-fix plan switches to ``SEARCH <base> USING INDEX (fts_rowid=?)``,
-- bringing hybrid_search end-to-end from ~400 s to ~0.5 s.
--
-- ``IF NOT EXISTS`` lets a hot-fix that pre-creates these indexes (e.g.
-- via ``docker exec indexer python3``) coexist with the migration; the
-- runner is idempotent across operator manual fix + scripted apply.
-- ``ANALYZE`` refreshes ``sqlite_stat1`` so the planner immediately
-- picks the new indexes — without it the planner can keep using its
-- previously-cached statistics for the first few sessions after the
-- upgrade and miss the speedup.

CREATE INDEX IF NOT EXISTS idx_threads_fts_rowid
    ON threads(fts_rowid);

CREATE INDEX IF NOT EXISTS idx_message_chunks_fts_rowid
    ON message_chunks(fts_rowid);

CREATE INDEX IF NOT EXISTS idx_attachments_fts_rowid
    ON attachments(fts_rowid);

ANALYZE;

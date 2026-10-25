-- Bounded literal source indexes; existing mail is reparsed without re-embedding unchanged chunks.

CREATE TABLE literal_state (
 claimant_id TEXT PRIMARY KEY REFERENCES messages(claimant_id) ON DELETE CASCADE,
 version INTEGER NOT NULL, unicode_version TEXT NOT NULL, generation TEXT,
 body_complete INTEGER, classified_complete INTEGER, attachments_complete INTEGER,
 body_windows INTEGER NOT NULL, body_bytes INTEGER NOT NULL,
 body_metadata INTEGER NOT NULL, body_membership INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE literal_attachment_state (
 occurrence_id TEXT PRIMARY KEY REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 generation TEXT, complete INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE literal_attachment_spans (
 occurrence_id TEXT NOT NULL REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, data BLOB NOT NULL CHECK(typeof(data)='blob' AND length(data)<=32768),
 PRIMARY KEY(occurrence_id,ordinal)
) WITHOUT ROWID;
CREATE TABLE literal_windows (
 id INTEGER PRIMARY KEY,
 claimant_id TEXT NOT NULL REFERENCES messages(claimant_id) ON DELETE CASCADE,
 part INTEGER, occurrence_id TEXT REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
 byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 0 AND 36764),
 read_bytes INTEGER NOT NULL CHECK(read_bytes BETWEEN 0 AND 36764),
 ranges TEXT NOT NULL CHECK(length(CAST(ranges AS BLOB))<=524288),
 CHECK((part IS NULL) <> (occurrence_id IS NULL)),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
);
CREATE INDEX idx_literal_claimant ON literal_windows(claimant_id);
CREATE INDEX idx_literal_occurrence ON literal_windows(occurrence_id);
CREATE VIRTUAL TABLE literal_long_fts USING fts5(tokens,tokenize='ascii',detail=none,content='',contentless_delete=1);
CREATE VIRTUAL TABLE literal_short_fts USING fts5(tokens,tokenize='ascii',detail=none,content='',contentless_delete=1);
CREATE TABLE literal_scrub (id INTEGER PRIMARY KEY CHECK(id=1));
CREATE TRIGGER literal_window_delete AFTER DELETE ON literal_windows BEGIN
 DELETE FROM literal_long_fts WHERE rowid=old.id;
 DELETE FROM literal_short_fts WHERE rowid=old.id;
 INSERT OR IGNORE INTO literal_scrub VALUES(1);
END;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

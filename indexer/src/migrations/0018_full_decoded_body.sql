-- v17 -> v18 (#1362): complete decoded MIME body sources, unknown until reparse.
ALTER TABLE messages ADD COLUMN full_body_complete INTEGER CHECK(full_body_complete IN (0,1));
ALTER TABLE messages ADD COLUMN full_body_generation TEXT;
ALTER TABLE messages ADD COLUMN full_body_version INTEGER;

CREATE TABLE message_body_parts (
 claimant_id TEXT NOT NULL REFERENCES messages(claimant_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, parent INTEGER NOT NULL,
 representation TEXT NOT NULL CHECK(representation IN ('container','alternative','related','message','plain','html','other')),
 selected INTEGER NOT NULL CHECK(selected IN (0,1)),
 losses INTEGER NOT NULL, source_chars INTEGER, source_bytes INTEGER,
 source_digest TEXT, classified_until INTEGER,
 PRIMARY KEY(claimant_id,ordinal)
) WITHOUT ROWID;
CREATE TABLE message_body_spans (
 claimant_id TEXT NOT NULL, part INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 data BLOB NOT NULL CHECK(typeof(data)='blob' AND length(data)<=32768),
 PRIMARY KEY(claimant_id,part,ordinal),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE message_body_ranges (
 claimant_id TEXT NOT NULL, part INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('body','quote','signature','forwarded')),
 PRIMARY KEY(claimant_id,part,start),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

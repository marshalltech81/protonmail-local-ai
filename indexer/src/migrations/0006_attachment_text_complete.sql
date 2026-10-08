-- v5 -> v6 (#1242): record per attachment occurrence whether its
-- committed attachment chunks hold all its text, so a filter over
-- attachment text can tell "not in the text" from "the text is not all
-- there" (a cap, an unread page, a failed or unsupported extraction).

-- 1 = complete, 0 = a known loss or a status that never certifies
-- absence (failed, unsupported, too large, OCR disabled), NULL = not
-- assessed. Phase 2c writes it with the occurrence's chunks, beside
-- ``text_extractor``, the extractor stamp of the result that applied
-- (``docx@7``; NULL when no extractor ran). An ``EXTRACTOR_VERSIONS``
-- bump sets it back to NULL at startup for every occurrence whose stamp
-- is older, dead-lettered messages included, until its message is
-- processed again.
ALTER TABLE attachments ADD COLUMN text_complete INTEGER
    CHECK (text_complete IN (0, 1));
ALTER TABLE attachments ADD COLUMN text_extractor TEXT;

-- Whether a cached result lost no text, so an occurrence served from
-- the cache inherits it. Written by the extractor, not the parser.
ALTER TABLE attachment_extractions ADD COLUMN text_complete INTEGER
    CHECK (text_complete IN (0, 1));

-- No defaults: every existing row starts NULL, and the reparse below
-- fills them (#1285). It re-reads each message, so the parser's payload
-- loss is known again, and a ``success`` or ``empty`` cached result with
-- no record (every one cached before this version) is re-extracted once
-- and gets one; chunks whose text is unchanged keep their IDs, so
-- nothing is re-embedded for them. An ``-ocr`` result is kept while OCR
-- is off (its text could only be replaced by "OCR disabled"); the
-- startup sweep re-queues its messages once OCR is on. A dead-lettered
-- job is left as it is, so its occurrences stay NULL until
-- ``make requeue-dead``.
INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;

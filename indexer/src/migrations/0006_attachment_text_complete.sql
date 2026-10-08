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

-- No defaults and no reparse: every existing row starts NULL. A reparse
-- would serve the cached results, which carry no completeness record
-- before this version, so it could only turn NULL into 0 for
-- occurrences that are never complete anyway; both read as "can't
-- tell". An occurrence using a cached result stays NULL until those
-- bytes are extracted again (a version bump, a retried failure, OCR
-- turned on, a raised size cap) and its message is processed again.

-- v2 -> v3 (#891): record on a cached extraction how many scanned PDF
-- pages the OCR page cap left unread, so an occurrence later served
-- from the cache still counts as capped. NULL = unknown: every existing
-- row starts NULL and counts as nothing; 0 = known, nothing skipped.
-- The extractor writes it, not the parser, so no reparse is queued and
-- nothing is re-extracted: the rows already cached stay unknown until
-- the same bytes are extracted again for another reason.

ALTER TABLE attachment_extractions ADD COLUMN ocr_pages_skipped INTEGER
    CHECK (ocr_pages_skipped >= 0);

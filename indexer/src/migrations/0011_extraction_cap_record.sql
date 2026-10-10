-- v10 -> v11 (#1418): record on a cached extraction the configured
-- limits that cut it, so raising a limit re-extracts the results it cut:
-- ``ocr_pages_cap`` (INDEXER_OCR_MAX_PAGES: scanned PDF pages, TIFF
-- frames), ``digital_pages_cap`` (INDEXER_PDF_MAX_DIGITAL_PAGES) and
-- ``extracted_chars_cap`` (INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS).
-- A value = the limit that cut; 0 = the limit in effect did not cut;
-- NULL = unknown (not applicable to the module, a status other than
-- success / empty, or a row cached before this version).

-- No defaults: every existing row starts NULL. The extractor writes
-- these, not the parser, so no reparse is queued. NULL alone never
-- proves a limit was raised: the startup sweep re-queues, once, the
-- messages using a ``success`` / ``empty`` row cached before this
-- version that lost text (``text_complete`` 0); the re-extraction
-- records the columns, which ends it.
ALTER TABLE attachment_extractions ADD COLUMN ocr_pages_cap INTEGER
    CHECK (ocr_pages_cap >= 0);
ALTER TABLE attachment_extractions ADD COLUMN digital_pages_cap INTEGER
    CHECK (digital_pages_cap >= 0);
ALTER TABLE attachment_extractions ADD COLUMN extracted_chars_cap INTEGER
    CHECK (extracted_chars_cap >= 0);

-- v26 -> v27 (#1361): add text identity while preserving metadata-first layout (#1381).
-- NULL is unknown until the bounded backfill; no text is hashed in this migration.
-- Preserve all cache fields and rowids. No reparse or embedding work is queued.
CREATE TABLE attachment_extractions_v27 (
    attachment_id      TEXT NOT NULL,
    extractor_module   TEXT NOT NULL,
    extraction_status  TEXT NOT NULL,
    extractor          TEXT,
    extraction_error   TEXT,
    extracted_at       TEXT NOT NULL,
    ocr_pages_skipped  INTEGER CHECK (ocr_pages_skipped >= 0),
    text_complete      INTEGER CHECK (text_complete IN (0, 1)),
    ocr_pages_cap      INTEGER CHECK (ocr_pages_cap >= 0),
    digital_pages_cap  INTEGER CHECK (digital_pages_cap >= 0),
    extracted_chars_cap INTEGER CHECK (extracted_chars_cap >= 0),
    identifier         TEXT,
    text_sha256        TEXT,
    extracted_text     TEXT,
    PRIMARY KEY (attachment_id, extractor_module)
);
INSERT INTO attachment_extractions_v27 (rowid, attachment_id, extractor_module, extraction_status, extractor, extraction_error, extracted_at, ocr_pages_skipped, text_complete, ocr_pages_cap, digital_pages_cap, extracted_chars_cap, identifier, extracted_text)
SELECT rowid, attachment_id, extractor_module, extraction_status, extractor, extraction_error, extracted_at, ocr_pages_skipped, text_complete, ocr_pages_cap, digital_pages_cap, extracted_chars_cap, identifier, extracted_text FROM attachment_extractions;
DROP TABLE attachment_extractions;
ALTER TABLE attachment_extractions_v27 RENAME TO attachment_extractions;
CREATE INDEX idx_extractions_missing_text_sha256
    ON attachment_extractions(attachment_id, extractor_module)
    WHERE text_sha256 IS NULL AND extracted_text IS NOT NULL;

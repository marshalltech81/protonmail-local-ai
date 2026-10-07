-- v0 -> v1 (#928): key the attachment extraction cache by
-- (content hash, extractor module) instead of content hash alone, so the
-- same bytes under labels that select different extractors keep separate
-- results.
--
-- Each existing row keeps its result and takes its module from its
-- extractor stamp: ``docx@5`` -> ``docx``, ``pdf-ocr@4`` -> ``pdf``,
-- ``html`` -> ``html`` (the ``extractors._extractor_module`` rule). A row
-- with no stamp (``unsupported`` and ``too_large`` rows) is keyed '',
-- the module of an occurrence that selects no extractor. Every existing
-- occurrence then uses the row of its payload, as it did before; an
-- occurrence that selects another module re-extracts once on its next
-- reprocess and moves to its own row.
--
-- SQLite cannot change a primary key in place, so the table is rebuilt.

ALTER TABLE attachments ADD COLUMN extractor_module TEXT NOT NULL DEFAULT '';

CREATE TABLE attachment_extractions_v1 (
    attachment_id      TEXT NOT NULL,
    extractor_module   TEXT NOT NULL,
    extraction_status  TEXT NOT NULL,
    extractor          TEXT,
    extracted_text     TEXT,
    extraction_error   TEXT,
    extracted_at       TEXT NOT NULL,
    PRIMARY KEY (attachment_id, extractor_module)
);

INSERT INTO attachment_extractions_v1
    (attachment_id, extractor_module, extraction_status, extractor,
     extracted_text, extraction_error, extracted_at)
SELECT
    attachment_id,
    CASE
        WHEN extractor IS NULL OR extractor = '' THEN ''
        ELSE
            -- Strip ``@<version>``, then ``-<suffix>``.
            CASE
                WHEN instr(base, '-') > 0 THEN substr(base, 1, instr(base, '-') - 1)
                ELSE base
            END
    END,
    extraction_status,
    extractor,
    extracted_text,
    extraction_error,
    extracted_at
FROM (
    SELECT
        *,
        CASE
            WHEN instr(extractor, '@') > 0 THEN substr(extractor, 1, instr(extractor, '@') - 1)
            ELSE extractor
        END AS base
    FROM attachment_extractions
);

DROP TABLE attachment_extractions;

ALTER TABLE attachment_extractions_v1 RENAME TO attachment_extractions;

UPDATE attachments
SET extractor_module = COALESCE(
    (SELECT e.extractor_module FROM attachment_extractions e
     WHERE e.attachment_id = attachments.attachment_id),
    ''
);

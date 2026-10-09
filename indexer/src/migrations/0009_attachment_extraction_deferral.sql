-- v8 -> v9 (#1236): mark an attachment occurrence whose extraction the
-- per-message extraction budget deferred to a later pass of its message.

-- NULL = not deferred. A deferred occurrence has ``text_complete`` 0,
-- keeps the chunks it had and has no ``attachment_extractions`` row
-- written for it; the pass that extracts it clears the mark. No message
-- has been deferred before this version, so every existing row stays
-- NULL and nothing is re-parsed.
ALTER TABLE attachments ADD COLUMN extraction_deferred_at TEXT;

-- The MCP server asks, once per chunk it reads, whether the chunk's
-- payload has a deferred copy in its message: a partial composite index
-- over the deferred rows only.
CREATE INDEX idx_attachments_deferred ON attachments(claimant_id, attachment_id)
    WHERE extraction_deferred_at IS NOT NULL;

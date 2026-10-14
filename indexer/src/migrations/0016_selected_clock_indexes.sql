-- v15 -> v16 (#1123): index definite-match pages, without reparsing evidence.
CREATE INDEX idx_messages_sent ON messages(sent_at, claimant_id)
    WHERE sent_at_status = 'parsed';

-- Match this exact Boolean-equality predicate only in page SQL: ordinary
-- occurred date bounds must not make broad count queries use this index.
CREATE INDEX idx_messages_occurred ON messages(occurred_at, claimant_id)
    WHERE (occurred_at IS NOT NULL) = 1;

-- v21 -> v22 (#1635): seek by the complete listing order, including timestamp ties.
-- No evidence rows, queue entries or embeddings change.
DROP INDEX idx_messages_effective;
CREATE INDEX idx_messages_effective ON messages(effective_at, claimant_id);

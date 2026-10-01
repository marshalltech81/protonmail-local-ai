-- v21 -> v22: index message_thread_map lookups (#302). Flag renames
-- look up and update by filepath; thread rebuilds and removals select
-- by thread_id. Both were full-table scans. Index-only: no table or
-- column changes.
CREATE INDEX idx_message_thread_map_filepath ON message_thread_map(filepath);
CREATE INDEX idx_message_thread_map_thread ON message_thread_map(thread_id);

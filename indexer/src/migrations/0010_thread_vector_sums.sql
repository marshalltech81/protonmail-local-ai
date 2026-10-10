-- v9 -> v10 (#1356): the exact running sum of each thread's chunk
-- vectors and their count, so the thread vector is derived without
-- reading every chunk vector of the thread.

-- ``sum`` is in units of 2**-149 in the ``encoding_version`` encoding
-- (``vector_sums``). No row is written here: the first write that
-- touches a thread fills its row in that write's transaction, and the
-- backfill sweep fills the rest in bounded batches. Nothing is
-- re-parsed or re-embedded.
CREATE TABLE thread_vector_sums (
    thread_id        TEXT PRIMARY KEY,
    count            INTEGER NOT NULL CHECK (count >= 0),
    encoding_version INTEGER NOT NULL,
    sum              BLOB NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

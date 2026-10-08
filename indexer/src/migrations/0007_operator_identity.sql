-- v6 -> v7 (#824): store the operator's own addresses from
-- ``config/identity.toml``, for the message-direction classifier.

-- The canonical addresses the operator listed. Replaced as a whole at
-- every indexer start (``Database.set_operator_identity``).
CREATE TABLE operator_addresses (address TEXT PRIMARY KEY) WITHOUT ROWID;

-- One row: whether a file was loaded (``unconfigured`` when it is
-- absent), how many addresses it listed and the SHA-256 of the sorted
-- addresses, each followed by a newline (``identity_digest``).
CREATE TABLE operator_identity (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    state          TEXT NOT NULL CHECK (state IN ('configured', 'unconfigured')),
    address_count  INTEGER NOT NULL,
    address_digest TEXT NOT NULL
);

-- Unconfigured until the indexer's next start writes the file's state.
-- The digest is that of the empty set (SHA-256 of the empty string, not
-- a secret). No per-message column changes, so nothing is re-parsed.
INSERT INTO operator_identity (id, state, address_count, address_digest)
VALUES (1, 'unconfigured', 0,
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'); -- pragma: allowlist secret

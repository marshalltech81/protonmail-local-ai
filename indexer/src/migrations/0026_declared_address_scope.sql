-- Operator declarations are metadata, not account identity; no reparse.
CREATE TABLE account_address_scope (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    declared TEXT NOT NULL CHECK (declared IN ('combined', 'split', 'unverified')),
    previous_declared TEXT CHECK (previous_declared IN ('combined', 'split', 'unverified')),
    changed_at TEXT,
    CHECK ((previous_declared IS NULL) = (changed_at IS NULL))
);

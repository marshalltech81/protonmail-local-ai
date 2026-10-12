-- v13 -> v14 (#1596): the account registry. One row per account the
-- index serves, today exactly the configured one: ``ACCOUNT_SCOPE`` and
-- its ``SOURCE_MODE`` provider, read through the connection-local
-- functions ``Database`` registers before the runner starts
-- (``account.register_account_functions``). The row commits with the
-- version stamp or not at all. Rows are never changed or removed.
CREATE TABLE accounts (
    id         INTEGER PRIMARY KEY,
    scope_uuid TEXT NOT NULL UNIQUE,
    provider   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER accounts_never_updated BEFORE UPDATE ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts rows are never changed');
END;
CREATE TRIGGER accounts_never_deleted BEFORE DELETE ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts rows are never removed');
END;

INSERT INTO accounts (scope_uuid, provider, created_at)
VALUES (pmai_account_scope(), pmai_account_provider(),
        strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

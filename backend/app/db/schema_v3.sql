-- Schema version 3 (Gmail import). Added on top of schema.sql + schema_v2.sql; no existing table changes.
-- Applied by init_db for a fresh database and by `python -m app.db.migrate` for a version-2 database.
-- Statements are separated by ";" at the end of a line (the migration runs them one by one inside ONE transaction).

-- One row per connected mail account. The refresh token is stored ONLY encrypted (Fernet, key from OAUTH_ENCRYPTION_KEY);
-- access tokens are never stored. key_fingerprint identifies the key that encrypted the row ("reconnect" if it changed).
CREATE TABLE oauth_credentials (
    id                 INTEGER PRIMARY KEY,
    provider           TEXT NOT NULL CHECK (provider IN ('google')),
    account_email      TEXT NOT NULL,
    scopes             TEXT NOT NULL,
    refresh_token_enc  BLOB NOT NULL,
    key_fingerprint    TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (provider, account_email)
);

-- One row per imported email attachment: the dedupe key (account, message, file hash) and the provenance record.
-- run_id has no foreign key: the run row is created later by the worker, and an attachment whose run never started stays recorded.
CREATE TABLE gmail_imports (
    id                 INTEGER PRIMARY KEY,
    account_email      TEXT NOT NULL,
    message_id         TEXT NOT NULL,
    attachment_sha256  TEXT NOT NULL,
    part_id            TEXT NOT NULL,
    filename           TEXT,
    mime_type          TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL CHECK (size_bytes > 0),
    sender             TEXT,
    message_date       TEXT,
    run_id             TEXT NOT NULL,
    imported_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (account_email, message_id, attachment_sha256)
);
CREATE INDEX idx_gmail_imports_msg ON gmail_imports(account_email, message_id);

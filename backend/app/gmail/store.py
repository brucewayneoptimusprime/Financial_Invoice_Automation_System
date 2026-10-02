"""The ONLY reader and writer of the Gmail tables (oauth_credentials, gmail_imports). A structural test enforces it.

Nothing here ever returns or logs a decrypted token; `credential_meta` deliberately leaves the encrypted column out.
"""
import sqlite3

from app.enums import OAuthProvider


def credential_meta(conn: sqlite3.Connection, provider: str = OAuthProvider.GOOGLE.value) -> dict | None:
    """The connected account for a provider (v1: at most one), without the encrypted token."""
    row = conn.execute("SELECT account_email, scopes, key_fingerprint, created_at, updated_at FROM oauth_credentials "
                       "WHERE provider = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (provider,)).fetchone()
    return None if row is None else dict(row)


def imported_parts(conn: sqlite3.Connection, account_email: str, message_ids: list[str]) -> dict[tuple[str, str, int], str]:
    """{(message_id, part_id, size_bytes): run_id} of attachments already imported from these messages. Used to mark the preview;
    the dedupe at import is by message id + SHA-256 of the downloaded bytes."""
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = conn.execute(f"SELECT message_id, part_id, size_bytes, run_id FROM gmail_imports WHERE account_email = ? "
                        f"AND message_id IN ({marks}) ORDER BY id", (account_email, *message_ids)).fetchall()
    return {(r["message_id"], r["part_id"], r["size_bytes"]): r["run_id"] for r in rows}


def import_run(conn: sqlite3.Connection, account_email: str, message_id: str, attachment_sha256: str) -> str | None:
    """The run of an earlier import of exactly this file from exactly this email (the dedupe key)."""
    row = conn.execute("SELECT run_id FROM gmail_imports WHERE account_email = ? AND message_id = ? AND attachment_sha256 = ?",
                       (account_email, message_id, attachment_sha256)).fetchone()
    return None if row is None else row["run_id"]


def processed_run(conn: sqlite3.Connection, account_email: str, attachment_sha256: str) -> str | None:
    """The run that already has this exact file through another path: imported from another email of this account, or saved as
    an invoice by any run (upload or import). None when the file is new."""
    row = conn.execute("SELECT run_id FROM gmail_imports WHERE account_email = ? AND attachment_sha256 = ? ORDER BY id LIMIT 1",
                       (account_email, attachment_sha256)).fetchone()
    if row is not None:
        return row["run_id"]
    row = conn.execute("SELECT run_id FROM invoices WHERE file_hash = ? AND run_id IS NOT NULL ORDER BY id LIMIT 1",
                       (attachment_sha256,)).fetchone()
    return None if row is None else row["run_id"]


def record_import(conn: sqlite3.Connection, *, account_email: str, message_id: str, attachment_sha256: str, part_id: str,
                  filename: str | None, mime_type: str, size_bytes: int, sender: str | None, message_date: str | None,
                  run_id: str) -> None:
    """One provenance row. Raises sqlite3.IntegrityError when the same (account, email, file) is already recorded."""
    with conn:
        conn.execute("INSERT INTO gmail_imports (account_email, message_id, attachment_sha256, part_id, filename, mime_type, size_bytes, "
                     "sender, message_date, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (account_email, message_id, attachment_sha256, part_id, filename, mime_type, size_bytes, sender, message_date,
                      run_id))


def load_credential(conn: sqlite3.Connection, provider: str = OAuthProvider.GOOGLE.value) -> dict | None:
    """The connected account WITH its encrypted token (for the service to decrypt in memory; never returned by an endpoint)."""
    row = conn.execute("SELECT id, account_email, scopes, refresh_token_enc, key_fingerprint, created_at, updated_at "
                       "FROM oauth_credentials WHERE provider = ? ORDER BY updated_at DESC, id DESC LIMIT 1", (provider,)).fetchone()
    return None if row is None else dict(row)


def save_credential(conn: sqlite3.Connection, *, account_email: str, scopes: str, refresh_token_enc: bytes, key_fingerprint: str,
                    provider: str = OAuthProvider.GOOGLE.value) -> None:
    """The one connected account (v1): any other account of the provider is removed, this one inserted or replaced."""
    with conn:
        conn.execute("DELETE FROM oauth_credentials WHERE provider = ? AND account_email != ?", (provider, account_email))
        conn.execute("INSERT INTO oauth_credentials (provider, account_email, scopes, refresh_token_enc, key_fingerprint) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, account_email) DO UPDATE SET scopes = excluded.scopes, "
                     "refresh_token_enc = excluded.refresh_token_enc, key_fingerprint = excluded.key_fingerprint, "
                     "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
                     (provider, account_email, scopes, refresh_token_enc, key_fingerprint))


def delete_credentials(conn: sqlite3.Connection, provider: str = OAuthProvider.GOOGLE.value) -> int:
    with conn:
        return conn.execute("DELETE FROM oauth_credentials WHERE provider = ?", (provider,)).rowcount

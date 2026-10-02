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

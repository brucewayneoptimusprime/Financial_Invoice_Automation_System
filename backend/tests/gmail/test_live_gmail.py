"""The ONE live test against the real Gmail API (SPEC section 11 item 87). Deselected by default; run it with

    python -m pytest -m live -k gmail

It is skipped unless Gmail import is set up (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_ENCRYPTION_KEY) AND an account was
connected through the UI (a credential in the configured database). It lists at most ONE message id, downloads nothing, changes
nothing, and asserts that the stored grant is exactly gmail.readonly. Unlike the app, it never deletes the credential on failure.
"""
from contextlib import closing

import pytest

from app.config import Settings
from app.db.connection import connect
from app.db.init_db import SCHEMA_VERSION, schema_version
from app.gmail import store
from app.gmail.crypto import TokenCipher
from app.gmail.google_client import GoogleGmailClient
from app.gmail.scopes import GMAIL_READONLY_SCOPE
from app.gmail.service import GmailService

pytestmark = pytest.mark.live


def test_the_connected_inbox_answers_read_only():
    s = Settings()
    if s.gmail_backend_effective() != "google":
        pytest.skip(f"Gmail import is not set up (missing: {', '.join(s.gmail_missing()) or 'GMAIL_BACKEND is not google'})")
    if not s.db_path.is_file():
        pytest.skip(f"no database at {s.db_path}")
    with closing(connect(s.db_path)) as conn:
        if schema_version(conn) != SCHEMA_VERSION:
            pytest.skip("the database is not at the current schema; run python -m app.db.migrate")
        cred = store.load_credential(conn)
    if cred is None:
        pytest.skip("no Gmail account is connected: connect the test inbox in the UI first")
    assert cred["scopes"] == GMAIL_READONLY_SCOPE
    svc = GmailService(s, s.db_path)
    try:
        refresh = TokenCipher.from_settings(s).decrypt(cred["refresh_token_enc"], cred["key_fingerprint"])
        client = GoogleGmailClient(svc.http, svc.oauth, refresh)          # no on_reconnect: a live test never deletes the credential
        assert client.profile().lower() == cred["account_email"].lower()
        ids, estimate = client.search("has:attachment newer_than:3650d", 1)
        assert len(ids) <= 1 and estimate >= len(ids)
    finally:
        svc.close()

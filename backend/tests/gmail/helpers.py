"""Shared setup for the Gmail tests: the fake inbox, settings for the fake backend, and an app built the same way as the API tests."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from app.gmail.fake import FakeGmailClient
from tests.api.helpers import api_settings, build_app

FAKE_INBOX = ROOT_DIR / "data" / "gmail_fake" / "inbox.json"


def inbox_data() -> dict:
    return json.loads(FAKE_INBOX.read_text(encoding="utf-8"))


def fake_client(**changes) -> FakeGmailClient:
    data = copy.deepcopy(inbox_data())
    data.update(changes)
    return FakeGmailClient(data)


def gmail_settings(tmp_path: Path, **kw):
    kw.setdefault("gmail_backend", "fake")
    return api_settings(tmp_path, **kw)


@contextmanager
def gmail_api(tmp_path: Path, *, settings=None, gmail_client=None, run_fn=None, tracker=None, inner_client=None, mode="offline", **kw):
    """(TestClient, app) with the fake Gmail backend unless told otherwise. `inner_client` (a scripted model double) + mode="live"
    make the translator and the labeller available; the default (offline) leaves both off."""
    settings = settings or gmail_settings(tmp_path, **kw)
    app, worker, db, settings = build_app(tmp_path, settings=settings, run_fn=run_fn, tracker=tracker, inner_client=inner_client, mode=mode)
    if gmail_client is not None:
        app.state.api.gmail._client_override = gmail_client
    with TestClient(app) as c:
        yield c, app


def table_counts(db: Path) -> dict[str, int]:
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(db)) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}

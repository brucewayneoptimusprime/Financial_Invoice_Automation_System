"""Records the Gmail panel's frontend fixtures from the REAL endpoints, with the labelled fake inbox (no Google, no model).

    cd backend && python -m tests.gmail.frontend_fixtures      writes frontend/src/test/fixtures/gmail_*.json

`test_frontend_fixtures_match_the_endpoints` regenerates them in memory and fails if the committed files drifted in shape.
"""
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from tests.api.helpers import build_app
from tests.gmail.helpers import gmail_settings
from tests.gmail.regression import HashRuns

OUT = ROOT_DIR / "frontend" / "src" / "test" / "fixtures"
NAMES = ("gmail_status_fake", "gmail_status_disabled", "gmail_status_google", "gmail_search", "gmail_search_422", "gmail_import",
         "gmail_run_view")


def _status(tmp: Path, **kw) -> dict:
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp, **kw))
    with TestClient(app) as c:
        return c.get("/api/gmail/status").json()


def generate(tmp: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    out["gmail_status_disabled"] = _status(tmp / "disabled", gmail_backend=None)
    out["gmail_status_google"] = _status(tmp / "google", gmail_backend=None, google_client_id="test-client-id",
                                         google_client_secret="test-client-secret", oauth_encryption_key=Fernet.generate_key().decode())
    root = tmp / "fake"
    root.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(root, settings=gmail_settings(root), run_fn=HashRuns())
    with TestClient(app) as c:
        out["gmail_status_fake"] = c.get("/api/gmail/status").json()
        out["gmail_search_422"] = c.post("/api/gmail/search", json={"query": "in:anywhere label:finance invoice"}).json()
        first = c.post("/api/gmail/search", json={"query": "after:2026/08/01"}).json()
        c.post("/api/gmail/import", json={"search_id": first["search_id"], "confirm": True,
                                          "items": [{"message_id": "fake-ss-10963", "part_id": "1"}]})
        assert worker.wait_idle(120)
        search = c.post("/api/gmail/search", json={"query": "after:2026/08/01"}).json()          # 10963 now shows "imported"
        out["gmail_search"] = search
        imported = c.post("/api/gmail/import", json={"search_id": search["search_id"], "confirm": True, "items": [
            {"message_id": "fake-ss-two", "part_id": "1"}, {"message_id": "fake-ss-10963", "part_id": "1"},
            {"message_id": "fake-acme", "part_id": "2"}, {"message_id": "fake-injection", "part_id": "1"},
            {"message_id": "fake-ss-24429", "part_id": "1"}]}).json()          # the same file as fake-injection: already_processed
        out["gmail_import"] = imported
        assert worker.wait_idle(120)
        out["gmail_run_view"] = c.get(f"/api/runs/{imported['items'][0]['run_id']}").json()
    return out


def write(out: dict[str, dict]) -> None:
    for name, body in out.items():
        (OUT / f"{name}.json").write_bytes((json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    for secret in ("ANTHROPIC_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY", "GMAIL_BACKEND"):
        os.environ[secret] = ""                                   # as in conftest: never the developer's real key or OAuth client
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        write(generate(Path(d)))
    print(f"wrote {len(NAMES)} fixtures to {OUT}")

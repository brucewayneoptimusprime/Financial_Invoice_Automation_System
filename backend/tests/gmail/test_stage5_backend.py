"""Gmail import, stage 5 (the UI panel): the backend pieces the panel relies on. The run view says where the file came from, and the
panel's recorded fixtures still have the shape the real endpoints produce."""
import json

from app.config import ROOT_DIR
from tests.gmail.frontend_fixtures import NAMES, OUT, generate
from tests.gmail.helpers import gmail_api
from tests.gmail.regression import HashRuns

INVOICES = ROOT_DIR / "data" / "invoices"


def test_the_run_view_names_the_gmail_source_and_an_upload_has_none(tmp_path):
    with gmail_api(tmp_path, run_fn=HashRuns()) as (c, app):
        s = c.post("/api/gmail/search", json={"query": "10963"}).json()
        gmail_run = c.post("/api/gmail/import", json={"search_id": s["search_id"], "confirm": True,
                                                      "items": [{"message_id": "fake-ss-10963", "part_id": "1"}]}).json()["items"][0]["run_id"]
        up = c.post("/api/runs", files={"file": ("u.pdf", (INVOICES / "invoice_Liz Thompson_14130.pdf").read_bytes(), "application/pdf")})
        assert app.state.api.worker.wait_idle(60)
        g, u = c.get(f"/api/runs/{gmail_run}").json(), c.get(f"/api/runs/{up.json()['run_id']}").json()
    assert g["source"] == {"kind": "gmail", "sender": "SuperStore Billing <billing@superstore.example>",
                           "message_date": "2026-09-03T09:12:00Z", "filename": "invoice_Scot Wooten_10963.pdf"}
    assert u["source"] is None


def _shape(value):
    """Keys and types, recursively (the first element stands for a list); values themselves may differ (ids, timestamps)."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__ if value is not None else "null"


def test_frontend_fixtures_match_the_endpoints(tmp_path):
    fresh = generate(tmp_path)
    assert set(fresh) == set(NAMES)
    for name in NAMES:
        committed = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert _shape(committed) == _shape(fresh[name]), f"{name}.json drifted: run `python -m tests.gmail.frontend_fixtures`"

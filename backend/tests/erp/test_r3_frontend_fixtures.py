"""The ERP sync UI's frontend fixtures are recorded from the real endpoints; this fails if the committed files drifted in shape.
Also: the export's Entered wording for an ERP PO equals the screen's ("Simulated ERP feed")."""
import json

from fastapi.testclient import TestClient

from tests.api.helpers import build_app
from tests.erp.frontend_fixtures import NAMES, OUT, generate
from tests.gmail.test_stage5_backend import _shape


def test_erp_frontend_fixtures_match_the_endpoints(tmp_path):
    fresh = generate(tmp_path)
    assert set(fresh) == set(NAMES)
    for name in NAMES:
        committed = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert _shape(committed) == _shape(fresh[name]), f"{name}.json drifted: run `python -m tests.erp.frontend_fixtures`"


def test_exports_say_simulated_erp_feed(tmp_path):
    app, *_ = build_app(tmp_path)
    with TestClient(app) as c:
        p = c.get("/api/erp/preview").json()
        res = c.post("/api/erp/import", json={"feed_sha256": p["feed"]["sha256"], "po_numbers": ["4500012001"], "confirm": True}).json()
        pid = res["results"][0]["po_id"]
        summary = c.get("/api/pos/export?format=csv").content.decode("utf-8-sig")
        detail = c.get(f"/api/pos/{pid}/export?format=csv&level=full").content.decode("utf-8-sig")
    row = next(line for line in summary.splitlines() if line.startswith("4500012001"))
    assert row.endswith("Simulated ERP feed")
    assert "Entered by,Simulated ERP feed" in detail and "Feed,erp_feed_sample.json" in detail and "ERP status,RELEASED" in detail

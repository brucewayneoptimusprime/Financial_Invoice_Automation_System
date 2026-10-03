"""Records the ERP sync UI's frontend fixtures from the REAL endpoints (demo database, the bundled sample feed; no model).

    cd backend && python -m tests.erp.frontend_fixtures      writes frontend/src/test/fixtures/erp_*.json

`test_erp_frontend_fixtures_match_the_endpoints` regenerates them in memory and fails if the committed files drifted in shape.
"""
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from tests.api.helpers import build_app

OUT = ROOT_DIR / "frontend" / "src" / "test" / "fixtures"
NAMES = ("erp_preview", "erp_import", "erp_preview_after", "erp_po_detail", "erp_po_list")


def generate(tmp: Path) -> dict[str, dict]:
    app, worker, db, settings = build_app(tmp)
    out: dict[str, dict] = {}
    with TestClient(app) as c:
        out["erp_preview"] = c.get("/api/erp/preview").json()
        p = out["erp_preview"]
        picks = ["4500012001", "4500012004", "PO-SS-005"]                       # two new (one with a new vendor) and one existing
        out["erp_import"] = c.post("/api/erp/import", json={"feed_sha256": p["feed"]["sha256"], "po_numbers": picks,
                                                            "confirm": True}).json()
        out["erp_preview_after"] = c.get("/api/erp/preview").json()
        po_id = next(r["po_id"] for r in out["erp_import"]["results"] if r["outcome"] == "imported")
        out["erp_po_detail"] = c.get(f"/api/pos/{po_id}").json()
        out["erp_po_list"] = c.get("/api/pos").json()
    return out


def write(out: dict[str, dict]) -> None:
    for name, body in out.items():
        (OUT / f"{name}.json").write_bytes((json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    for secret in ("ANTHROPIC_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY", "GMAIL_BACKEND", "ACCESS_TOKEN"):
        os.environ[secret] = ""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        write(generate(Path(d)))
    print(f"wrote {len(NAMES)} fixtures to {OUT}")

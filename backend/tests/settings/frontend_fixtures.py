"""Records the settings UI's frontend fixtures from the REAL endpoints (demo database, scripted runs; no model, no Google).

    cd backend && python -m tests.settings.frontend_fixtures      writes frontend/src/test/fixtures/settings_*.json

`test_settings_frontend_fixtures_match_the_endpoints` regenerates them in memory and fails if the committed files drifted in shape.
"""
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from tests.api.helpers import build_app, run_and_wait
from tests.review.helpers import SS_10963, runs_with_scenarios

OUT = ROOT_DIR / "frontend" / "src" / "test" / "fixtures"
NAMES = ("settings_global", "settings_pos", "settings_po_custom", "settings_po_default", "settings_history", "settings_run_view")


def generate(tmp: Path) -> dict[str, dict]:
    app, worker, db, settings = build_app(tmp, run_fn=runs_with_scenarios())
    out: dict[str, dict] = {}
    with TestClient(app) as c:
        c.worker = worker
        c.post("/api/settings/global", json={"values": {"duplicate_days": 10}})
        c.post("/api/settings/pos/1", json={"values": {"tolerance_pct": 5, "tolerance_abs": "100.00", "confidence_threshold": 0.9},
                                            "rules": {"r_po_line_price": False}})
        out["settings_global"] = c.get("/api/settings").json()
        out["settings_pos"] = c.get("/api/settings/pos").json()
        out["settings_po_custom"] = c.get("/api/settings/pos/1").json()
        out["settings_po_default"] = c.get("/api/settings/pos/2").json()
        out["settings_history"] = c.get("/api/settings/history").json()
        rid = run_and_wait(c, SS_10963)                                         # matched to PO-SS-001: judged under its overrides
        out["settings_run_view"] = c.get(f"/api/runs/{rid}").json()
    return out


def write(out: dict[str, dict]) -> None:
    for name, body in out.items():
        (OUT / f"{name}.json").write_bytes((json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    for secret in ("ANTHROPIC_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_ENCRYPTION_KEY", "GMAIL_BACKEND"):
        os.environ[secret] = ""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        write(generate(Path(d)))
    print(f"wrote {len(NAMES)} fixtures to {OUT}")

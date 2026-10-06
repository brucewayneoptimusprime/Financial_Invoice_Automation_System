"""The six real invoices, pinned (stage C0, from `deploy` at 60ae234), and unchanged by a cross-check run in the same database."""
from pathlib import Path

from fastapi.testclient import TestClient

from tests.api.helpers import build_app
from tests.extraction.real import real_pdf
from tests.gmail.helpers import gmail_settings
from tests.gmail.regression import SIX, HashRuns, outcome

EXPECTED = {  # decision, matched PO, triggered rules: the behaviour of `deploy` before this feature existed
    "superstore_10963": ("review", "PO-SS-001", ["r_po_found:matched_without_reference"]),
    "superstore_24429": ("review", "PO-SS-002", ["r_po_found:matched_without_reference"]),
    "superstore_14021": ("review", "PO-SS-003", ["r_po_found:matched_without_reference"]),
    "superstore_14130": ("review", "PO-SS-005", ["r_po_found:matched_without_reference"]),
    "superstore_6459": ("review", "PO-SS-004", ["r_po_found:matched_without_reference"]),
    "iq_electronics": ("review", "PO-IQ-2025-001", ["engine_floor:floor_applied", "r_extraction_confidence:low_confidence",
                                                     "r_po_found:matched_without_reference"]),
}


def six_outcomes(tmp: Path, between=None) -> dict[str, dict]:
    """Upload the six through the API. `between(client)` runs after the first three, so the last three are judged after it."""
    tmp.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp), run_fn=HashRuns())
    out = {}
    with TestClient(app) as c:
        c.db_path, c.settings = db, settings
        ids = {}
        for i, (name, (_, _, filename)) in enumerate(SIX.items()):
            if i == 3:
                assert worker.wait_idle(180)
                if between is not None:
                    between(c)
            ids[name] = c.post("/api/runs", files={"file": (filename, real_pdf(name).read_bytes(), "application/octet-stream")}).json()["run_id"]
        assert worker.wait_idle(180)
        if between is not None:
            between(c)
        for name, rid in ids.items():
            out[name] = outcome(c.get(f"/api/runs/{rid}").json())
    return out


def test_the_six_real_invoices_produce_the_decisions_pinned_from_deploy(tmp_path):
    got = six_outcomes(tmp_path)
    for name, (decision, po, triggered) in EXPECTED.items():
        assert (got[name]["decision"], got[name]["matched_po"], got[name]["triggered"]) == (decision, po, triggered), name

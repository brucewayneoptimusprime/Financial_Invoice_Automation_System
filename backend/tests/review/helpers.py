"""Review items for the review-action tests. Real invoice 10963 (v5 reply) is held for review by the shipped rules; the SYNTHETIC
line-item scenarios (tests/pipeline/test_line_items_synthetic.py) are held for review by making their test-only vendor `new`."""
import json
from contextlib import closing

from app.api.worker import open_db
from tests.api.helpers import ScriptedRuns, api, run_and_wait, upload
from tests.extraction.helpers import make_source
from tests.extraction.real import REAL
from tests.pipeline.test_line_items_synthetic import ITEMISED, LOOKALIKES, SCENARIOS, add_northwind

SS_10963 = "superstore_10963"


def v5(name):
    return json.loads((REAL / f"{name}.v5.reply.json").read_text(encoding="utf-8"))


def runs_with_scenarios() -> ScriptedRuns:
    return ScriptedRuns({SS_10963: v5(SS_10963), **{f"scenario_{k}": r for k, (_, r) in SCENARIOS.items()}})


def db(c):
    return closing(open_db(c.db_path, c.settings))


def add_new_vendor_po(c, po_lines):
    """The test-only Northwind vendor and PO-5001, with the vendor `new` so the shipped r_vendor_status holds the invoice for review."""
    with db(c) as conn:
        add_northwind(conn, po_lines)
        with conn:
            conn.execute("UPDATE vendors SET status = 'new' WHERE id = 90")


def run_scenario(c, tmp_path, scenario) -> str:
    png = make_source(tmp_path / scenario, "png").read_bytes()
    rid = c.post("/api/runs", files={"file": (f"scenario_{scenario}.png", png)}).json()["run_id"]
    assert c.worker.wait_idle(60)
    return rid


def item_for(c, run_id) -> int:
    with db(c) as conn:
        return conn.execute("SELECT id FROM review_queue WHERE run_id = ?", (run_id,)).fetchone()[0]


def counts(c, *tables):
    with db(c) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


__all__ = ["api", "run_and_wait", "upload", "ITEMISED", "LOOKALIKES"]

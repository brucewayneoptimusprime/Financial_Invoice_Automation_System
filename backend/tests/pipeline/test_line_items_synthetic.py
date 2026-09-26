"""Line-item consumption stage 4: four labelled SYNTHETIC scenarios through the WHOLE pipeline, plus the stored data the reviewer's
line picker will use. The invoice is the generated Northwind test invoice (no real client document), photographed as a PNG so no
text layer caps the edited values; the extraction reply is the recorded-style Northwind fixture edited per scenario (labelled in its
notes); the PO is a test-only multi-line PO for a test-only vendor. The demo seed and the six real invoices are untouched.

  clean         invoice Widget A 10 x 60.00, Widget B 5 x 80.00  vs  PO lines Widget A / Widget B / Widget C
  ambiguous     the same invoice                                 vs  PO lines Widget A (blue) / Widget A (green) / Widget B
  bundled       one line "Goods as per purchase order PO-5001"   vs  the itemised PO
  price         Widget A billed at 66.00                         vs  the PO's 60.00
"""
import json
from contextlib import closing

import pytest

from app.db.consumption import consumption_problems
from app.enums import Decision, LineMatchStatus
from app.pipeline.runner import run_pipeline
from tests.api.helpers import ScriptedRuns, api
from tests.extraction.helpers import load_reply, make_source
from tests.extraction.wire_convert import set_field
from tests.pipeline.helpers import cfg, demo_db, scripted

LABEL = "[SYNTHETIC LINE-ITEM SCENARIO: generated Northwind invoice, edited reply, test-only PO]"
ITEMISED = [("Widget A", "10", "60.00", 60000), ("Widget B", "5", "80.00", 40000), ("Widget C", "2", "50.00", 10000)]
LOOKALIKES = [("Widget A (blue)", "10", "60.00", 60000), ("Widget A (green)", "10", "60.00", 60000), ("Widget B", "5", "80.00", 40000)]


def add_northwind(conn, po_lines):
    with conn:
        conn.execute("INSERT INTO vendors (id, name, aliases, tax_id, country, status) VALUES (90, 'Northwind Trading Co', '[]', "
                     "'12-3456789', 'US', 'approved')")
        conn.execute("INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, issued_date, status, meta) "
                     "VALUES (90, 'PO-5001', 90, 'USD', 150000, '2026-03-01', 'open', '{\"test_only\": true}')")
        for n, (desc, qty, price, amount) in enumerate(po_lines, start=1):
            conn.execute("INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) VALUES (90,?,?,?,?,?)",
                         (n, desc, qty, price, amount))


def reply(lines=None, subtotal=None, total=None):
    r = load_reply("us_native_invoice")
    if lines is not None:
        r["line_items"] = [{"description": d, "item_code": "", "quantity": q, "unit_price": p, "amount": a, "page": 1,
                            "source_text": f"{d} {q} {p} {a}", "confidence": 0.95} for d, q, p, a in lines]
    if subtotal is not None:
        set_field(r, "subtotal", value=subtotal, source_text=f"Subtotal: {subtotal}")
    if total is not None:
        set_field(r, "total", value=total, source_text=f"Total Due: {total}")
    r["extraction_notes"] = (r.get("extraction_notes") or "") + " " + LABEL
    return r


SCENARIOS = {
    "clean": (ITEMISED, reply()),
    "ambiguous": (LOOKALIKES, reply()),
    "bundled": (ITEMISED, reply(lines=[("Goods as per purchase order PO-5001", "1", "1000.00", "1000.00")])),
    "price": (ITEMISED, reply(lines=[("Widget A", "10", "66.00", "660.00"), ("Widget B", "5", "80.00", "400.00")],
                              subtotal="1060.00", total="1165.00")),
}


def run(tmp_path, scenario):
    po_lines, r = SCENARIOS[scenario]
    conn = demo_db(tmp_path)
    add_northwind(conn, po_lines)
    result = run_pipeline(make_source(tmp_path, "png"), conn, client=scripted(r), settings=cfg(tmp_path))
    return conn, result


def rule(result, rule_id):
    return next(x for x in result.ctx.rule_results if x.rule_id == rule_id)


def stored(conn, run_id):
    return [dict(x) for x in conn.execute("SELECT m.*, il.line_no FROM invoice_line_matches m JOIN invoice_lines il ON il.id = m.invoice_line_id "
                                          "WHERE m.run_id = ? ORDER BY il.line_no", (run_id,))]


# ------------------------------------------------------------------------------------------ the four scenarios

def test_clean_line_for_line(tmp_path):
    conn, r = run(tmp_path, "clean")
    with closing(conn):
        lm = r.ctx.line_matches
        assert r.ctx.matched_po.po_number == "PO-5001" and lm.mode == "line_level"
        assert [(x.status, x.po_line_no) for x in lm.lines] == [(LineMatchStatus.MATCHED, 1), (LineMatchStatus.MATCHED, 2)]
        assert rule(r, "r_po_line_price").outcome_key == "within_tolerance"
        assert r.decision is Decision.APPROVE
        rows = stored(conn, r.run_id)
        assert [(x["line_no"], x["status"], x["score"]) for x in rows] == [(1, "matched", 1.0), (2, "matched", 1.0)]
        line_ids = [x[0] for x in conn.execute("SELECT id FROM po_lines WHERE po_id = 90 ORDER BY line_no")]
        assert [x["po_line_id"] for x in rows] == line_ids[:2]
        cons = [dict(x) for x in conn.execute("SELECT * FROM po_consumption WHERE run_id = ?", (r.run_id,))]
        assert [(c["po_line_id"], c["amount"], c["matched_by"]) for c in cons] == [(None, 110500, "auto")]   # total-only (decision 2)
        assert consumption_problems(conn) == []


def test_ambiguous_line_match(tmp_path):
    conn, r = run(tmp_path, "ambiguous")
    with closing(conn):
        lm = r.ctx.line_matches
        first, second = lm.lines
        assert first.status is LineMatchStatus.AMBIGUOUS and first.po_line_id is None
        assert [c.po_line_no for c in first.candidates[:2]] == [1, 2] and first.candidates[0].score - first.candidates[1].score < 0.10
        assert second.status is LineMatchStatus.MATCHED and second.po_line_no == 3 and lm.mode == "partial"
        assert rule(r, "r_po_line_price").detail["skipped"] == [{"invoice_line_no": 1, "reason": "line match ambiguous"}]
        assert r.decision is Decision.APPROVE                                 # owner decision 3: line matching changes no decision yet
        rows = stored(conn, r.run_id)
        cands = json.loads(rows[0]["candidates"])
        assert rows[0]["status"] == "ambiguous" and rows[0]["po_line_id"] is None and [c["po_line_no"] for c in cands] == [1, 2, 3]
        # "Widget A" vs "Widget B" is 0.88 on description alone (short names one letter apart); price, quantity and amount keep it
        # far below a match (0.575 < 0.75), so it is listed for the reviewer but never chosen
        assert cands[2]["score"] < 0.60 and "price:differs(25.0%)" in cands[2]["reasons"]


def test_bundled_invoice_against_an_itemised_po(tmp_path):
    conn, r = run(tmp_path, "bundled")
    with closing(conn):
        lm = r.ctx.line_matches
        assert lm.mode == "total_only" and lm.bundled_hint and lm.lines[0].status is LineMatchStatus.NO_MATCH
        assert r.ctx.matched_po.po_number == "PO-5001"                      # the whole-PO match (by reference) is unaffected
        assert rule(r, "r_po_line_price").outcome_key == "not_evaluable" and r.decision is Decision.APPROVE
        assert stored(conn, r.run_id)[0]["status"] == "no_match"


def test_price_mismatch(tmp_path):
    conn, r = run(tmp_path, "price")
    with closing(conn):
        lm = r.ctx.line_matches
        assert [x.status for x in lm.lines] == [LineMatchStatus.MATCHED, LineMatchStatus.MATCHED] and lm.lines[0].score == pytest.approx(0.93)
        res = rule(r, "r_po_line_price")
        assert (res.outcome_key, res.severity) == ("price_above_po", 1)
        c = res.detail["lines"][0]
        assert (c["invoice_unit_price"], c["po_unit_price"], c["difference"], c["allowance"]) == ("66.00", "60.00", "6.00", "0.600")
        assert rule(r, "r_arithmetic").outcome.value == "pass" and rule(r, "r_tolerance_pct").outcome_key == "within_balance"
        assert r.decision is Decision.REVIEW
        assert [(f.rule_id, f.outcome_key) for f in r.digest.triggered] == [("r_po_line_price", "price_above_po")]


def test_every_scenario_is_labelled_synthetic():
    for _, r in SCENARIOS.values():
        assert LABEL in r["extraction_notes"]


# ------------------------------------------------------------------------------------------ the picker's data through the API

def test_the_run_view_carries_the_picker_data(tmp_path):
    runs = ScriptedRuns({"ambiguous_photo": SCENARIOS["ambiguous"][1]})
    with api(tmp_path, run_fn=runs) as c:
        from app.api.worker import open_db
        with closing(open_db(c.db_path, c.settings)) as conn:
            add_northwind(conn, LOOKALIKES)
        png = make_source(tmp_path, "png").read_bytes()
        rid = c.post("/api/runs", files={"file": ("ambiguous_photo.png", png)}).json()["run_id"]
        assert c.worker.wait_idle(60)
        v = c.get(f"/api/runs/{rid}").json()["line_matches"]
        po = c.get("/api/pos/90").json()
    assert v["mode"] == "partial" and v["po"]["po_number"] == "PO-5001" and len(v["po_lines"]) == 3
    first = v["lines"][0]
    assert (first["invoice_line_no"], first["status"], first["po_line_id"], first["description"]) == (1, "ambiguous", None, "Widget A")
    cand = first["candidates"][0]
    assert {"po_line_id", "po_line_no", "score", "breakdown", "reasons", "description", "unit_price", "remaining_quantity",
            "remaining_amount"} <= set(cand)
    assert cand["description"] == "Widget A (blue)" and cand["remaining_quantity"] == "10" and cand["remaining_amount"] == "600.00"
    assert v["po_consumption"] == {"consumed_by_lines": "0.00", "consumed_without_line": "1105.00"}   # the approve: total-only
    assert po["amounts"]["consumed_without_line"] == "1105.00" and po["lines"][0]["remaining_quantity"] == "10"


def test_a_run_without_a_matched_po_has_empty_picker_data(tmp_path):
    with api(tmp_path) as c:
        from tests.api.helpers import SS_10963, run_and_wait
        from app.api.worker import open_db
        with closing(open_db(c.db_path, c.settings)) as conn:
            with conn:
                conn.execute("UPDATE purchase_orders SET vendor_id = 2 WHERE po_number LIKE 'PO-SS-%'")
        v = c.get(f"/api/runs/{run_and_wait(c, SS_10963)}").json()["line_matches"]
    assert v["mode"] == "not_evaluable" and v["po"] is None and v["lines"] == [] and v["reason"]

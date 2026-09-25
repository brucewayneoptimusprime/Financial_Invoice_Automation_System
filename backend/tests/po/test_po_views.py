"""PO list and PO detail (PO integration stage 2): derived balances, matched invoices, ledger, 'also considered in'."""
from contextlib import closing

from app.api.worker import open_db
from tests.api.helpers import SS_10963, SS_24429, ScriptedRuns, api, run_and_wait
from tests.pipeline.helpers import controlled_variant_reply


def po_id(c, number):
    with closing(open_db(c.db_path, c.settings)) as conn:
        return conn.execute("SELECT id FROM purchase_orders WHERE po_number = ?", (number,)).fetchone()[0]


def test_vendors_are_listed_for_the_picker(tmp_path):
    with api(tmp_path) as c:
        v = c.get("/api/vendors").json()["vendors"]
    assert {x["name"] for x in v} == {"SuperStore", "Electronics Mart India Limited"} and all(x["status"] == "approved" for x in v)


def test_the_po_list_shows_derived_balances_and_filters(tmp_path):
    with api(tmp_path) as c:
        pos = c.get("/api/pos").json()["pos"]
        by = {p["po_number"]: p for p in pos}
        assert len(pos) == 6 and by["PO-SS-005"]["balance"] == "7500.00" and by["PO-SS-005"]["total"] == "9000.00"
        assert by["PO-SS-005"]["invoice_count"] == 1 and by["PO-IQ-2025-001"]["currency"] == "INR"
        assert [p["po_number"] for p in c.get("/api/pos?q=IQ").json()["pos"]] == ["PO-IQ-2025-001"]
        assert len(c.get("/api/pos?q=superstore").json()["pos"]) == 5                    # vendor name search
        assert c.get("/api/pos?q=%25").json()["pos"] == []                                # LIKE wildcards are literal
        assert c.get("/api/pos?status=closed").json()["pos"] == []
        assert len(c.get("/api/pos?status=bogus").json()["pos"]) == 6                     # unknown status filter ignored


def test_detail_of_a_seeded_po_with_a_historic_invoice(tmp_path):
    with api(tmp_path) as c:
        d = c.get(f"/api/pos/{po_id(c, 'PO-SS-005')}").json()
    assert d["po"]["vendor"] == "SuperStore" and d["amounts"] == {"total": "9000.00", "committed": "1500.00", "balance": "7500.00",
                                                                   "awaiting_review": "0.00", "over_billed": False}
    assert len(d["lines"]) == 1 and d["ledger"][0]["amount"] == "1500.00"
    assert d["invoices"][0]["historic"] is True and d["invoices"][0]["invoice_number"] == "HIST-SS-0001"


def test_detail_lists_the_runs_matched_to_it_with_status_and_decision(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)
        d = c.get(f"/api/pos/{po_id(c, 'PO-SS-001')}").json()
    inv = d["invoices"][0]
    assert (inv["run_id"], inv["decision"], inv["status"], inv["total"], inv["run_status"]) == (rid, "review", "in_review", "5338.08", "completed")
    assert d["amounts"]["awaiting_review"] == "5338.08" and d["amounts"]["balance"] == "6000.00"      # review does not consume


def test_an_approve_shows_in_the_ledger_and_the_balance(tmp_path):
    runs = ScriptedRuns({SS_24429: controlled_variant_reply(SS_24429, "PO-SS-002")})   # SYNTHETIC controlled variant
    with api(tmp_path, run_fn=runs) as c:
        run_and_wait(c, SS_24429)
        d = c.get(f"/api/pos/{po_id(c, 'PO-SS-002')}").json()
    assert d["amounts"]["committed"] == "1770.61" and d["amounts"]["balance"] == "729.39" and d["po"]["status"] == "partially_billed"
    assert d["ledger"][0]["type"] == "commit" and d["invoices"][0]["status"] == "approved"


def test_also_considered_in_lists_near_misses_but_not_the_matched_run(tmp_path):
    with api(tmp_path) as c:
        rid = run_and_wait(c, SS_10963)                                     # matched PO-SS-001; the others were ranked too
        other = c.get(f"/api/pos/{po_id(c, 'PO-SS-002')}").json()
        own = c.get(f"/api/pos/{po_id(c, 'PO-SS-001')}").json()
    near = other["considered_in"]
    assert [n["run_id"] for n in near] == [rid] and near[0]["matched_po"] == "PO-SS-001" and 0 < near[0]["score"] < 0.5
    assert own["considered_in"] == []


def test_an_unknown_po_is_404(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/pos/9999").status_code == 404
        assert c.get("/api/pos/abc").status_code == 422


def test_a_saved_po_appears_in_the_list_with_its_source(tmp_path):
    with api(tmp_path) as c:
        c.post("/api/pos", json={"po": {"po_number": "PO-M-1", "vendor_id": 2, "currency": "INR", "total": "100.00"}})
        top = c.get("/api/pos").json()["pos"][0]
    assert (top["po_number"], top["source"], top["balance"], top["status"]) == ("PO-M-1", "manual", "100.00", "open")

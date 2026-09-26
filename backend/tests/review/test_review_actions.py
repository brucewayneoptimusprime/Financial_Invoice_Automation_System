"""POST approve / reject (review actions + line allocation, stage 2). The owner's five required tests come first."""
import json

import pytest

from app.db.consumption import consumption_problems
from tests.review.helpers import (ITEMISED, LOOKALIKES, SS_10963, add_new_vendor_po, api, counts, db, item_for, run_and_wait, run_scenario,
                                  runs_with_scenarios)

TABLES = ("ledger_entries", "po_consumption", "invoices", "review_queue", "purchase_orders")


def preview(c, iid):
    return c.get(f"/api/review-queue/{iid}").json()["approve"]


def approve(c, iid, allocations=(), token=None, **kw):
    body = {"confirm": True, "state_token": token or preview(c, iid)["state_token"], "allocations": list(allocations), **kw}
    return c.post(f"/api/review-queue/{iid}/approve", json=body)


def state(c, iid):
    with db(c) as conn:
        item = dict(conn.execute("SELECT * FROM review_queue WHERE id = ?", (iid,)).fetchone())
        inv = dict(conn.execute("SELECT * FROM invoices WHERE run_id = ?", (item["run_id"],)).fetchone())
        cons = [dict(r) for r in conn.execute("SELECT * FROM po_consumption WHERE invoice_id = ? ORDER BY id", (inv["id"],))]
        ledger = [dict(r) for r in conn.execute("SELECT * FROM ledger_entries WHERE invoice_id = ?", (inv["id"],))]
        run = dict(conn.execute("SELECT * FROM runs WHERE id = ?", (item["run_id"],)).fetchone())
        problems = consumption_problems(conn)
    return item, inv, cons, ledger, run, problems


def line_ids(c, po_id=90):
    with db(c) as conn:
        return [r[0] for r in conn.execute("SELECT id FROM po_lines WHERE po_id = ? ORDER BY line_no", (po_id,))]


def invoice_line_id(c, iid, line_no):
    with db(c) as conn:
        return conn.execute("SELECT il.id FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id JOIN review_queue q ON q.run_id = i.run_id "
                            "WHERE q.id = ? AND il.line_no = ?", (iid, line_no)).fetchone()[0]


# ------------------------------------------------------------------------------------------ the five required tests

def test_1_all_lines_confident_needs_no_input_and_allocates_correctly(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        rid = run_and_wait(c, SS_10963)                                        # real invoice, extract-v5 reply
        iid = item_for(c, rid)
        r = approve(c, iid)                                                    # no allocations supplied at all
        assert r.status_code == 200, r.text
        body = r.json()
        item, inv, cons, ledger, run, problems = state(c, iid)
    assert body["amount"] == "5338.08" and body["po"] == {"id": 1, "po_number": "PO-SS-001", "balance_before": "6000.00",
                                                          "balance_after": "661.92", "status": "partially_billed"}
    assert [(x["po_line_id"], x["invoice_line_id"] is not None, x["amount"], x["quantity"], x["matched_by"]) for x in cons] == [
        (1, True, 514176, "4", "auto"), (None, False, 19632, None, "auto")]
    assert [x["amount"] for x in ledger] == [533808] and problems == []
    assert (inv["status"], item["status"], item["resolution"], run["final_decision"]) == ("approved", "resolved", "approved", "review")


def test_1b_the_synthetic_clean_scenario_allocates_both_lines_automatically(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, ITEMISED)
        iid = item_for(c, run_scenario(c, tmp_path, "clean"))
        assert preview(c, iid)["needs_input"] == []
        assert approve(c, iid).status_code == 200
        _, _, cons, _, _, problems = state(c, iid)
        ids = line_ids(c)
    assert [(x["po_line_id"], x["amount"], x["quantity"], x["matched_by"]) for x in cons] == [
        (ids[0], 60000, "10", "auto"), (ids[1], 40000, "5", "auto"), (None, 10500, None, "auto")] and problems == []


def test_2_mixed_confident_and_ambiguous_blocks_until_every_ambiguous_line_has_a_choice(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, LOOKALIKES)
        iid = item_for(c, run_scenario(c, tmp_path, "ambiguous"))
        before = counts(c, *TABLES)
        r = approve(c, iid)
        assert r.status_code == 422 and r.json()["error"] == "allocation_required"
        need = r.json()["needs_input"]
        assert [n["invoice_line_no"] for n in need] == [1] and need[0]["status"] == "ambiguous" and len(need[0]["candidates"]) == 3
        assert "1 line needs a choice" in r.json()["message"]
        assert counts(c, *TABLES) == before                                    # nothing written, nothing defaulted
        item, inv, *_ = state(c, iid)
        assert item["status"] == "open" and inv["status"] == "in_review"
        green = line_ids(c)[1]
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "po_line", "po_line_id": green}])
        assert r.status_code == 200, r.text
        _, _, cons, _, _, problems = state(c, iid)
    by = {(x["po_line_id"], x["matched_by"]) for x in cons}
    assert (green, "manual_reviewer") in by and (line_ids(c)[2], "auto") in by and problems == []


def test_3_an_assignment_over_the_remaining_amount_is_refused_clearly(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, ITEMISED)
        iid = item_for(c, run_scenario(c, tmp_path, "bundled"))               # one 1,000.00 line, no automatic match
        before = counts(c, *TABLES)
        widget_b = line_ids(c)[1]                                              # 400.00 remaining, allowance 8.00
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "po_line", "po_line_id": widget_b}])
        assert r.status_code == 422 and r.json()["error"] == "allocation_invalid"
        p = r.json()["problems"][0]
        assert (p["code"], p["amount"], p["remaining"], p["allowance"]) == ("exceeds_remaining", "1000.00", "400.00", "8.00")
        assert p["message"] == "Invoice line 1 (1000.00) does not fit PO line 2: 400.00 remaining, allowance 8.00."
        assert counts(c, *TABLES) == before


def test_3b_the_boundary_remaining_plus_allowance_is_accepted(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, [("Widget A", "10", "60.00", 60000), ("Widget B", "5", "80.00", 40000), ("Bulk goods", "1", "980.39", 98039)])
        iid = item_for(c, run_scenario(c, tmp_path, "bundled"))               # 1,000.00 vs 980.39 remaining: allowance min(19.60, 50) = 19.60
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "po_line", "po_line_id": line_ids(c)[2]}])
        assert r.status_code == 422 and r.json()["problems"][0]["allowance"] == "19.60"          # 19.61 over: refused
    with api(tmp_path / "b", run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, [("Widget A", "10", "60.00", 60000), ("Widget B", "5", "80.00", 40000), ("Bulk goods", "1", "980.40", 98040)])
        iid = item_for(c, run_scenario(c, tmp_path / "b", "bundled"))          # 19.60 over, allowance 19.60: accepted
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "po_line", "po_line_id": line_ids(c)[2]}])
        assert r.status_code == 200, r.text


def test_4_unassigned_shows_in_the_po_figure_and_reduces_no_line(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, ITEMISED)
        iid = item_for(c, run_scenario(c, tmp_path, "bundled"))
        po_before = c.get("/api/pos/90").json()
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "unassigned"}])
        assert r.status_code == 200, r.text
        po_after = c.get("/api/pos/90").json()
        _, _, cons, _, _, problems = state(c, iid)
    assert [(x["po_line_id"], x["amount"], x["matched_by"], x["invoice_line_id"] is not None) for x in cons] == [
        (None, 100000, "manual_reviewer", True), (None, 10500, "auto", False)] and problems == []
    assert po_before["amounts"]["consumed_without_line"] == "0.00" and po_after["amounts"]["consumed_without_line"] == "1105.00"
    assert [(ln["remaining_amount"], ln["remaining_quantity"]) for ln in po_after["lines"]] == [
        (ln["remaining_amount"], ln["remaining_quantity"]) for ln in po_before["lines"]]
    assert po_after["amounts"]["balance"] == "395.00"                          # 1,500.00 - 1,105.00: the PO total still pays for it


def test_5_reject_writes_no_allocation_at_all(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        before = counts(c, "ledger_entries", "po_consumption")
        r = c.post(f"/api/review-queue/{iid}/reject", json={"confirm": True, "reason": "Not our order."})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        assert counts(c, "ledger_entries", "po_consumption") == before
        item, inv, cons, ledger, run, _ = state(c, iid)
        with db(c) as conn:
            ev = [dict(e) for e in conn.execute("SELECT event_type, detail FROM audit_events WHERE run_id = ? AND stage = 'review' ORDER BY seq",
                                                (item["run_id"],))]
    assert (inv["status"], item["status"], item["resolution"], cons, ledger, run["final_decision"]) == ("rejected", "resolved", "rejected", [], [], "review")
    assert [e["event_type"] for e in ev] == ["human_rejected", "review_resolved"] and json.loads(ev[0]["detail"])["reason"] == "Not our order."


# ------------------------------------------------------------------------------------------ re-verification and validation

def test_a_stale_token_is_409_with_a_fresh_preview_and_writes_nothing(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        token = preview(c, iid)["state_token"]
        with db(c) as conn:                                                    # another approval on the same PO in between
            with conn:
                conn.execute("INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type) VALUES (77, 1, 1, 1000, 'commit')")
                conn.execute("INSERT INTO po_consumption (ledger_entry_id, po_id, invoice_id, amount, type, matched_by) VALUES (77, 1, 1, 1000, 'commit', 'auto')")
        before = counts(c, *TABLES)
        r = approve(c, iid, token=token)
        assert r.status_code == 409 and r.json()["error"] == "stale" and r.json()["preview"]["po"]["balance_before"] == "5990.00"
        assert counts(c, *TABLES) == before
        assert approve(c, iid, token=r.json()["preview"]["state_token"]).status_code == 200


def test_approving_twice_or_after_reject_is_409(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        token = preview(c, iid)["state_token"]
        assert approve(c, iid, token=token).status_code == 200
        r = approve(c, iid, token=token)
        assert r.status_code == 409 and r.json()["error"] == "stale"
        assert c.post(f"/api/review-queue/{iid}/reject", json={"confirm": True}).status_code == 409
    with api(tmp_path / "b", run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        assert c.post(f"/api/review-queue/{iid}/reject", json={"confirm": True}).status_code == 200
        assert approve(c, iid).status_code == 409


@pytest.mark.parametrize("sql, code", [
    ("UPDATE purchase_orders SET status = 'closed' WHERE id = 1", "po_closed"),
    ("UPDATE invoices SET currency = NULL WHERE po_id = 1", "currency_missing"),
    ("UPDATE invoices SET total = NULL WHERE po_id = 1", "total_missing"),
    ("UPDATE invoices SET po_id = NULL WHERE po_id = 1 AND run_id IS NOT NULL", "no_matched_po"),
])
def test_blocked_items_cannot_be_approved(tmp_path, sql, code):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        with db(c) as conn:
            with conn:
                conn.execute(sql)
        before = counts(c, *TABLES)
        r = approve(c, iid)
        assert r.status_code == 409 and r.json()["error"] == "not_approvable" and code in [b["code"] for b in r.json()["blocked_by"]]
        assert counts(c, *TABLES) == before
        assert c.post(f"/api/review-queue/{iid}/reject", json={"confirm": True}).status_code == 200    # reject stays available


def test_a_fault_after_the_ledger_insert_rolls_everything_back(tmp_path, monkeypatch):
    from app.review import actions
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        before = counts(c, *TABLES)
        monkeypatch.setattr(actions, "record_consumption", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected")))
        with pytest.raises(RuntimeError):
            approve(c, iid)
        assert counts(c, *TABLES) == before
        item, inv, *_ = state(c, iid)
        assert item["status"] == "open" and inv["status"] == "in_review"


@pytest.mark.parametrize("allocs, code", [
    ([{"invoice_line_id": "L1", "target": "unassigned"}, {"invoice_line_id": "L1", "target": "unassigned"}], "duplicate_line"),
    ([{"invoice_line_id": "L1", "target": "po_line", "po_line_id": 9999}], "not_a_line_of_this_po"),
    ([{"invoice_line_id": 424242, "target": "unassigned"}], "unknown_invoice_line"),
])
def test_invalid_choices(tmp_path, allocs, code):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, ITEMISED)
        iid = item_for(c, run_scenario(c, tmp_path, "bundled"))
        lid = invoice_line_id(c, iid, 1)
        allocs = [{**a, "invoice_line_id": lid if a["invoice_line_id"] == "L1" else a["invoice_line_id"]} for a in allocs]
        r = approve(c, iid, allocs)
    assert r.status_code == 422 and code in [p["code"] for p in r.json()["problems"]]


def test_an_automatic_line_takes_no_choice(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        lid = invoice_line_id(c, iid, 1)
        r = approve(c, iid, [{"invoice_line_id": lid, "target": "unassigned"}])
    assert r.status_code == 422 and r.json()["problems"][0]["code"] == "automatic_line"


def test_a_po_line_without_an_amount_cannot_be_chosen(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, [("Widget A", "10", "60.00", 60000), ("Services (open-ended)", None, None, None)])
        iid = item_for(c, run_scenario(c, tmp_path, "bundled"))
        r = approve(c, iid, [{"invoice_line_id": invoice_line_id(c, iid, 1), "target": "po_line", "po_line_id": line_ids(c)[1]}])
    assert r.status_code == 422 and r.json()["problems"][0]["code"] == "po_line_has_no_amount"


def test_confirm_is_required_and_unknown_items_are_404(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        r = c.post(f"/api/review-queue/{iid}/approve", json={"state_token": "x"})
        assert r.status_code == 400 and r.json()["error"] == "confirm_required"
        assert c.post(f"/api/review-queue/{iid}/reject", json={}).status_code == 400
        assert c.post("/api/review-queue/999/approve", json={"confirm": True, "state_token": "x"}).status_code == 404
        assert c.post("/api/review-queue/999/reject", json={"confirm": True}).status_code == 404


def test_the_audit_trail_of_an_approve(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        rid = run_and_wait(c, SS_10963)
        iid = item_for(c, rid)
        approve(c, iid, note="Checked against the delivery note.")
        with db(c) as conn:
            ev = [dict(e) for e in conn.execute("SELECT seq, stage, event_type, detail FROM audit_events WHERE run_id = ? ORDER BY seq", (rid,))]
    review = [e for e in ev if e["stage"] == "review"]
    assert [e["event_type"] for e in review] == ["human_approved", "allocation", "review_resolved"]
    assert review[0]["seq"] == max(e["seq"] for e in ev if e["stage"] != "review") + 1                   # the run's trail continues
    d = json.loads(review[0]["detail"])
    assert (d["note"], d["balance_before"], d["balance_after"], d["reviewer"]) == ("Checked against the delivery note.", "6000.00", "661.92",
                                                                                   "reviewer (local UI)")
    assert [r["kind"] for r in json.loads(review[1]["detail"])["rows"]] == ["automatic", "remainder"]


def test_the_list_moves_resolved_items(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        iid = item_for(c, run_and_wait(c, SS_10963))
        approve(c, iid)
        assert c.get("/api/review-queue").json() == {"items": [], "open_count": 0}
        done = c.get("/api/review-queue?status=resolved").json()["items"]
    assert [(i["id"], i["resolution"]) for i in done] == [(iid, "approved")]


def test_no_bulk_endpoint_exists(tmp_path):
    with api(tmp_path) as c:
        assert c.post("/api/review-queue/approve", json={"ids": [1, 2]}).status_code in (404, 405, 422)
        routes = set(c.get("/api/openapi.json").json()["paths"])
    assert {p for p in routes if p.startswith("/api/review-queue")} == {"/api/review-queue", "/api/review-queue/{item_id}",
                                                                        "/api/review-queue/{item_id}/approve", "/api/review-queue/{item_id}/reject"}

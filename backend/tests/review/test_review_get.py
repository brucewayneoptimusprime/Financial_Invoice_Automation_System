"""GET /api/review-queue and /api/review-queue/{id}: the list, the approve preview, blockers, and the reused line-match data."""
from tests.review.helpers import (ITEMISED, LOOKALIKES, SS_10963, add_new_vendor_po, api, db, item_for, run_and_wait, run_scenario,
                                  runs_with_scenarios)


def test_the_list_shows_open_items_oldest_first_with_their_approve_state(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, LOOKALIKES)
        first = run_and_wait(c, SS_10963)
        second = run_scenario(c, tmp_path, "ambiguous")
        body = c.get("/api/review-queue").json()
    items = body["items"]
    assert body["open_count"] == 2 and [i["run_id"] for i in items] == [first, second]
    assert (items[0]["invoice_number"], items[0]["total"], items[0]["po_number"], items[0]["can_approve"], items[0]["lines_needing_input"]) == (
        "10963", "5338.08", "PO-SS-001", True, 0)
    assert items[1]["po_number"] == "PO-5001" and items[1]["lines_needing_input"] == 1 and items[1]["vendor"] == "Northwind Trading Co"


def test_the_detail_of_an_all_confident_item(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        rid = run_and_wait(c, SS_10963)
        d = c.get(f"/api/review-queue/{item_for(c, rid)}").json()
        run_view = c.get(f"/api/runs/{rid}").json()
        again = c.get(f"/api/review-queue/{item_for(c, rid)}").json()
    a = d["approve"]
    assert a["possible"] and a["blocked_by"] == [] and a["needs_input"] == [] and a["warnings"] == []
    assert a["po"] == {"id": 1, "po_number": "PO-SS-001", "status": "open", "currency": "USD", "balance_before": "6000.00", "balance_after": "661.92"}
    assert a["commit_amount"] == "5338.08" and a["tolerance"] == {"pct": 2.0, "abs": "50.00", "mode": "lesser_of"}
    assert [(r["kind"], r["po_line_no"], r["amount"], r["quantity"]) for r in a["rows"]] == [("automatic", 1, "5141.76", "4"),
                                                                                             ("remainder", None, "196.32", None)]
    assert a["remainder"] == {"amount": "196.32", "label": "tax, shipping and other amounts not on a line"}
    assert d["line_matches"] == run_view["line_matches"]                          # reused, not recomputed
    assert d["run"]["decision"] == "review" and d["invoice"]["status"] == "in_review" and a["state_token"] == again["approve"]["state_token"]


def test_the_detail_lists_the_lines_that_need_a_choice(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, LOOKALIKES)
        a = c.get(f"/api/review-queue/{item_for(c, run_scenario(c, tmp_path, 'ambiguous'))}").json()["approve"]
    assert [r["kind"] for r in a["rows"]] == ["automatic", "remainder"]            # line 2 automatic; line 1 waits for the reviewer
    need = a["needs_input"]
    assert len(need) == 1 and (need[0]["invoice_line_no"], need[0]["status"], need[0]["amount"]) == (1, "ambiguous", "600.00")
    assert [c_["po_line_no"] for c_ in need[0]["candidates"]] == [1, 2, 3] and need[0]["suggested"]["target"] == "po_line"
    assert need[0]["candidates"][0]["fits"] and need[0]["candidates"][0]["remaining_amount"] == "600.00"
    assert need[0]["candidates"][2]["fits"] is False and need[0]["other_lines"] == []    # Widget B (400.00) cannot take 600.00


def test_a_bundled_invoice_needs_a_choice_for_its_one_line(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        add_new_vendor_po(c, ITEMISED)
        a = c.get(f"/api/review-queue/{item_for(c, run_scenario(c, tmp_path, 'bundled'))}").json()["approve"]
    need = a["needs_input"][0]
    assert need["status"] == "no_match" and need["candidates"] == [] and need["suggested"] == {"target": "unassigned"}
    assert [o["po_line_no"] for o in need["other_lines"]] == [1, 2, 3]


def test_blockers_are_listed_and_stop_approve(tmp_path):
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        rid = run_and_wait(c, SS_10963)
        iid = item_for(c, rid)
        with db(c) as conn:
            with conn:
                conn.execute("UPDATE purchase_orders SET status = 'closed' WHERE id = 1")
                conn.execute("UPDATE invoices SET currency = 'EUR' WHERE run_id = ?", (rid,))
        a = c.get(f"/api/review-queue/{iid}").json()["approve"]
        assert not a["possible"] and {b["code"] for b in a["blocked_by"]} == {"po_closed", "currency_mismatch"}
        with db(c) as conn:
            with conn:
                conn.execute("UPDATE invoices SET po_id = NULL WHERE run_id = ?", (rid,))
        a = c.get(f"/api/review-queue/{iid}").json()["approve"]
        assert [b["code"] for b in a["blocked_by"]] == ["no_matched_po"] and a["po"] is None
        assert "It can be rejected" in a["blocked_by"][0]["message"]


def test_unknown_and_malformed_ids(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/review-queue/999").status_code == 404
        assert c.get("/api/review-queue/abc").status_code == 422
        assert c.get("/api/review-queue?status=bogus").status_code == 422
        assert c.get("/api/review-queue").json() == {"items": [], "open_count": 0}

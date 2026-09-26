"""The pure allocation planner (review actions + line allocation, stage 1)."""
from decimal import Decimal

import pytest

from app.pipeline.allocation import Choice, InvoiceLineIn, POLineNow, StoredMatch, TolParams, fit, plan_allocation

D = Decimal
TOL = TolParams(pct=2.0, abs=D("50.00"), mode="lesser_of")


def inv(id, no, amount, desc="x", qty="1"):
    return InvoiceLineIn(id, no, desc, D(qty) if qty else None, None, amount)


def pol(id, no, amount, remaining=None, desc="p"):
    return POLineNow(id, no, desc, D("10"), None, amount, amount if remaining is None else remaining, D("10"))


def match(line_id, status, po_line_id=None, cands=()):
    return StoredMatch(line_id, status, po_line_id, None, tuple({"po_line_id": c, "po_line_no": c - 100, "score": 0.8} for c in cands))


PO = [pol(101, 1, 60000), pol(102, 2, 40000), pol(103, 3, 10000)]


def test_all_confident_lines_are_automatic_and_the_rest_is_the_remainder():
    p = plan_allocation([inv(1, 1, 60000), inv(2, 2, 40000)], {1: match(1, "matched", 101), 2: match(2, "matched", 102)}, PO, 110500, {}, TOL)
    assert p.complete and not p.needs_input
    assert [(r.kind, r.po_line_id, r.amount_minor, r.matched_by) for r in p.rows] == [
        ("automatic", 101, 60000, "auto"), ("automatic", 102, 40000, "auto"), ("remainder", None, 10500, "auto")]
    assert sum(r.amount_minor for r in p.rows) == 110500


def test_an_ambiguous_line_needs_input_with_ranked_candidates_and_a_best_guess():
    p = plan_allocation([inv(1, 1, 60000), inv(2, 2, 40000)], {1: match(1, "ambiguous", None, (101, 102)), 2: match(2, "matched", 102)}, PO,
                        110500, {}, TOL)
    assert not p.complete and p.missing == [1]
    need = p.needs_input[0]
    assert need["status"] == "ambiguous" and [c["po_line_id"] for c in need["candidates"]] == [101, 102]
    assert need["suggested"] == {"target": "po_line", "po_line_id": 101} and [o["po_line_id"] for o in need["other_lines"]] == [103]
    assert need["candidates"][0]["fits"] and not need["candidates"][1]["fits"]          # 600.00 does not fit line 2 (400.00 + 8.00)


def test_a_reviewer_may_choose_any_line_of_the_po_regardless_of_description():
    p = plan_allocation([inv(1, 1, 10000)], {1: match(1, "no_match")}, PO, 10000, {1: Choice("po_line", 103)}, TOL)
    assert p.complete and [(r.kind, r.po_line_id, r.matched_by) for r in p.rows] == [("reviewer_line", 103, "manual_reviewer")]


def test_unassigned_is_a_row_against_the_po_total_with_the_invoice_line():
    p = plan_allocation([inv(1, 1, 10000)], {1: match(1, "no_match")}, PO, 10000, {1: Choice("unassigned")}, TOL)
    assert [(r.kind, r.po_line_id, r.invoice_line_id, r.quantity, r.matched_by) for r in p.rows] == [
        ("reviewer_unassigned", None, 1, None, "manual_reviewer")]


def test_an_assignment_over_the_remaining_amount_is_refused_with_its_numbers():
    po = [pol(101, 1, 60000, remaining=25000)]
    p = plan_allocation([inv(1, 1, 60000)], {1: match(1, "ambiguous", None, (101,))}, po, 60000, {1: Choice("po_line", 101)}, TOL)
    assert not p.complete and p.problems[0]["code"] == "exceeds_remaining"
    assert (p.problems[0]["remaining"], p.problems[0]["allowance"]) == ("250.00", "5.00") and "does not fit PO line 1" in p.problems[0]["message"]


def test_the_boundary_remaining_plus_allowance_fits():
    assert fit(25500, 25000, 0, TOL)["fits"] and not fit(25501, 25000, 0, TOL)["fits"]


def test_several_lines_use_up_one_po_line_in_order():
    po = [pol(101, 1, 60000)]
    p = plan_allocation([inv(1, 1, 40000), inv(2, 2, 40000)], {1: match(1, "matched", 101), 2: match(2, "ambiguous", None, (101,))}, po,
                        80000, {2: Choice("po_line", 101)}, TOL)
    assert p.problems and p.problems[0]["remaining"] == "200.00"                # 600 - the 400 the first line took


def test_a_matched_line_that_no_longer_fits_goes_to_the_reviewer():
    po = [pol(101, 1, 60000, remaining=1000)]
    p = plan_allocation([inv(1, 1, 60000)], {1: match(1, "matched", 101, (101,))}, po, 60000, {}, TOL)
    assert p.missing == [1] and p.needs_input[0]["status"] == "no_longer_fits" and p.needs_input[0]["suggested"] == {"target": "unassigned"}


def test_an_automatic_line_takes_no_choice():
    p = plan_allocation([inv(1, 1, 60000)], {1: match(1, "matched", 101)}, PO, 60000, {1: Choice("unassigned")}, TOL)
    assert p.problems[0]["code"] == "automatic_line"


@pytest.mark.parametrize("choice, code", [(Choice("po_line", 999), "not_a_line_of_this_po")])
def test_invalid_targets(choice, code):
    p = plan_allocation([inv(1, 1, 100)], {1: match(1, "no_match")}, PO, 100, {1: choice}, TOL)
    assert p.problems[0]["code"] == code


def test_a_po_line_without_an_amount_cannot_be_chosen():
    po = [POLineNow(101, 1, "p", None, None, None, None, None), pol(102, 2, 40000)]
    p = plan_allocation([inv(1, 1, 100)], {1: match(1, "no_match")}, po, 100, {1: Choice("po_line", 101)}, TOL)
    assert p.problems[0]["code"] == "po_line_has_no_amount"


def test_unknown_invoice_line():
    p = plan_allocation([inv(1, 1, 100)], {1: match(1, "matched", 101)}, PO, 100, {7: Choice("unassigned")}, TOL)
    assert p.problems[-1]["code"] == "unknown_invoice_line"


def test_lines_without_amounts_are_not_allocated():
    p = plan_allocation([inv(1, 1, None), inv(2, 2, 0)], {}, PO, 5000, {}, TOL)
    assert p.complete and [(r.kind, r.amount_minor) for r in p.rows] == [("remainder", 5000)] and len(p.notes) == 2


def test_a_po_without_assignable_lines_puts_everything_on_the_total_without_asking():
    p = plan_allocation([inv(1, 1, 60000)], {1: match(1, "not_evaluable")}, [], 60000, {}, TOL)
    assert p.complete and not p.needs_input and [(r.kind, r.amount_minor) for r in p.rows] == [("remainder", 60000)]


def test_a_negative_remainder_scales_the_lines_down_pro_rata():
    p = plan_allocation([inv(1, 1, 60000), inv(2, 2, 40000)], {1: match(1, "matched", 101), 2: match(2, "matched", 102)}, PO, 90001, {}, TOL)
    assert p.pro_rata and sum(r.amount_minor for r in p.rows) == 90001 and [r.kind for r in p.rows] == ["automatic", "automatic"]
    assert [r.amount_minor for r in p.rows] == [54001, 36000]                   # 54000.6 -> 54000 (+1 leftover on the largest), 36000.4 -> 36000


def test_greater_of_from_the_rule_params():
    greater = TolParams(2.0, D("50.00"), "greater_of")
    assert not fit(26000, 25000, 0, TOL)["fits"]                 # lesser_of: allowance min(5.00, 50.00) = 5.00
    assert fit(26000, 25000, 0, greater)["fits"]                  # greater_of: allowance max(5.00, 50.00) = 50.00
    assert fit(30000, 25000, 0, greater)["fits"] and not fit(30001, 25000, 0, greater)["fits"]


def test_nothing_remaining_leaves_no_allowance():
    assert not fit(1, 0, 0, TOL)["fits"] and not fit(1, 100, 100, TOL)["fits"]

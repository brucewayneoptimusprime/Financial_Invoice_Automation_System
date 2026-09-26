"""r_po_line_price (line-item consumption stage 3): invoice line unit price vs the matched PO line's, with a tolerance."""
from decimal import Decimal

import pytest

from app.builtin_rules import builtin_rules
from app.config import get_settings
from app.engine.engine import run_validate_stage
from app.engine.line_matching import match_lines
from app.enums import Outcome
from app.models.rules import Rule
from app.engine.facts import POLineFact
from tests.factories import make_ctx, make_extracted, make_facts, make_po

D = Decimal
RULE = next(r for r in builtin_rules(get_settings()) if r.id == "r_po_line_price")


def lines(*specs):
    return [{"description": d, "quantity": q, "unit_price": p, "amount": a, "confidence": 0.9} for d, q, p, a in specs]


def run(invoice_lines, po_lines=None, **params):
    po = make_po(total="100000.00", lines=po_lines) if po_lines is not None else make_po(total="100000.00")
    ctx = make_ctx(extracted=make_extracted(line_items=invoice_lines), facts=make_facts(pos=[po]))
    ctx.line_matches = match_lines(ctx.extracted, ctx.facts.po_by_id(po.id))
    rule = RULE.model_copy(update={"params": {**RULE.params, **params}}) if params else RULE
    run_validate_stage(ctx, [rule])
    return next(r for r in ctx.rule_results if r.rule_id == "r_po_line_price"), ctx


def test_the_seeded_rule():
    assert (RULE.type, RULE.severity_on_trigger, RULE.params["pct"], RULE.params["abs"], RULE.params["mode"], RULE.params["direction"]) == (
        "po_line_unit_price", 1, 1.0, "1.00", "lesser_of", "above")


def test_equal_prices_pass():
    r, _ = run(lines(("Standard widget", 10, "60.00", "600.00"), ("Premium gadget", 5, "80.00", "400.00")))
    assert (r.outcome, r.outcome_key) == (Outcome.PASS, "within_tolerance") and len(r.detail["lines"]) == 2


def test_a_price_above_the_po_line_is_flagged_with_its_numbers():
    r, _ = run(lines(("Standard widget", 10, "66.00", "660.00"), ("Premium gadget", 5, "80.00", "400.00")))
    assert (r.outcome, r.outcome_key, r.severity) == (Outcome.FLAG, "price_above_po", 1)
    c = r.detail["lines"][0]
    assert (c["invoice_unit_price"], c["po_unit_price"], c["difference"], c["allowance"]) == ("66.00", "60.00", "6.00", "0.600")
    assert "Invoice line 1 is priced 66 per unit, above PO PO-A-1 line 1 at 60 by 6" in r.message


def test_within_the_allowance_passes():
    r, _ = run(lines(("Standard widget", 10, "60.50", "605.00")))                     # 0.50 <= min(0.60, 1.00)
    assert r.outcome_key == "within_tolerance"


def test_greater_of_uses_the_larger_allowance():
    lo, _ = run(lines(("Standard widget", 10, "60.90", "609.00")))                    # 0.90 > 0.60 (lesser_of)
    hi, _ = run(lines(("Standard widget", 10, "60.90", "609.00")), mode="greater_of")  # 0.90 <= 1.00
    assert (lo.outcome_key, hi.outcome_key) == ("price_above_po", "within_tolerance")


def test_a_lower_price_passes_with_direction_above_and_flags_with_both():
    above, _ = run(lines(("Standard widget", 10, "50.00", "500.00")))
    both, _ = run(lines(("Standard widget", 10, "50.00", "500.00")), direction="both")
    assert above.outcome_key == "within_tolerance" and "below the PO" in above.message and above.detail["lines"][0]["below_po"]
    assert (both.outcome, both.outcome_key) == (Outcome.FLAG, "price_below_po")


def test_prices_with_more_than_two_decimals():
    po_lines = (POLineFact(line_no=1, id=1, description="Resistor 10k pack", quantity=D(1000), unit_price=D("0.0125"), amount=D("12.50")),)
    ok, _ = run(lines(("Resistor 10k pack", 1000, "0.01251", "12.51")), po_lines)        # allowance 1% of 0.0125 = 0.000125
    bad, _ = run(lines(("Resistor 10k pack", 1000, "0.0130", "13.00")), po_lines)
    assert ok.outcome_key == "within_tolerance" and bad.outcome_key == "price_above_po"


@pytest.mark.parametrize("invoice_lines, po_lines, reason", [
    (lines(("Goods as per order", 1, "1000.00", "1000.00")), None, "no_compared_lines"),                  # bundled: no match
    (lines(("Standard widget", 10, None, "600.00")), None, "no_compared_lines"),                           # price missing
    (lines(("Widget A", 10, "60.00", "600.00")),
     (POLineFact(line_no=1, description="Widget A (blue)", quantity=D(10), unit_price=D("60"), amount=D("600")),
      POLineFact(line_no=2, description="Widget A (green)", quantity=D(10), unit_price=D("60"), amount=D("600"))), "no_compared_lines"),
])
def test_not_evaluable_is_never_a_pass(invoice_lines, po_lines, reason):
    r, _ = run(invoice_lines, po_lines)
    assert (r.outcome, r.outcome_key, r.detail["reason"]) == (Outcome.INFO, "not_evaluable", reason) and r.detail["skipped"]


def test_no_matched_po_is_not_evaluable():
    ctx = make_ctx(matched=False)
    ctx.line_matches = match_lines(ctx.extracted, None)
    run_validate_stage(ctx, [RULE])
    r = next(r for r in ctx.rule_results if r.rule_id == "r_po_line_price")
    assert (r.outcome, r.detail["reason"]) == (Outcome.INFO, "no_line_matching")


def test_only_confident_lines_are_compared():
    r, ctx = run(lines(("Standard widget", 10, "66.00", "660.00"), ("Freight", 1, "99.00", "99.00")))
    assert [c["invoice_line_no"] for c in r.detail["lines"]] == [1] and r.detail["skipped"][0]["invoice_line_no"] == 2


def test_bad_params_fail_safe_to_a_flag_at_the_default_severity():
    bad = Rule(**{**RULE.model_dump(), "params": {**RULE.params, "direction": "sideways"}})
    ctx = make_ctx()
    run_validate_stage(ctx, [bad])
    res = next(x for x in ctx.rule_results if x.rule_id == "r_po_line_price")
    assert res.outcome is Outcome.FLAG and res.severity == 1

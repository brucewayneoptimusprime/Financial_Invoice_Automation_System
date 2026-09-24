from decimal import Decimal

import pytest

from app.enums import Outcome
from tests.engine.real import ev, ev_builtin
from tests.factories import field, make_ctx, make_extracted

D = Decimal


def line(desc="x", qty=1, price="10.00", amount="10.00"):
    return {"description": desc, "quantity": qty, "unit_price": price, "amount": amount, "confidence": 0.9}


def run(**over):
    return ev_builtin("r_arithmetic", make_ctx(extracted=make_extracted(**over)))


def checks(r, name):
    return [c for c in r.detail["checks"] if c["check"] == name]


def test_consistent_invoice_passes_and_records_every_number():
    r = ev_builtin("r_arithmetic", make_ctx())
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.PASS, 0, "consistent")
    assert len(r.detail["checks"]) == 4 and r.detail["skipped"] == []       # 2 lines + lines-vs-subtotal + subtotal+tax
    c = checks(r, "subtotal_plus_tax_equals_total")[0]
    assert (c["expected"], c["actual"], c["difference"], c["ok"]) == ("1100.00", "1100.00", "0.00", True)


def test_line_math_mismatch_is_flagged_with_the_line_number():
    r = run(line_items=[line(qty=3, price="10.00", amount="35.00")], subtotal="35.00", tax="0.00", total="35.00")
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "mismatch")
    bad = checks(r, "line_math")[0]
    assert bad["line"] == 1 and bad["expected"] == "30.00" and bad["actual"] == "35.00" and not bad["ok"]
    assert "line 1" in r.message and r.detail["failed"] == ["line_math"]


def test_lines_not_summing_to_subtotal_is_flagged():
    r = run(subtotal="1200.00", tax="100.00", total="1300.00")
    assert r.outcome_key == "mismatch" and r.detail["failed"] == ["lines_vs_subtotal"]
    c = checks(r, "lines_vs_subtotal")[0]
    assert (c["expected"], c["actual"], c["difference"]) == ("1000.00", "1200.00", "200.00")


def test_subtotal_plus_tax_not_equal_total_is_flagged():
    r = run(total="1105.00")
    assert r.detail["failed"] == ["subtotal_plus_tax_equals_total"] and "5.00" in r.message


def test_rounding_allowance_boundary_line_math():
    # 3 x 33.33 = 99.99; amount 100.00 -> diff 0.01 == allowance -> ok; 100.02 -> diff 0.03 -> flagged
    ok_ = run(line_items=[line(qty=3, price="33.33", amount="100.00")], subtotal="100.00", tax="0.00", total="100.00")
    assert ok_.outcome is Outcome.PASS and checks(ok_, "line_math")[0]["difference"] == "0.01"
    bad = run(line_items=[line(qty=3, price="33.33", amount="100.02")], subtotal="100.02", tax="0.00", total="100.02")
    assert bad.outcome is Outcome.FLAG


def test_rounding_allowance_scales_with_the_number_of_summed_lines():
    lines = [line(amount="10.00") for _ in range(10)]                     # 10 lines -> allowance 0.10 on the sum
    within = run(line_items=lines, subtotal="100.10", tax="0.00", total="100.10")
    beyond = run(line_items=lines, subtotal="100.11", tax="0.00", total="100.11")
    assert within.outcome is Outcome.PASS and beyond.outcome is Outcome.FLAG
    assert checks(beyond, "lines_vs_subtotal")[0]["allowance"] == "0.10"


def test_rounding_allowance_is_configurable():
    lines = [line(qty=3, price="33.33", amount="100.00")]
    kw = dict(line_items=lines, subtotal="100.00", tax="0.00", total="100.00")
    assert ev_builtin("r_arithmetic", make_ctx(extracted=make_extracted(**kw)), rounding_per_term=0.0).outcome is Outcome.FLAG
    assert ev_builtin("r_arithmetic", make_ctx(extracted=make_extracted(**kw)), rounding_per_term=0.05).outcome is Outcome.PASS
    assert ev("arithmetic_consistency", make_ctx(), {"rounding_per_term": -1}).outcome_key == "invalid_params"


def test_tax_included_in_total_compares_subtotal_to_total():
    inc = field("100.00", included_in_total=True)
    ok_ = run(tax=inc, subtotal="1100.00", total="1100.00", line_items=[line(amount="1100.00", price="1100.00")])
    assert ok_.outcome is Outcome.PASS and checks(ok_, "total_equals_subtotal_tax_included")
    bad = run(tax=inc, subtotal="1000.00", total="1100.00", line_items=[line(amount="1000.00", price="1000.00")])
    assert bad.detail["failed"] == ["total_equals_subtotal_tax_included"]


def test_unknown_tax_inclusion_flag_is_treated_as_excluded_and_recorded():
    r = run(tax=field("100.00"))                                            # included_in_total not stated
    assert checks(r, "subtotal_plus_tax_equals_total")[0]["tax_flag_assumed"] is True


@pytest.mark.parametrize("over,skipped_check,reason", [
    (dict(tax=None), "subtotal_tax_total", "missing:tax"),
    (dict(tax=field(None, 0.99)), "subtotal_tax_total", "missing:tax"),      # null is missing whatever the confidence
    (dict(subtotal=field(None, 0.99)), "lines_vs_subtotal", "missing:subtotal"),
    (dict(total=field(None, 0.99)), "subtotal_tax_total", "missing:total"),
])
def test_null_inputs_skip_only_the_checks_that_need_them(over, skipped_check, reason):
    r = run(**over)
    assert r.outcome is Outcome.PASS                                        # the checks that could run all agree
    assert {"check": skipped_check, "reason": reason} in r.detail["skipped"]


def test_null_line_values_skip_that_line_and_the_lines_sum():
    lines = [line(), {"description": "y", "quantity": None, "unit_price": None, "amount": None, "confidence": 0.9}]
    r = run(line_items=lines, subtotal="10.00", tax="0.00", total="10.00")
    assert r.outcome is Outcome.PASS
    reasons = {(s["check"], s["reason"]) for s in r.detail["skipped"]}
    assert ("lines_vs_subtotal", "missing:line_amount") in reasons and any(c == "line_math" for c, _ in reasons)


def test_nothing_checkable_is_not_evaluable_not_a_pass():
    r = run(line_items=[], subtotal=None, tax=None, total=None)
    assert r.outcome is Outcome.INFO and r.severity == 0 and r.detail["reason"] == "no_checkable_amounts"


def test_no_extraction_is_not_evaluable():
    assert ev_builtin("r_arithmetic", make_ctx(extracted=None)).outcome is Outcome.INFO


def test_exact_decimal_arithmetic_no_float_drift():
    lines = [line(price="0.10", amount="0.10") for _ in range(3)]
    r = run(line_items=lines, subtotal="0.30", tax="0.00", total="0.30")
    assert r.outcome is Outcome.PASS and checks(r, "lines_vs_subtotal")[0]["difference"] == "0.00"


def test_severity_default_and_override():
    bad = dict(total="1105.00")
    assert run(**bad).severity == 1
    assert ev("arithmetic_consistency", make_ctx(extracted=make_extracted(**bad)), {}, severity=2).severity == 2
    r = ev("arithmetic_consistency", make_ctx(extracted=make_extracted(**bad)), {"severity_by_outcome": {"mismatch": 3}})
    assert r.severity == 3

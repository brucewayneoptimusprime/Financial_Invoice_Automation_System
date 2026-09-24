"""amount_tolerance evaluator: outcomes, keys, messages and the numbers recorded. The exhaustive
semantics tests (modes, boundaries, ledger + status scenarios) live in test_tolerance_semantics.py."""
import pytest

from app.enums import Outcome
from tests.engine.real import ev, ev_builtin
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po


def ctx_for(total="1100.00", balance_total="1200.00", committed="0.00", **extracted):
    return make_ctx(extracted=make_extracted(total=total, **extracted),
                    facts=make_facts(pos=[make_po(total=balance_total, net_committed=committed)]))


def test_invoice_within_the_remaining_balance_passes_with_the_numbers():
    r = ev_builtin("r_tolerance_pct", ctx_for())
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.PASS, 0, "within_balance")
    assert r.detail["invoice_amount"] == "1100.00" and r.detail["balance"] == "1200.00" and r.detail["excess"] == "-100.00"
    assert "1,100.00 USD" in r.message and "1,200.00 USD" in r.message


def test_balance_is_derived_from_the_ledger_not_the_po_total():
    r = ev_builtin("r_tolerance_pct", ctx_for(total="500.00", balance_total="1000.00", committed="600.00"))
    assert r.detail["balance"] == "400.00" and r.outcome_key == "over_tolerance"


def test_over_balance_within_tolerance_passes_and_says_so():
    # balance 1000, invoice 1015: excess 15; allowance = min(2% of 1000 = 20, 50) = 20 -> within tolerance
    r = ev_builtin("r_tolerance_pct", ctx_for(total="1015.00", balance_total="1000.00", subtotal="1015.00", tax="0.00"))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.PASS, 0, "within_tolerance")
    assert r.detail["excess"] == "15.00" and r.detail["allowance"] == "20.00" and r.detail["within_tolerance"] is True
    assert "within tolerance" in r.message


def test_over_tolerance_is_flagged_for_review():
    r = ev_builtin("r_tolerance_pct", ctx_for(total="1021.00", balance_total="1000.00"))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "over_tolerance")
    assert r.detail["excess"] == "21.00" and r.detail["allowance"] == "20.00" and "beyond tolerance" in r.message


def test_message_states_how_the_allowance_was_computed():
    r = ev_builtin("r_tolerance_pct", ctx_for(total="1100.00", balance_total="1000.00"))
    assert "allowance 20.00 USD = the lesser of 2.0% of balance (20.00 USD) and the fixed limit (50.00 USD)" in r.message
    g = ev_builtin("r_tolerance_pct", ctx_for(total="1100.00", balance_total="1000.00"), mode="greater_of")
    assert "allowance 50.00 USD = the greater of" in g.message


def test_compare_field_can_be_the_subtotal():
    ctx = ctx_for(total="1100.00", balance_total="1000.00", subtotal="1000.00")
    assert ev_builtin("r_tolerance_pct", ctx).outcome_key == "over_tolerance"
    r = ev_builtin("r_tolerance_pct", ctx, compare_field="subtotal")
    assert r.outcome_key == "within_balance" and r.detail["compare_field"] == "subtotal"


@pytest.mark.parametrize("total", ["0.00", "-10.00"])
def test_non_positive_total_is_flagged_not_silently_passed(total):
    r = ev_builtin("r_tolerance_pct", ctx_for(total=total))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "non_positive_total")


def test_sub_cent_total_is_flagged_as_invalid():
    r = ev_builtin("r_tolerance_pct", ctx_for(total="10.005"))
    assert (r.outcome, r.outcome_key) == (Outcome.FLAG, "invalid_amount")


@pytest.mark.parametrize("total", [None, field(None, 0.99)])
def test_null_total_is_missing_whatever_the_confidence(total):
    r = ev_builtin("r_tolerance_pct", make_ctx(extracted=make_extracted(total=total)))
    assert r.outcome is Outcome.INFO and r.severity == 0 and r.detail["reason"] == "missing:total"


def test_not_evaluable_cases_are_info_never_pass():
    assert ev_builtin("r_tolerance_pct", make_ctx(matched=False)).outcome is Outcome.INFO
    assert ev_builtin("r_tolerance_pct", make_ctx(extracted=make_extracted(currency=None))).detail["reason"] == "missing:currency"
    r = ev_builtin("r_tolerance_pct", make_ctx(extracted=make_extracted(currency="EUR")))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "currency_mismatch"


def test_param_validation():
    ctx = ctx_for()
    for bad in ({"pct": -1}, {"abs": -5}, {"abs": 10.005}, {"mode": "either"}, {"compare_field": "tax"}, {"extra": 1}):
        assert ev("amount_tolerance", ctx, bad).outcome_key == "invalid_params", bad


def test_severity_override_for_over_tolerance():
    ctx = ctx_for(total="2000.00", balance_total="1000.00")
    r = ev("amount_tolerance", ctx, {"severity_by_outcome": {"over_tolerance": 2}})
    assert r.outcome is Outcome.FLAG and r.severity == 2

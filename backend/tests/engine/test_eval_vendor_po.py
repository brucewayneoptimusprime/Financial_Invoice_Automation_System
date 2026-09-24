import pytest

from app.enums import MatchStatus, Outcome, POStatus, VendorStatus
from app.models import POCandidate
from app.models.run import VendorMatch
from tests.engine.real import ev, ev_builtin
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po, make_vendor


def cand(number="PO-A-1", score=0.8, reasons=(), po_id=1):
    return POCandidate(po_id=po_id, po_number=number, score=score, reasons=list(reasons))


def facts_with_status(status):
    return make_facts(vendors=[make_vendor(status=status)])


# ------------------------------------------------------------------------------ vendor_status

def test_approved_vendor_passes():
    r = ev_builtin("r_vendor_status", make_ctx())
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.PASS, 0, "approved") and r.rule_id == "r_vendor_status"


def test_new_vendor_is_flagged_for_review():
    r = ev_builtin("r_vendor_status", make_ctx(facts=facts_with_status(VendorStatus.NEW)))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "new")
    assert r.detail["vendor_status"] == "new" and "new" in r.message


def test_blocked_vendor_fails_with_severity_3():
    r = ev_builtin("r_vendor_status", make_ctx(facts=facts_with_status(VendorStatus.BLOCKED)))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FAIL, 3, "blocked") and "BLOCKED" in r.message


def test_unresolved_vendor_is_flagged_unknown():
    ctx = make_ctx()
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    r = ev_builtin("r_vendor_status", ctx)
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "unknown")


def test_never_resolved_vendor_escalates_rather_than_passing():
    r = ev_builtin("r_vendor_status", make_ctx(vendor_id=None))
    assert r.outcome is Outcome.FLAG and r.outcome_key == "unknown"


def test_ambiguous_vendor_is_flagged():
    ctx = make_ctx()
    ctx.matched_vendor = VendorMatch(vendor_id=1, score=0.9, method="fuzzy", ambiguous=True, runner_up_score=0.88)
    r = ev_builtin("r_vendor_status", ctx)
    assert (r.outcome, r.outcome_key) == (Outcome.FLAG, "ambiguous") and "0.88" in r.message


@pytest.mark.parametrize("name", [None, field(None, 0.99), "  "])
def test_vendor_status_null_name_is_not_evaluable_whatever_the_confidence(name):
    r = ev_builtin("r_vendor_status", make_ctx(extracted=make_extracted(vendor_name=name)))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "missing:vendor_name"


def test_vendor_status_overrides_are_applied_and_bounded():
    ctx = make_ctx(facts=facts_with_status(VendorStatus.NEW))
    assert ev("vendor_status", ctx, {"severity_by_outcome": {"new": 2}}).severity == 2
    assert ev("vendor_status", ctx, {"severity_by_outcome": {"new": 3}}, severity=1).severity == 3
    assert ev("vendor_status", ctx, {}, severity=2).severity == 2                       # falls back to the default
    bad = ev("vendor_status", ctx, {"severity_by_outcome": {"new": 0}})
    assert bad.outcome_key == "invalid_params" and bad.severity == 1                     # 0 refused -> escalates


# ------------------------------------------------------------------------------ po_found

def test_po_found_passes_when_matched():
    r = ev_builtin("r_po_found", make_ctx())
    assert (r.outcome, r.outcome_key) == (Outcome.PASS, "found") and "PO-A-1" in r.message and "explicit reference" in r.message


def test_po_found_passes_for_an_ambiguous_match_because_ambiguity_has_its_own_rule():
    r = ev_builtin("r_po_found", make_ctx(match_status=MatchStatus.AMBIGUOUS))
    assert r.outcome is Outcome.PASS


def test_matched_without_a_reference_says_so():
    r = ev_builtin("r_po_found", make_ctx(extracted=make_extracted(po_reference=None)))
    assert r.outcome is Outcome.PASS and "other signals" in r.message


@pytest.mark.parametrize("ref", [None, field(None, 0.99), " "])
def test_no_reference_and_no_match_asks_the_vendor(ref):
    ctx = make_ctx(matched=False, extracted=make_extracted(po_reference=ref))
    r = ev_builtin("r_po_found", ctx)
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 2, "no_reference")


def test_reference_that_matches_nothing_is_flagged_for_review():
    r = ev_builtin("r_po_found", make_ctx(matched=False, extracted=make_extracted(po_reference="PO-ZZZ-9")))
    assert (r.severity, r.outcome_key) == (1, "reference_not_found") and "PO-ZZZ-9" in r.message


def test_low_score_candidate_that_resembles_the_reference():
    ctx = make_ctx(matched=False, match_status=MatchStatus.LOW_SCORE)
    ctx.candidates = [cand(score=0.3, reasons=["reference:fuzzy"])]
    r = ev_builtin("r_po_found", ctx)
    assert (r.severity, r.outcome_key) == (1, "no_confident_match")
    assert r.detail["candidates"][0]["po_number"] == "PO-A-1"


def test_low_score_without_any_reference_hit_is_reference_not_found():
    ctx = make_ctx(matched=False, match_status=MatchStatus.LOW_SCORE)
    ctx.candidates = [cand(score=0.3, reasons=["reference:none", "vendor:match"])]
    assert ev_builtin("r_po_found", ctx).outcome_key == "reference_not_found"


def test_po_found_not_evaluable_before_matching():
    assert ev_builtin("r_po_found", make_ctx(match_status=None)).outcome is Outcome.INFO


# ------------------------------------------------------------------------------ po_ambiguity

def test_ambiguity_flags_with_both_candidates_and_the_gap():
    ctx = make_ctx(matched=False, match_status=MatchStatus.AMBIGUOUS)
    ctx.candidates = [cand("PO-A-1", 0.71, po_id=1), cand("PO-A-2", 0.66, po_id=2)]
    r = ev_builtin("r_po_ambiguity", ctx)
    assert (r.outcome, r.severity) == (Outcome.FLAG, 1)
    assert "PO-A-1" in r.message and "PO-A-2" in r.message and r.detail["score_gap"] == pytest.approx(0.05)


def test_unambiguous_match_passes_and_unmatched_run_is_not_evaluable_wrongly():
    assert ev_builtin("r_po_ambiguity", make_ctx()).outcome is Outcome.PASS
    assert ev_builtin("r_po_ambiguity", make_ctx(match_status=None)).outcome is Outcome.INFO


# ------------------------------------------------------------------------------ vendor_po_mismatch

def test_vendor_matches_po_vendor():
    assert ev_builtin("r_vendor_po_mismatch", make_ctx()).outcome is Outcome.PASS


def test_vendor_differs_from_po_vendor_is_flagged_with_both_names():
    facts = make_facts(vendors=[make_vendor(1, "Vendor Alpha Ltd"), make_vendor(2, "Vendor Beta Inc")],
                       pos=[make_po(vendor_id=2)])
    r = ev_builtin("r_vendor_po_mismatch", make_ctx(facts=facts))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "mismatch")
    assert "Vendor Beta Inc" in r.message and r.detail["po_vendor_id"] == 2


def test_unresolved_or_ambiguous_vendor_cannot_be_confirmed_against_the_po():
    ctx = make_ctx()
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    assert ev_builtin("r_vendor_po_mismatch", ctx).outcome_key == "vendor_unresolved"
    ctx.matched_vendor = VendorMatch(vendor_id=1, score=0.9, method="fuzzy", ambiguous=True)
    assert ev_builtin("r_vendor_po_mismatch", ctx).outcome_key == "vendor_unresolved"


def test_vendor_po_mismatch_not_evaluable_without_po_or_name():
    assert ev_builtin("r_vendor_po_mismatch", make_ctx(matched=False)).outcome is Outcome.INFO
    r = ev_builtin("r_vendor_po_mismatch", make_ctx(extracted=make_extracted(vendor_name=field(None, 0.99))))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "missing:vendor_name"


# ------------------------------------------------------------------------------ currency_mismatch

def test_currency_match_passes():
    assert ev_builtin("r_currency_mismatch", make_ctx()).outcome is Outcome.PASS


def test_currency_mismatch_is_flagged_and_explains_no_fx():
    r = ev_builtin("r_currency_mismatch", make_ctx(extracted=make_extracted(currency="EUR")))
    assert (r.outcome, r.severity) == (Outcome.FLAG, 1)
    assert r.detail["invoice_currency"] == "EUR" and r.detail["po_currency"] == "USD" and "FX" in r.message


@pytest.mark.parametrize("cur", [None, field(None, 0.99)])
def test_currency_null_is_missing_whatever_the_confidence(cur):
    r = ev_builtin("r_currency_mismatch", make_ctx(extracted=make_extracted(currency=cur)))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "missing:currency"


def test_currency_not_evaluable_without_a_po():
    assert ev_builtin("r_currency_mismatch", make_ctx(matched=False)).outcome is Outcome.INFO


# ------------------------------------------------------------------------------ po_status

def test_open_po_with_balance_passes():
    r = ev_builtin("r_po_status", make_ctx())
    assert r.outcome is Outcome.PASS and r.detail["derived_status"] == "open"


def test_partially_billed_po_with_balance_passes():
    ctx = make_ctx(facts=make_facts(pos=[make_po(net_committed="400.00", status=POStatus.PARTIALLY_BILLED)]))
    r = ev_builtin("r_po_status", ctx)
    assert r.outcome is Outcome.PASS and r.detail["derived_status"] == "partially_billed" and "800.00" in r.message


def test_stored_fully_billed_status_is_flagged():
    r = ev_builtin("r_po_status", make_ctx(facts=make_facts(pos=[make_po(status=POStatus.FULLY_BILLED, net_committed="0")])))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "fully_billed")


def test_derived_zero_or_negative_balance_is_fully_billed_even_if_stored_status_lags():
    for committed in ("1200.00", "1250.00"):
        ctx = make_ctx(facts=make_facts(pos=[make_po(net_committed=committed, status=POStatus.PARTIALLY_BILLED)]))
        r = ev_builtin("r_po_status", ctx)
        assert r.outcome_key == "fully_billed" and r.detail["derived_status"] == "fully_billed"


def test_closed_po_is_severity_3():
    r = ev_builtin("r_po_status", make_ctx(facts=make_facts(pos=[make_po(status=POStatus.CLOSED)])))
    assert (r.severity, r.outcome_key) == (3, "closed")


def test_po_status_not_evaluable_without_a_po():
    assert ev_builtin("r_po_status", make_ctx(matched=False)).outcome is Outcome.INFO

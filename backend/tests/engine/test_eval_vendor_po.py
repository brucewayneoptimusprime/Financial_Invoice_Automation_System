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
    r = ev_builtin("r_vendor_status", make_ctx(extracted=make_extracted(vendor_name=name), vendor_id=None))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "missing:vendor_name"


def test_vendor_status_still_runs_without_a_name_when_the_tax_id_resolved_a_vendor():
    ctx = make_ctx(extracted=make_extracted(vendor_name=None))                    # vendor 1 was resolved (by tax id)
    r = ev_builtin("r_vendor_status", ctx)
    assert r.outcome is Outcome.PASS and r.outcome_key == "approved"


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


@pytest.mark.parametrize("ref", [None, field(None, 0.99), " "])
def test_matched_without_a_reference_is_a_suggestion_for_a_person_to_confirm(ref):
    r = ev_builtin("r_po_found", make_ctx(extracted=make_extracted(po_reference=ref)))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "matched_without_reference")
    assert "PO-A-1" in r.message and "confirm" in r.message and r.detail["po_reference"] in (None, " ")


def test_the_three_po_reference_cases_side_by_side():
    stated_but_unmatched = ev_builtin("r_po_found", make_ctx(matched=False, extracted=make_extracted(po_reference="PO-ZZZ-9")))
    no_reference_confident_match = ev_builtin("r_po_found", make_ctx(extracted=make_extracted(po_reference=None)))
    no_reference_no_match = ev_builtin("r_po_found", make_ctx(matched=False, extracted=make_extracted(po_reference=None)))
    assert (stated_but_unmatched.outcome_key, stated_but_unmatched.severity) == ("reference_not_found", 2)          # request_info
    assert (no_reference_confident_match.outcome_key, no_reference_confident_match.severity) == ("matched_without_reference", 1)  # review
    assert (no_reference_no_match.outcome_key, no_reference_no_match.severity) == ("no_reference", 2)               # request_info


def test_an_ambiguous_match_without_a_reference_is_left_to_the_ambiguity_rule():
    r = ev_builtin("r_po_found", make_ctx(match_status=MatchStatus.AMBIGUOUS, extracted=make_extracted(po_reference=None)))
    assert r.outcome is Outcome.PASS


@pytest.mark.parametrize("ref", [None, field(None, 0.99), " "])
def test_no_reference_and_no_match_asks_the_vendor(ref):
    ctx = make_ctx(matched=False, extracted=make_extracted(po_reference=ref))
    r = ev_builtin("r_po_found", ctx)
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 2, "no_reference")


def test_a_stated_reference_that_matches_nothing_asks_the_vendor():
    r = ev_builtin("r_po_found", make_ctx(matched=False, extracted=make_extracted(po_reference="PO-ZZZ-9")))
    assert (r.severity, r.outcome_key) == (2, "reference_not_found") and "PO-ZZZ-9" in r.message


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


# ------------------------------------------------------------------------------ ambiguity involving a blocked vendor

def _ambiguous_ctx(*vendors, tied=None):
    ctx = make_ctx(facts=make_facts(vendors=list(vendors)))
    ids = tied if tied is not None else [v.id for v in vendors]
    ctx.matched_vendor = VendorMatch(vendor_id=ids[0], score=0.93, method="fuzzy", ambiguous=True, runner_up_score=0.91,
                                     candidate_vendor_ids=ids)
    return ctx


def test_ambiguity_with_a_blocked_candidate_stays_severity_1_but_names_it_and_sets_the_flag():
    ctx = _ambiguous_ctx(make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co", status=VendorStatus.BLOCKED))
    r = ev_builtin("r_vendor_status", ctx)
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "ambiguous")           # a human decides: NOT 3
    assert r.detail["blocked_candidate"] is True
    assert "BLOCKED vendor 'Acme Tradings Co'" in r.message
    assert [(v["id"], v["status"]) for v in r.detail["candidate_vendors"]] == [(1, "approved"), (2, "blocked")]


def test_ambiguity_without_a_blocked_candidate_says_so():
    ctx = _ambiguous_ctx(make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co", status=VendorStatus.NEW))
    r = ev_builtin("r_vendor_status", ctx)
    assert (r.severity, r.outcome_key) == (1, "ambiguous") and r.detail["blocked_candidate"] is False
    assert "BLOCKED" not in r.message


def test_a_blocked_vendor_outside_the_tie_does_not_set_the_flag():
    ctx = _ambiguous_ctx(make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co"),
                         make_vendor(3, "Unrelated Blocked Co", status=VendorStatus.BLOCKED), tied=[1, 2])
    assert ev_builtin("r_vendor_status", ctx).detail["blocked_candidate"] is False


def test_every_blocked_candidate_is_named():
    ctx = _ambiguous_ctx(make_vendor(1, "Acme Trading Co", status=VendorStatus.BLOCKED),
                         make_vendor(2, "Acme Tradings Co", status=VendorStatus.BLOCKED))
    r = ev_builtin("r_vendor_status", ctx)
    assert "'Acme Trading Co' and 'Acme Tradings Co'" in r.message and r.detail["blocked_candidate"] is True


def test_blocked_candidate_severity_still_follows_the_ambiguous_override():
    ctx = _ambiguous_ctx(make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co", status=VendorStatus.BLOCKED))
    assert ev("vendor_status", ctx, {"severity_by_outcome": {"ambiguous": 2}}).severity == 2


def test_end_to_end_blocked_near_tie_goes_to_review_with_the_risk_spelled_out():
    from tests.engine.real import pipeline
    from app.enums import Decision
    facts = make_facts(vendors=[make_vendor(1, "Acme Trading Co"), make_vendor(2, "Acme Tradings Co", status=VendorStatus.BLOCKED)],
                       pos=[make_po(vendor_id=1)])
    ctx, res, _ = pipeline(make_extracted(vendor_name="Acme Tradin"), facts)
    assert ctx.matched_vendor.ambiguous and ctx.matched_vendor.candidate_vendor_ids == [1, 2]
    assert res["r_vendor_status"].detail["blocked_candidate"] is True and ctx.decision is Decision.REVIEW


def test_low_score_with_no_reference_at_all_still_asks_the_vendor():
    ctx = make_ctx(matched=False, match_status=MatchStatus.LOW_SCORE, extracted=make_extracted(po_reference=None))
    ctx.candidates = [cand(score=0.3, reasons=["vendor:match"])]
    r = ev_builtin("r_po_found", ctx)
    assert (r.outcome_key, r.severity) == ("no_reference", 2)

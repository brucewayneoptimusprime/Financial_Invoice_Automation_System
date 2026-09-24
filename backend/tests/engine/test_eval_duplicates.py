from datetime import date

import pytest

from app.enums import InvoiceStatus, Outcome
from app.models.run import VendorMatch
from tests.engine.real import ev, ev_builtin
from tests.factories import field, make_ctx, make_extracted, make_facts, make_prior

CURRENT_NUMBER, CURRENT_HASH, CURRENT_TOTAL, CURRENT_DATE = "INV-1001", "hash-current", "1100.00", date(2026, 3, 14)


def exact(priors, **ctx_kw):
    ctx = make_ctx(facts=make_facts(priors=priors), **ctx_kw)
    return ev_builtin("r_duplicate_exact", ctx)


def fuzzy(priors, extracted=None, **ctx_kw):
    ctx = make_ctx(facts=make_facts(priors=priors), extracted=extracted or make_extracted(), **ctx_kw)
    return ev_builtin("r_duplicate_fuzzy", ctx)


# ------------------------------------------------------------------------------ exact: file hash

def test_no_priors_passes():
    r = exact([])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.PASS, 0, "no_duplicate")


def test_same_file_hash_as_an_approved_prior_is_a_hard_fail():
    r = exact([make_prior(file_hash=CURRENT_HASH, invoice_number="OTHER-1")])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FAIL, 3, "same_file_hash")
    assert r.detail["matches"][0]["prior_id"] == 100 and "same file" in r.message


@pytest.mark.parametrize("status", [InvoiceStatus.PENDING, InvoiceStatus.IN_REVIEW, InvoiceStatus.APPROVED])
def test_counted_statuses_all_trigger_on_hash(status):
    assert exact([make_prior(file_hash=CURRENT_HASH, status=status)]).severity == 3


@pytest.mark.parametrize("status", [InvoiceStatus.REJECTED, InvoiceStatus.AWAITING_INFO])
def test_same_file_hash_against_rejected_or_awaiting_info_is_a_severity_1_resubmission_flag(status):
    r = exact([make_prior(file_hash=CURRENT_HASH, status=status, invoice_number="OTHER-1")])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "resubmission")
    assert "resubmission" in r.message.lower() and r.detail["matches"][0]["prior_status"] == status.value


def test_null_hash_never_matches_null_hash():
    r = exact([make_prior(file_hash=None, invoice_number="OTHER-1")], file_hash=None)
    assert r.outcome is Outcome.PASS and any("missing:file_hash" in s for s in r.detail["skipped"])


def test_blank_hash_never_matches_blank_hash():
    assert exact([make_prior(file_hash="  ", invoice_number="OTHER-1")], file_hash="  ").outcome is Outcome.PASS


# ------------------------------------------------------------------------------ exact: vendor + number

def test_same_vendor_and_number_and_total_is_a_hard_fail():
    r = exact([make_prior(invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL)])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FAIL, 3, "same_vendor_number_same_total")


@pytest.mark.parametrize("prior_number", ["inv 1001", "INV/1001", "inv-01001", "Inv1001"])
def test_invoice_numbers_are_compared_normalised(prior_number):
    assert exact([make_prior(invoice_number=prior_number, total=CURRENT_TOTAL)]).outcome_key == "same_vendor_number_same_total"


def test_same_vendor_and_number_but_different_total_is_only_a_review_flag():
    r = exact([make_prior(invoice_number=CURRENT_NUMBER, total="999.00")])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "same_vendor_number_different_total")
    assert "revised" in r.message


def test_unknown_prior_total_cannot_confirm_a_same_total_duplicate():
    assert exact([make_prior(invoice_number=CURRENT_NUMBER, total=None)]).severity == 1


def test_same_number_from_a_different_vendor_is_not_a_duplicate():
    assert exact([make_prior(vendor_id=2, invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL)]).outcome is Outcome.PASS


@pytest.mark.parametrize("status", [InvoiceStatus.REJECTED, InvoiceStatus.AWAITING_INFO])
def test_number_only_match_against_rejected_or_awaiting_info_is_ignored(status):
    """A vendor resending a corrected invoice (same number, new file) after a request_info must not be rejected."""
    r = exact([make_prior(invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL, status=status, file_hash="different")])
    assert r.outcome is Outcome.PASS and r.detail["matches"] == []


def test_missing_number_skips_the_number_check_but_hash_check_still_runs():
    ctx_kw = dict(extracted=make_extracted(invoice_number=field(None, 0.99)))
    r = exact([make_prior(file_hash=CURRENT_HASH)], **ctx_kw)
    assert r.outcome_key == "same_file_hash"
    r = exact([make_prior(invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL)], **ctx_kw)
    assert r.outcome is Outcome.PASS and any("missing:invoice_number" in s for s in r.detail["skipped"])


def test_unresolved_vendor_skips_the_number_check_but_hash_still_matches():
    ctx = make_ctx(facts=make_facts(priors=[make_prior(file_hash=CURRENT_HASH)]))
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    assert ev_builtin("r_duplicate_exact", ctx).outcome_key == "same_file_hash"
    ctx = make_ctx(facts=make_facts(priors=[make_prior(invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL)]))
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    assert ev_builtin("r_duplicate_exact", ctx).outcome is Outcome.PASS


def test_nothing_to_compare_is_not_evaluable():
    ctx = make_ctx(extracted=make_extracted(invoice_number=None), file_hash=None)
    r = ev_builtin("r_duplicate_exact", ctx)
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "no_checkable_identifiers"


def test_strongest_match_wins_and_all_matches_are_listed():
    priors = [make_prior(id=1, invoice_number=CURRENT_NUMBER, total="1.00"),
              make_prior(id=2, file_hash=CURRENT_HASH, invoice_number="OTHER"),
              make_prior(id=3, invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL, file_hash="z")]
    r = exact(priors)
    assert r.outcome_key == "same_file_hash" and r.severity == 3
    assert sorted(m["prior_id"] for m in r.detail["matches"]) == [1, 2, 3]


# ------------------------------------------------------------------------------ exact: exclusions and normality

def test_the_current_run_is_excluded():
    own = make_prior(run_id="run-test", file_hash=CURRENT_HASH, invoice_number=CURRENT_NUMBER, total=CURRENT_TOTAL)
    assert exact([own]).outcome is Outcome.PASS
    other = make_prior(run_id="some-other-run", file_hash=CURRENT_HASH)
    assert exact([other]).outcome_key == "same_file_hash"


def test_a_second_invoice_against_the_same_po_from_the_same_vendor_is_normal():
    """Same vendor, same PO, different number / amount / file: NOT a duplicate by either rule."""
    prior = make_prior(po_id=1, vendor_id=1, invoice_number="INV-0500", total="400.00", file_hash="other-hash")
    assert exact([prior]).outcome is Outcome.PASS
    assert fuzzy([prior]).outcome is Outcome.PASS


def test_counted_statuses_are_configurable():
    prior = make_prior(file_hash=CURRENT_HASH, status=InvoiceStatus.REJECTED)
    ctx = make_ctx(facts=make_facts(priors=[prior]))
    r = ev_builtin("r_duplicate_exact", ctx, counted_statuses=["approved", "rejected"])
    assert r.outcome_key == "same_file_hash" and r.severity == 3


def test_partial_severity_override_keeps_the_other_defaults():
    ctx = make_ctx(facts=make_facts(priors=[make_prior(file_hash=CURRENT_HASH, status=InvoiceStatus.REJECTED)]))
    r = ev("duplicate_exact", ctx, {"severity_by_outcome": {"same_file_hash": 3}}, severity=3)
    assert r.outcome_key == "resubmission" and r.severity == 1


def test_no_facts_is_not_evaluable():
    ctx = make_ctx()
    ctx.facts = None
    assert ev_builtin("r_duplicate_exact", ctx).outcome is Outcome.INFO


# ------------------------------------------------------------------------------ fuzzy

def near(**kw):
    base = dict(id=200, invoice_number="INV-7777", invoice_date=date(2026, 3, 10), total=CURRENT_TOTAL, file_hash="other")
    return make_prior(**{**base, **kw})


def test_same_vendor_same_amount_close_date_different_number_is_flagged():
    r = fuzzy([near()])
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "near_duplicate")
    m = r.detail["matches"][0]
    assert m["prior_id"] == 200 and m["days_apart"] == 4 and m["prior_total"] == "1100.00"
    assert "different invoice number" in r.message


@pytest.mark.parametrize("prior_date,flagged", [
    (date(2026, 3, 7), True),      # exactly 7 days apart
    (date(2026, 3, 21), True),     # 7 days after
    (date(2026, 3, 6), False),     # 8 days
    (date(2026, 1, 13), False),    # 60 days
])
def test_date_window_boundary(prior_date, flagged):
    assert (fuzzy([near(invoice_date=prior_date)]).outcome is Outcome.FLAG) is flagged


def test_same_amount_but_a_different_vendor_or_amount_is_not_flagged():
    assert fuzzy([near(vendor_id=2)]).outcome is Outcome.PASS
    assert fuzzy([near(total="1100.01")]).outcome is Outcome.PASS


def test_amount_tolerance_param_widens_same_amount():
    ctx = make_ctx(facts=make_facts(priors=[near(total="1104.99")]))
    assert ev_builtin("r_duplicate_fuzzy", ctx).outcome is Outcome.PASS
    assert ev_builtin("r_duplicate_fuzzy", ctx, amount_tolerance=5.0).outcome is Outcome.FLAG
    ctx = make_ctx(facts=make_facts(priors=[near(total="1105.01")]))
    assert ev_builtin("r_duplicate_fuzzy", ctx, amount_tolerance=5.0).outcome is Outcome.PASS


def test_window_is_configurable():
    ctx = make_ctx(facts=make_facts(priors=[near(invoice_date=date(2026, 2, 20))]))
    assert ev_builtin("r_duplicate_fuzzy", ctx).outcome is Outcome.PASS
    assert ev_builtin("r_duplicate_fuzzy", ctx, days=30).outcome is Outcome.FLAG


def test_the_same_number_or_same_file_belongs_to_the_exact_rule_not_fuzzy():
    assert fuzzy([near(invoice_number="inv 1001")]).outcome is Outcome.PASS
    assert fuzzy([near(file_hash=CURRENT_HASH)]).outcome is Outcome.PASS


def test_prior_without_a_number_counts_as_a_different_number():
    assert fuzzy([near(invoice_number=None)]).outcome is Outcome.FLAG


@pytest.mark.parametrize("status", [InvoiceStatus.REJECTED, InvoiceStatus.AWAITING_INFO])
def test_uncounted_statuses_are_ignored_by_fuzzy(status):
    assert fuzzy([near(status=status)]).outcome is Outcome.PASS


def test_fuzzy_ignores_priors_with_unknown_date_total_or_other_currency_and_the_current_run():
    assert fuzzy([near(invoice_date=None)]).outcome is Outcome.PASS
    assert fuzzy([near(total=None)]).outcome is Outcome.PASS
    assert fuzzy([near(currency="EUR")]).outcome is Outcome.PASS
    assert fuzzy([near(run_id="run-test")]).outcome is Outcome.PASS


@pytest.mark.parametrize("over,reason", [
    (dict(invoice_date=field(None, 0.99)), "invoice_date"),
    (dict(total=field(None, 0.99)), "total"),
])
def test_fuzzy_null_inputs_are_missing_whatever_the_confidence(over, reason):
    r = fuzzy([near()], extracted=make_extracted(**over))
    assert r.outcome is Outcome.INFO and reason in r.detail["reason"]


def test_fuzzy_needs_a_resolved_vendor():
    ctx = make_ctx(facts=make_facts(priors=[near()]))
    ctx.matched_vendor = VendorMatch(vendor_id=None, method="none")
    r = ev_builtin("r_duplicate_fuzzy", ctx)
    assert r.outcome is Outcome.INFO and "vendor" in r.detail["reason"]


def test_fuzzy_reports_how_many_similar_priors_exist():
    r = fuzzy([near(id=1), near(id=2, invoice_number="INV-8888")])
    assert len(r.detail["matches"]) == 2 and "2 similar" in r.message

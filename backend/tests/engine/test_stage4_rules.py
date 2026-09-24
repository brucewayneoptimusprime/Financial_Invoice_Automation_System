"""Stage 4 rule changes: adjustments in the arithmetic, tax-id vendor resolution, r_document_type."""
from decimal import Decimal

import pytest

from app.config import MatchConfig
from app.engine.facts import VendorFact
from app.engine.vendor_match import resolve_vendor, tax_ids_match
from app.enums import Outcome, VendorStatus
from tests.engine.real import ev_builtin, pipeline
from tests.factories import field, make_ctx, make_extracted, make_facts, make_po, make_vendor

D = Decimal
CFG = MatchConfig()


def adj(kind, amount, conf=0.9):
    """An adjustment as the post-process leaves it: `amount` already signed by kind."""
    printed = abs(D(amount))
    signed = -printed if kind in ("discount", "credit") else printed if kind in ("shipping", "fee") else D(amount)
    return {"kind": kind, "amount": str(signed), "printed_amount": str(D(amount)), "confidence": conf}


def arithmetic(**over):
    return ev_builtin("r_arithmetic", make_ctx(extracted=make_extracted(**over)))


def check(r, name="subtotal_plus_tax_equals_total"):
    return next(c for c in r.detail["checks"] if c["check"] == name)


# ------------------------------------------------------------------------------------------- adjustments

def test_shipping_is_added_before_comparing_to_the_total():
    r = arithmetic(adjustments=[adj("shipping", "25.00")], total="1125.00")             # 1000 + 25 + 100
    assert r.outcome is Outcome.PASS
    c = check(r)
    assert (c["expected"], c["actual"], c["adjustments_total"], c["adjustment_count"]) == ("1125.00", "1125.00", "25.00", 1)


def test_a_discount_is_subtracted_and_a_credit_too():
    assert arithmetic(adjustments=[adj("discount", "50.00")], total="1050.00").outcome is Outcome.PASS       # 1000 - 50 + 100
    assert arithmetic(adjustments=[adj("credit", "30.00")], total="1070.00").outcome is Outcome.PASS


def test_sign_by_kind_shipping_discount_fee_and_rounding_together():
    adjustments = [adj("shipping", "20.00"), adj("discount", "15.00"), adj("fee", "5.00"), adj("rounding", "-0.40")]
    r = arithmetic(adjustments=adjustments, total="1109.60")                            # 1000 + 20 - 15 + 5 - 0.40 + 100
    assert r.outcome is Outcome.PASS and check(r)["adjustments_total"] == "9.60"


def test_the_wrong_sign_convention_would_be_caught():
    """A discount ADDED instead of subtracted would make this pass; it must fail."""
    r = arithmetic(adjustments=[adj("discount", "50.00")], total="1150.00")
    assert r.outcome is Outcome.FLAG and r.detail["failed"] == ["subtotal_plus_tax_equals_total"]


def test_a_mismatch_with_adjustments_is_flagged_with_the_numbers():
    r = arithmetic(adjustments=[adj("shipping", "25.00")], total="1100.00")            # the shipping is not in the total
    assert r.outcome is Outcome.FLAG and "expected 1,125.00, found 1,100.00" in r.message


def test_the_rounding_allowance_grows_with_the_number_of_terms():
    two = arithmetic(adjustments=[adj("shipping", "25.00"), adj("fee", "5.00")], total="1130.03")     # 3 cents out, 4 terms
    assert two.outcome is Outcome.PASS and check(two)["allowance"] == "0.04"
    assert arithmetic(adjustments=[adj("shipping", "25.00")], total="1125.04").outcome is Outcome.FLAG


def test_an_adjustment_with_no_amount_skips_the_total_check_instead_of_guessing():
    r = arithmetic(adjustments=[{"kind": "shipping", "amount": None, "confidence": 0.5}], total="9999.00")
    assert {"check": "subtotal_tax_total", "reason": "missing:adjustment_amount"} in r.detail["skipped"]


def test_adjustments_count_when_the_tax_is_included_or_absent():
    included = arithmetic(tax=field("100.00", included_in_total=True), adjustments=[adj("shipping", "25.00")], total="1025.00")
    assert included.outcome is Outcome.PASS and check(included, "total_equals_subtotal_tax_included")["expected"] == "1025.00"
    no_tax = arithmetic(tax=None, adjustments=[adj("shipping", "25.00"), adj("discount", "10.00")], total="1015.00")
    assert no_tax.outcome is Outcome.PASS and check(no_tax, "subtotal_plus_adjustments_equals_total")["tax_assumed_zero"] is True


# ------------------------------------------------------------------------------------------- tax-id vendors

def vendor(id, name, tax_id=None, aliases=(), status=VendorStatus.APPROVED):
    return VendorFact(id=id, name=name, tax_id=tax_id, aliases=aliases, status=status)


@pytest.mark.parametrize("a,b", [("12-3456789", "123456789"), ("gb 123-456.789", "GB123456789"), ("EIN 12-3456789", "12-3456789"),
                                 ("GSTIN 27AAPFU0939F1ZV", "27AAPFU0939F1ZV"), ("27aapfu0939f1zv", "27AAPFU0939F1ZV"),
                                 ("123456789", "GB123456789")])
def test_tax_ids_match_ignoring_case_separators_and_a_one_sided_label_or_country_prefix(a, b):
    assert tax_ids_match(a, b) and tax_ids_match(b, a)


@pytest.mark.parametrize("a,b", [("GB123456789", "DE123456789"), ("EIN 12-3456789", "GSTIN 12-3456789"), ("123456789", "123456780"),
                                 (None, "123"), ("", "123"), ("---", "..."), ("AB12", "12"), ("12345", "1234")])
def test_tax_ids_that_differ_do_not_match(a, b):
    assert not tax_ids_match(a, b)


def test_the_tax_id_resolves_the_vendor_even_when_the_name_differs():
    vendors = [vendor(1, "Acme Industries", "12-3456789"), vendor(2, "Other Co", "98-7654321")]
    vm = resolve_vendor("ACME Ind. (trading as Widgets R Us)", vendors, CFG, tax_id="EIN 12 3456789")
    assert (vm.vendor_id, vm.method, vm.score, vm.ambiguous) == (1, "tax_id", 1.0, False)


def test_the_tax_id_alone_is_enough_when_there_is_no_name():
    vm = resolve_vendor(None, [vendor(1, "Acme", "111-22-3333")], CFG, tax_id="111223333")
    assert vm.vendor_id == 1 and vm.method == "tax_id"


def test_a_tax_id_that_agrees_with_the_name_stays_a_clean_tax_id_match():
    vm = resolve_vendor("Acme Ltd", [vendor(1, "Acme Limited", "12-3456789")], CFG, tax_id="12-3456789")
    assert (vm.vendor_id, vm.method, vm.ambiguous) == (1, "tax_id", False)


def test_a_tax_id_and_a_name_pointing_at_different_vendors_are_ambiguous_with_both_candidates():
    vendors = [vendor(1, "Acme Industries", "12-3456789"), vendor(2, "Globex Corporation", "98-7654321")]
    vm = resolve_vendor("Globex Corporation", vendors, CFG, tax_id="12-3456789")
    assert vm.ambiguous and vm.method == "tax_id" and vm.vendor_id == 1 and vm.candidate_vendor_ids == [1, 2]
    assert vm.runner_up_score == 1.0


def test_the_conflict_with_a_blocked_vendor_is_spelled_out_by_the_vendor_rule():
    vendors = [vendor(1, "Acme Industries", "12-3456789"), vendor(2, "Globex Corporation", "98-7654321", status=VendorStatus.BLOCKED)]
    ctx, res, _ = pipeline(make_extracted(vendor_name="Globex Corporation", vendor_tax_id=field("12-3456789")),
                           make_facts(vendors=vendors, pos=[make_po(vendor_id=1)]))
    r = res["r_vendor_status"]
    assert r.outcome is Outcome.FLAG and r.outcome_key == "ambiguous" and r.detail["blocked_candidate"] is True
    assert ctx.decision.value != "approve"


def test_a_name_that_only_matches_the_same_vendor_is_not_a_conflict():
    vendors = [vendor(1, "Acme Industries", "12-3456789"), vendor(2, "Acme Industries Group"), vendor(3, "Acme Industries Grp")]
    vm = resolve_vendor("Acme Industries", vendors, CFG, tax_id="12-3456789")
    assert vm.vendor_id == 1 and vm.method == "tax_id" and not vm.ambiguous and vm.candidate_vendor_ids == []


def test_the_tax_id_beats_a_name_that_was_ambiguous_between_other_vendors_but_reports_the_conflict():
    vendors = [vendor(1, "Acme Industries", "12-3456789"), vendor(2, "Globex Corporation"), vendor(3, "Globex Corporation Ltd")]
    vm = resolve_vendor("Globex Corporation", vendors, CFG, tax_id="12-3456789")
    assert vm.vendor_id == 1 and vm.ambiguous and vm.candidate_vendor_ids == [1, 2, 3]


def test_the_same_tax_id_on_two_vendor_records_is_ambiguous():
    vm = resolve_vendor("Acme", [vendor(1, "Acme A", "12-3456789"), vendor(2, "Acme B", "123456789")], CFG, tax_id="12-3456789")
    assert vm.ambiguous and vm.candidate_vendor_ids == [1, 2] and vm.method == "tax_id"


def test_an_unknown_or_absent_tax_id_falls_back_to_the_name():
    vendors = [vendor(1, "Acme Industries", "12-3456789")]
    assert resolve_vendor("Acme Industries", vendors, CFG, tax_id="99-9999999").method == "exact_name"
    assert resolve_vendor("Acme Industries", vendors, CFG, tax_id=None).method == "exact_name"
    assert resolve_vendor("Acme Industries", [vendor(1, "Acme Industries")], CFG, tax_id="12-3456789").method == "exact_name"
    assert resolve_vendor(None, vendors, CFG, tax_id="99-9999999").vendor_id is None


def test_the_matching_stage_passes_the_extracted_tax_id():
    vendors = [vendor(1, "Acme Industries", "12-3456789")]
    ctx, _, (match, _, _) = pipeline(make_extracted(vendor_name="Totally Different Name", vendor_tax_id=field("EIN 12-3456789")),
                                     make_facts(vendors=vendors, pos=[make_po(vendor_id=1)]))
    assert ctx.matched_vendor.vendor_id == 1 and ctx.matched_vendor.method == "tax_id"
    event = match.events[0]
    assert event.event_type == "vendor_resolved" and "(tax_id, score 1.00)" in event.message


# ------------------------------------------------------------------------------------------ r_document_type

@pytest.mark.parametrize("kind", ["credit_note", "proforma", "quote", "statement", "receipt", "other"])
def test_anything_but_an_invoice_is_flagged_for_review(kind):
    r = ev_builtin("r_document_type", make_ctx(extracted=make_extracted(document_type=field(kind))))
    assert r.outcome is Outcome.FLAG and r.severity == 1 and r.outcome_key == "not_an_invoice"
    assert kind.replace("_", " ") in r.message and r.detail["document_type"] == kind


def test_an_invoice_passes_and_a_missing_type_is_not_evaluable():
    assert ev_builtin("r_document_type", make_ctx()).outcome is Outcome.PASS
    r = ev_builtin("r_document_type", make_ctx(extracted=make_extracted(document_type=field(None, 0.9))))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "missing:document_type"
    assert ev_builtin("r_document_type", make_ctx(extracted=None)).outcome is Outcome.INFO


def test_the_allowed_types_are_a_param():
    ctx = make_ctx(extracted=make_extracted(document_type=field("credit_note")))
    assert ev_builtin("r_document_type", ctx, allowed=["invoice", "credit_note"]).outcome is Outcome.PASS


def test_a_credit_note_never_auto_approves_even_when_everything_else_is_clean():
    ctx, res, _ = pipeline(make_extracted(document_type=field("credit_note")), make_facts())
    assert res["r_document_type"].outcome is Outcome.FLAG and ctx.decision.value == "review"

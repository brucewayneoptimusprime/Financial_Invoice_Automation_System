"""match -> validate -> decide with the REAL engine, on hand-written extracted invoices and facts.
No LLM, no seed data, no real invoice: each scenario exercises a general mechanism."""
import random
from decimal import Decimal as D

import pytest

from app.engine.facts import POLineFact
from app.engine.engine import run_decide_stage, run_validate_stage
from app.engine.matching import run_match_stage
from app.enums import Decision, InvoiceStatus, MatchStatus, Outcome, POStatus, VendorStatus
from app.models import RunContext
from tests.engine.real import BUILTIN
from tests.factories import field, make_extracted, make_facts, make_po, make_prior, make_vendor


def pipeline(extracted, facts, file_hash="hash-current", run_id="run-e2e"):
    ctx = RunContext(run_id=run_id, source_file="generic-invoice.pdf", file_hash=file_hash, extracted=extracted, facts=facts)
    match = run_match_stage(ctx)
    validate = run_validate_stage(ctx, list(BUILTIN.values()))
    decide = run_decide_stage(ctx)
    res = {r.rule_id: r for r in ctx.rule_results}
    return ctx, res, (match, validate, decide)


def triggered(res):
    return {k: v.outcome_key for k, v in res.items() if v.outcome in (Outcome.FLAG, Outcome.FAIL)}


def test_clean_invoice_is_approved_with_a_complete_trail():
    ctx, res, (match, validate, decide) = pipeline(make_extracted(), make_facts())
    assert ctx.decision is Decision.APPROVE and ctx.match_status is MatchStatus.MATCHED and ctx.matched_po.po_number == "PO-A-1"
    assert triggered(res) == {} and len(ctx.rule_results) == 13
    assert [e.event_type for e in match.events] == ["vendor_resolved", "po_candidates_ranked", "po_match_decision"]
    assert match.outputs["matched_po"] == "PO-A-1" and validate.outputs["final_severity"] == 0 and decide.outputs["decision"] == "approve"


def test_match_stage_records_scores_and_breakdown_in_the_trail():
    _, _, (match, _, _) = pipeline(make_extracted(), make_facts())
    ranked = match.events[1].detail["candidates"][0]
    assert ranked["po_number"] == "PO-A-1" and set(ranked["breakdown"]) == {"reference", "vendor", "amount", "lines"}
    assert match.events[2].outcome is Outcome.PASS and "Matched PO-A-1" in match.events[2].message


def test_invoice_without_a_po_reference_matches_on_other_signals_and_approves():
    ctx, res, _ = pipeline(make_extracted(po_reference=None), make_facts())
    assert ctx.match_status is MatchStatus.MATCHED and ctx.decision is Decision.APPROVE
    assert "other signals" in res["r_po_found"].message


def test_ambiguous_match_goes_to_review_even_though_every_other_rule_passes():
    facts = make_facts(pos=[make_po(id=1, po_number="PO-A-1"), make_po(id=2, po_number="PO-A-2")])
    ctx, res, (match, _, _) = pipeline(make_extracted(po_reference=None), facts)
    assert ctx.match_status is MatchStatus.AMBIGUOUS and ctx.matched_po is None and ctx.decision is Decision.REVIEW
    assert "r_po_ambiguity" in triggered(res) and res["engine_floor"].detail["reasons"][0]["code"] == "ambiguous_po_match"
    assert res["r_tolerance_pct"].outcome is Outcome.INFO                     # amount cannot be checked without a PO
    assert match.status.value == "flagged" and "Ambiguous" in match.events[2].message


def test_unknown_vendor_and_unknown_po_are_reviewed_never_approved():
    ex = make_extracted(vendor_name="Nobody Known Trading", po_reference="ZZ-000", line_items=[])
    ctx, res, _ = pipeline(ex, make_facts())
    assert ctx.match_status is MatchStatus.NO_CANDIDATES and ctx.decision is not Decision.APPROVE
    assert triggered(res)["r_vendor_status"] == "unknown" and triggered(res)["r_po_found"] == "reference_not_found"


def test_invoice_with_no_po_reference_and_no_match_asks_the_vendor():
    ex = make_extracted(po_reference=None, line_items=[], vendor_name="Nobody Known Trading")
    ctx, res, _ = pipeline(ex, make_facts())
    assert triggered(res)["r_po_found"] == "no_reference" and ctx.decision is Decision.REQUEST_INFO


def test_a_typo_of_a_blocked_vendor_is_rejected():
    facts = make_facts(vendors=[make_vendor(1, "Bad Actor Corp", status=VendorStatus.BLOCKED)])
    ctx, res, _ = pipeline(make_extracted(vendor_name="Bad Acter Corp"), facts)
    assert res["r_vendor_status"].outcome is Outcome.FAIL and ctx.decision is Decision.REJECT


def test_new_vendor_is_reviewed():
    ctx, res, _ = pipeline(make_extracted(), make_facts(vendors=[make_vendor(status=VendorStatus.NEW)]))
    assert triggered(res) == {"r_vendor_status": "new"} and ctx.decision is Decision.REVIEW


def test_invoice_referencing_another_vendors_po_is_reviewed_for_vendor_mismatch():
    facts = make_facts(vendors=[make_vendor(1), make_vendor(2, "Vendor Beta Inc")], pos=[make_po(vendor_id=2)])
    ctx, res, _ = pipeline(make_extracted(), facts)
    assert triggered(res) == {"r_vendor_po_mismatch": "mismatch"} and ctx.decision is Decision.REVIEW


def test_currency_mismatch_is_reviewed_and_amount_check_is_not_evaluated():
    ctx, res, _ = pipeline(make_extracted(currency="EUR"), make_facts())
    assert triggered(res)["r_currency_mismatch"] == "mismatch" and res["r_tolerance_pct"].outcome is Outcome.INFO
    assert ctx.decision is Decision.REVIEW


def test_missing_required_field_requests_info():
    ctx, res, _ = pipeline(make_extracted(invoice_number=field(None, 0.99)), make_facts())
    assert triggered(res)["r_required_fields"] == "missing" and ctx.decision is Decision.REQUEST_INFO


def test_over_balance_within_and_beyond_tolerance():
    facts = make_facts(pos=[make_po(total="1000.00")])
    within = make_extracted(total="1015.00", subtotal="1015.00", tax="0.00", line_items=[
        {"description": "Standard widget", "quantity": 10, "unit_price": "60.00", "amount": "600.00", "confidence": 0.9},
        {"description": "Premium gadget", "quantity": 5, "unit_price": "83.00", "amount": "415.00", "confidence": 0.9}])
    ctx, res, _ = pipeline(within, facts)
    assert res["r_tolerance_pct"].outcome_key == "within_tolerance" and ctx.decision is Decision.APPROVE
    ctx, res, _ = pipeline(make_extracted(total="1100.00"), facts)
    assert res["r_tolerance_pct"].outcome_key == "over_tolerance" and ctx.decision is Decision.REVIEW


def test_duplicate_file_is_rejected_and_resubmission_of_a_rejected_one_is_reviewed():
    ctx, res, _ = pipeline(make_extracted(), make_facts(priors=[make_prior(file_hash="hash-current")]))
    assert res["r_duplicate_exact"].outcome is Outcome.FAIL and ctx.decision is Decision.REJECT
    rejected = make_prior(file_hash="hash-current", status=InvoiceStatus.REJECTED)
    ctx, res, _ = pipeline(make_extracted(), make_facts(priors=[rejected]))
    assert triggered(res) == {"r_duplicate_exact": "resubmission"} and ctx.decision is Decision.REVIEW


def test_corrected_resend_after_request_info_is_not_rejected_as_a_duplicate():
    prior = make_prior(invoice_number="INV-1001", total="1100.00", status=InvoiceStatus.AWAITING_INFO, file_hash="old-file")
    ctx, res, _ = pipeline(make_extracted(), make_facts(priors=[prior]), file_hash="new-file")
    assert res["r_duplicate_exact"].outcome is Outcome.PASS and ctx.decision is Decision.APPROVE


def test_second_invoice_against_the_same_po_from_the_same_vendor_is_normal():
    prior = make_prior(po_id=1, invoice_number="INV-0500", total="400.00", file_hash="other", invoice_date=None)
    facts = make_facts(pos=[make_po(total="1600.00", net_committed="400.00", status=POStatus.PARTIALLY_BILLED)], priors=[prior])
    ctx, res, _ = pipeline(make_extracted(), facts)
    assert ctx.decision is Decision.APPROVE and triggered(res) == {}


def test_a_po_that_the_invoice_would_overdraw_is_reviewed_and_a_fully_billed_one_is_flagged():
    facts = make_facts(pos=[make_po(total="1200.00", net_committed="1200.00", status=POStatus.FULLY_BILLED)])
    ctx, res, _ = pipeline(make_extracted(), facts)
    assert triggered(res)["r_po_status"] == "fully_billed" and triggered(res)["r_tolerance_pct"] == "over_tolerance"
    assert ctx.decision is Decision.REVIEW


def _two_pos_one_nearly_consumed(second_lines=None):
    kw = {} if second_lines is None else {"lines": second_lines}
    return make_facts(pos=[
        make_po(id=1, po_number="PO-A-1", total="5000.00", net_committed="4400.00", status=POStatus.PARTIALLY_BILLED),  # balance 600
        make_po(id=2, po_number="PO-A-2", total="5000.00", **kw)])                                                       # balance 5000


def test_amount_fit_alone_is_not_enough_to_pick_between_two_otherwise_identical_pos():
    """No reference, same vendor, identical lines: an invoice equal to one PO's remaining balance ranks that PO
    first (0.20 vs 0.11) but the gap is under the ambiguity margin, so the engine asks a human instead of guessing."""
    ex = make_extracted(po_reference=None, subtotal="545.45", tax="54.55", total="600.00", line_items=[
        {"description": "Standard widget", "quantity": 10, "unit_price": "54.545", "amount": "545.45", "confidence": 0.9}])
    ctx, res, _ = pipeline(ex, _two_pos_one_nearly_consumed())
    assert [c.po_number for c in ctx.candidates] == ["PO-A-1", "PO-A-2"]
    assert ctx.candidates[0].breakdown["amount"] > ctx.candidates[1].breakdown["amount"]
    assert ctx.match_status is MatchStatus.AMBIGUOUS and ctx.matched_po is None and ctx.decision is Decision.REVIEW


def test_remaining_balance_plus_distinguishing_lines_resolves_the_match():
    other_lines = (POLineFact(line_no=1, description="Unrelated consulting", quantity=D(1), unit_price=D("5000.00"), amount=D("5000.00")),)
    ex = make_extracted(po_reference=None, subtotal="600.00", tax="0.00", total="600.00", line_items=[
        {"description": "Standard widget", "quantity": 10, "unit_price": "60.00", "amount": "600.00", "confidence": 0.9}])
    ctx, res, _ = pipeline(ex, _two_pos_one_nearly_consumed(other_lines))
    assert ctx.match_status is MatchStatus.MATCHED and ctx.matched_po.po_number == "PO-A-1"
    assert res["r_tolerance_pct"].outcome_key == "within_balance"


def test_the_whole_pipeline_is_deterministic_under_reordering():
    def build(seed):
        rng = random.Random(seed)
        vendors = [make_vendor(1), make_vendor(2, "Vendor Beta Inc")]
        pos = [make_po(id=1, po_number="PO-A-1"), make_po(id=2, po_number="PO-A-2", vendor_id=2, total="9000.00")]
        priors = [make_prior(id=1, file_hash="x"), make_prior(id=2, invoice_number="INV-2", total="1100.00", vendor_id=1)]
        for lst in (vendors, pos, priors):
            rng.shuffle(lst)
        return make_facts(vendors=vendors, pos=pos, priors=priors)

    dumps = []
    for seed in range(8):
        ctx, _, stages = pipeline(make_extracted(), build(seed))
        dumps.append([s.model_dump(mode="json") for s in stages] + [ctx.model_dump(mode="json", exclude={"facts"})])
    assert all(d == dumps[0] for d in dumps)


def test_match_stage_without_facts_or_extraction_degrades_safely():
    ctx = RunContext(run_id="r", source_file="f.pdf", extracted=make_extracted())
    stage = run_match_stage(ctx)
    assert ctx.match_status is MatchStatus.NO_CANDIDATES and stage.status.value == "flagged"
    ctx = RunContext(run_id="r", source_file="f.pdf", extracted=None, facts=make_facts())
    run_match_stage(ctx)
    assert ctx.match_status is MatchStatus.NO_CANDIDATES and ctx.matched_vendor.vendor_id is None
    validate = run_validate_stage(ctx, list(BUILTIN.values()))
    assert validate.outputs["final_severity"] >= 1


def test_all_builtin_rules_disabled_after_a_real_match_run_still_floors_when_unmatched():
    facts = make_facts(pos=[make_po(id=1, po_number="PO-A-1"), make_po(id=2, po_number="PO-A-2")])
    ctx = RunContext(run_id="r", source_file="f.pdf", file_hash="h", extracted=make_extracted(po_reference=None), facts=facts)
    run_match_stage(ctx)
    off = [r.model_copy(update={"enabled": False}) for r in BUILTIN.values()]
    assert run_validate_stage(ctx, off).outputs["final_severity"] >= 1

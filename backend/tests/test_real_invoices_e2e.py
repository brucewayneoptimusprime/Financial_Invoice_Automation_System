"""End to end, offline, on the two REAL invoices (SuperStore 10963 and 24429).

Real PDFs -> real ingest (real text layers) -> the model's recorded reply (played back, no API call) -> post-process ->
grounding -> vendor / PO matching -> the 13 builtin rules and both floors -> decision. The procurement facts are hand-written
(a SuperStore vendor and a PO with plenty of balance); nothing about these invoices is hard-coded in the application.
"""
import pytest

from app.enums import Decision, MatchStatus
from tests.real_e2e import facts_for, run_real, trail

NAMES = ("superstore_10963", "superstore_24429")

MATCHED_NO_REFERENCE = [
    ("r_arithmetic", "pass", 0, "consistent"), ("r_currency_mismatch", "pass", 0, "match"), ("r_document_type", "pass", 0, "allowed"),
    ("r_duplicate_exact", "pass", 0, "no_duplicate"), ("r_duplicate_fuzzy", "pass", 0, "no_near_duplicate"),
    ("r_extraction_confidence", "pass", 0, "confident"), ("r_po_ambiguity", "pass", 0, "unambiguous"), ("r_po_found", "flag", 1, "matched_without_reference"),
    ("r_po_line_price", "pass", 0, "within_tolerance"), ("r_po_status", "pass", 0, "open"), ("r_required_fields", "pass", 0, "complete"), ("r_tolerance_pct", "pass", 0, "within_balance"),
    ("r_vendor_po_mismatch", "pass", 0, "match"), ("r_vendor_status", "pass", 0, "approved"),
    ("engine_floor", "pass", 0, "floor_not_applied"), ("engine_floor_reference", "pass", 0, "reference_floor_not_applied"),
]

# Without a PO: nothing is wrong with the invoice, but there is nothing to match it to. The shipped rule asks for a PO.
NO_PO = [
    ("r_arithmetic", "pass", 0, "consistent"), ("r_currency_mismatch", "info", 0, "not_evaluable"), ("r_document_type", "pass", 0, "allowed"),
    ("r_duplicate_exact", "pass", 0, "no_duplicate"), ("r_duplicate_fuzzy", "pass", 0, "no_near_duplicate"),
    ("r_extraction_confidence", "pass", 0, "confident"), ("r_po_ambiguity", "pass", 0, "unambiguous"), ("r_po_found", "flag", 2, "no_reference"),
    ("r_po_line_price", "info", 0, "not_evaluable"), ("r_po_status", "info", 0, "not_evaluable"), ("r_required_fields", "pass", 0, "complete"), ("r_tolerance_pct", "info", 0, "not_evaluable"),
    ("r_vendor_po_mismatch", "info", 0, "not_evaluable"), ("r_vendor_status", "pass", 0, "approved"),
    ("engine_floor", "flag", 1, "floor_applied"), ("engine_floor_reference", "pass", 0, "reference_floor_not_applied"),
]


@pytest.mark.parametrize("name", NAMES)
def test_a_real_invoice_with_no_po_reference_and_a_confident_match_goes_to_review_on_that_one_rule(tmp_path, name):
    """Neither invoice prints a PO number (Order ID is not one). The match is strong, so a person confirms it."""
    ctx = run_real(tmp_path, name, facts_for(name))
    assert ctx.decision is Decision.REVIEW and ctx.match_status is MatchStatus.MATCHED and ctx.matched_po.po_number == "PO-SS-1"
    assert trail(ctx) == MATCHED_NO_REFERENCE
    assert {rid for rid, outcome, *_ in trail(ctx) if outcome in ("flag", "fail")} == {"r_po_found"}
    assert ctx.matched_vendor.vendor_id == 1 and ctx.matched_vendor.method == "exact_name"
    assert not ctx.extraction_meta.degraded and ctx.extraction_meta.injection_suspected is False


@pytest.mark.parametrize("name", NAMES)
def test_without_a_po_the_only_trigger_is_the_missing_po_and_the_decision_asks_for_it(tmp_path, name):
    ctx = run_real(tmp_path, name, facts_for(name, with_po=False))
    assert ctx.decision is Decision.REQUEST_INFO and ctx.match_status is MatchStatus.NO_CANDIDATES
    assert trail(ctx) == NO_PO
    triggered = {rid for rid, outcome, sev, _ in trail(ctx) if outcome in ("flag", "fail")}
    assert triggered == {"r_po_found", "engine_floor"}
    floor = next(r for r in ctx.rule_results if r.rule_id == "engine_floor")
    assert [r["code"] for r in floor.detail["reasons"]] == ["no_po_candidates"]


@pytest.mark.parametrize("name,total", [("superstore_10963", "5338.08"), ("superstore_24429", "1770.61")])
def test_the_amounts_the_rules_saw_are_the_printed_ones(tmp_path, name, total):
    ctx = run_real(tmp_path, name, facts_for(name))
    assert str(ctx.extracted.total.value) == total and ctx.extracted.currency.value == "USD"
    arithmetic = next(r for r in ctx.rule_results if r.rule_id == "r_arithmetic")
    names = {c["check"] for c in arithmetic.detail["checks"]}
    assert "subtotal_plus_adjustments_equals_total" in names and "line_math" in names and arithmetic.detail["skipped"] == []


def test_the_discount_on_24429_is_what_makes_the_arithmetic_work(tmp_path):
    """1845.94 - 184.59 + 109.26 = 1770.61: without signed adjustments this clean invoice would have been flagged."""
    ctx = run_real(tmp_path, "superstore_24429", facts_for("superstore_24429"))
    arithmetic = next(r for r in ctx.rule_results if r.rule_id == "r_arithmetic")
    check = next(c for c in arithmetic.detail["checks"] if c["check"] == "subtotal_plus_adjustments_equals_total")
    assert (check["expected"], check["actual"], check["adjustments_total"], check["tax_assumed_zero"]) == ("1770.61", "1770.61", "-75.33", True)
    line = next(c for c in arithmetic.detail["checks"] if c["check"] == "line_math")
    assert (line["expected"], line["actual"], line["ok"]) == ("1845.92", "1845.94", True)          # a unit price rounded to the cent


def test_a_po_without_enough_balance_is_never_approved(tmp_path):
    ctx = run_real(tmp_path, "superstore_10963", facts_for("superstore_10963", po_total="6000.00", net_committed="5500.00"))
    assert ctx.decision is not Decision.APPROVE                       # balance 500 against an invoice of 5,338.08
    assert any(outcome in ("flag", "fail") for _, outcome, *_ in trail(ctx))


def test_the_same_file_submitted_twice_is_a_duplicate(tmp_path):
    from app.engine.facts import PriorInvoiceFact
    from app.enums import InvoiceStatus
    from tests.factories import make_facts, make_po, make_vendor
    from tests.real_e2e import LINES

    ctx = run_real(tmp_path / "first", "superstore_10963", facts_for("superstore_10963"))
    prior = PriorInvoiceFact(id=1, vendor_id=1, invoice_number="10963", total=ctx.extracted.total.value, currency="USD",
                             status=InvoiceStatus.APPROVED, file_hash=ctx.file_hash)
    facts = make_facts(vendors=[make_vendor(1, "SuperStore")], priors=[prior],
                       pos=[make_po(id=1, po_number="PO-SS-1", vendor_id=1, total="6000.00", lines=(LINES["superstore_10963"],))])
    again = run_real(tmp_path / "second", "superstore_10963", facts)
    assert again.decision is Decision.REJECT
    assert {rid for rid, outcome, *_ in trail(again) if outcome in ("flag", "fail")} >= {"r_duplicate_exact"}


@pytest.mark.parametrize("field,value", [("quantity", "5"), ("unit_price", "471.48"), ("amount", "1854.94")])
def test_a_real_line_item_mistake_is_caught_by_the_arithmetic_and_never_approved(tmp_path, field, value):
    """The allowance for unit prices rounded to the cent must not hide a genuine error on a real invoice."""
    def mistake(reply):
        reply["line_items"][0][field] = value

    ctx = run_real(tmp_path, "superstore_24429", facts_for("superstore_24429"), edit=mistake)
    arithmetic = next(r for r in ctx.rule_results if r.rule_id == "r_arithmetic")
    assert arithmetic.outcome.value == "flag" and ctx.decision is not Decision.APPROVE

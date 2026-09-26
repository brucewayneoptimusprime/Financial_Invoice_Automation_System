"""The trail digest, the template explanation and the template drafts: deterministic, from the audit trail only."""
import pytest

from app.config import Settings
from app.enums import Decision, VendorStatus
from app.models.extraction_meta import ExtractionMeta
from app.pipeline.actions import plan_actions, review_reason
from app.pipeline.digest import build_digest, numbers_in
from app.pipeline.draft import draft_kind, request_line, template_draft
from app.pipeline.explain import template_explanation
from tests.factories import field, make_extracted, make_facts, make_po, make_vendor

S = Settings(_env_file=None)
INTERNAL_WORDS = ("r_", "severity", "blocked", "score", "threshold", "engine", "floor", "status approved", "tolerance")


def digest_for(extracted, facts, meta=None):
    from app.engine.engine import run_decide_stage, run_validate_stage
    from app.engine.matching import run_match_stage
    from app.models import RunContext
    from tests.engine.real import BUILTIN

    ctx = RunContext(run_id="run-e2e", source_file="generic-invoice.pdf", file_hash="hash-current", extracted=extracted, facts=facts,
                     extraction_meta=meta)
    run_match_stage(ctx)
    run_validate_stage(ctx, list(BUILTIN.values()))
    run_decide_stage(ctx)
    return ctx, build_digest(ctx, S, {"r_po_found": "Invoice matches a purchase order"})


def by_rule(digest, rule_id):
    return [f for f in digest.facts if f.rule_id == rule_id]


# ---------------------------------------------------------------------------------------------- numbers

@pytest.mark.parametrize("text,expected", [
    ("expected 1,125.00, found 1,100.00 (difference 25.00)", {"1125.00", "1100.00", "25.00"}),
    ("score 0.57, dated 2013-03-07, 3 checks", {"0.57", "2013-03-07", "3"}), ("no numbers here", set()),
    ("INV-1001 total 50.", {"1001", "50"})])
def test_numbers_are_extracted_without_commas_and_dates_stay_whole(text, expected):
    assert numbers_in(text) == expected


# ------------------------------------------------------------------------------------------------ digest

def test_a_clean_approve_digest_is_the_decision_the_passes_the_vendor_the_match_and_the_invoice():
    ctx, d = digest_for(make_extracted(), make_facts())
    assert d.decision is Decision.APPROVE and d.severity == 0 and d.triggered == ()
    assert [f.kind for f in d.facts] == ["decision", "summary", "vendor", "match", "invoice"]
    assert [f.id for f in d.facts] == ["F1", "F2", "F3", "F4", "F5"]
    assert "14 checks passed" in d.facts[1].text and "PO-A-1 is a confident" in d.facts[3].text
    assert "Vendor Alpha Ltd" in d.facts[2].text and "total 1100.00 USD" in d.facts[4].text


def test_triggered_facts_use_the_engines_own_words_highest_severity_first():
    ctx, d = digest_for(make_extracted(po_reference="ZZ-000"), make_facts(pos=[make_po(vendor_id=99)]))
    assert d.decision is Decision.REQUEST_INFO and d.severity == 2
    first = d.triggered[0]
    assert first.severity == 2 and first.rule_id in ("r_po_found", "engine_floor")
    rule = by_rule(d, "r_po_found")[0]
    assert "reference_not_found, severity 2" in rule.text and "ZZ-000" in rule.text and rule.text.endswith("does not match any purchase order.")
    sev = [f.severity for f in d.triggered]
    assert sev == sorted(sev, reverse=True)


def test_only_configured_rules_are_vendor_facing_and_vendor_status_never_is():
    ctx, d = digest_for(make_extracted(vendor_name="Nobody Known Trading", po_reference="ZZ-000", total=None), make_facts())
    cats = {f.rule_id: f.category for f in d.triggered if f.kind == "rule"}
    assert cats["r_required_fields"] == "missing_fields" and cats["r_po_found"] == "po_reference"
    assert cats["r_vendor_status"] is None
    assert all(f.category is None for f in d.kind("vendor") + d.kind("match") + d.kind("summary"))


def test_floor_reasons_become_separate_facts_and_a_vendor_side_failure_is_vendor_facing():
    meta = ExtractionMeta(degraded=True, failure_kind="vendor_side", failure_code="password_protected", failure_reason="locked")
    ctx, d = digest_for(make_extracted(vendor_name=None, invoice_number=None, total=None), make_facts(), meta)
    floor = [f for f in d.facts if f.kind == "floor"]
    assert any(f.outcome_key == "extraction_degraded" and f.category == "unreadable_file" and f.items == ("password_protected",) for f in floor)
    assert any(f.outcome_key == "required_fields_missing" and f.category is None for f in floor)
    system = ExtractionMeta(degraded=True, failure_kind="system_side", failure_code="timeout", failure_reason="x")
    ctx, d = digest_for(make_extracted(vendor_name=None, total=None), make_facts(), system)
    assert not any(f.category == "unreadable_file" for f in d.facts)                 # our failure is never presented to the vendor


def test_extraction_problems_appear_as_facts():
    meta = ExtractionMeta(grounding={"exact": 5, "value_mismatch": 2, "not_found": 1})
    ctx, d = digest_for(make_extracted(), make_facts(), meta)
    text = [f.text for f in d.kind("extraction")]
    assert text == ["Extracted values that the document text did not support: 1 not found, 2 value mismatch."]


def test_the_prompt_view_carries_only_ids_kinds_text_severity_category_and_items():
    ctx, d = digest_for(make_extracted(total=None), make_facts())
    view = d.prompt_dict()
    assert set(view) == {"decision", "severity", "facts"} and view["decision"] == d.decision.value
    assert all(set(f) == {"id", "kind", "text", "severity", "category", "items"} for f in view["facts"])
    assert not any("detail" in f or "data" in f for f in view["facts"])
    narrowed = d.prompt_dict(only=d.vendor_facing)
    assert [f["id"] for f in narrowed["facts"]] == [f.id for f in d.vendor_facing]


def test_the_digest_is_deterministic():
    a = digest_for(make_extracted(po_reference=None), make_facts())[1]
    b = digest_for(make_extracted(po_reference=None), make_facts())[1]
    assert a == b and a.prompt_dict() == b.prompt_dict()


def test_an_override_replaces_the_decision_and_adds_a_triggered_fact():
    ctx, d = digest_for(make_extracted(), make_facts())
    o = d.with_override("Approval withheld: records changed.", Decision.REVIEW, 1)
    assert o.decision is Decision.REVIEW and o.facts[0].text.startswith("Final decision: review (severity 1)")
    assert o.triggered[-1].kind == "override" and o.triggered[-1].id == f"F{len(d.facts) + 1}" and d.triggered == ()


def test_allowed_numbers_come_from_the_facts_only():
    ctx, d = digest_for(make_extracted(po_reference=None), make_facts())
    allowed = d.allowed_numbers()
    assert "1100.00" in allowed and "9999.99" not in allowed
    assert d.allowed_numbers(["F1"]) <= allowed


# ------------------------------------------------------------------------------------------- explanation

def test_the_template_explanation_cites_every_triggered_fact_and_names_the_next_step():
    ctx, d = digest_for(make_extracted(po_reference=None), make_facts())
    e = template_explanation(d)
    assert e.source == "template" and e.model is None and e.cost_usd == 0
    assert e.text.startswith("Decision: REVIEW.") and "A person needs to look at this invoice" in e.text
    assert "matched_without_reference" in e.text and "[F2]" in e.text and "Next step: A reviewer should check" in e.text
    assert e.cited == tuple(f.id for f in d.triggered) and e.one_line == "review: r_po_found matched_without_reference"


def test_a_clean_invoice_explains_that_all_checks_passed():
    ctx, d = digest_for(make_extracted(), make_facts())
    e = template_explanation(d)
    assert e.text.startswith("Decision: APPROVE.") and "14 checks passed" in e.text and e.one_line == "approve: all checks passed"
    assert e.cited == ("F2",)


@pytest.mark.parametrize("decision", list(Decision))
def test_every_decision_has_a_meaning_and_a_next_step(decision):
    from app.pipeline.templates import DECISION_MEANING, NEXT_STEP

    assert decision in DECISION_MEANING and decision in NEXT_STEP


def test_the_explanation_never_states_a_different_decision_than_the_real_one():
    ctx, d = digest_for(make_extracted(po_reference=None), make_facts())
    text = template_explanation(d).text.lower()
    assert "decision: review" in text and "decision: approve" not in text and "decision: reject" not in text


# ---------------------------------------------------------------------------------------- actions/plans

def test_the_action_table_per_decision():
    ctx, approve = digest_for(make_extracted(), make_facts())
    assert plan_actions(ctx, approve, S).commit_ledger and plan_actions(ctx, approve, S).ready_for_payment
    ctx, review = digest_for(make_extracted(po_reference=None), make_facts())
    p = plan_actions(ctx, review, S)
    assert p.review_reason and not p.commit_ledger and not p.draft and "r_po_found (matched_without_reference)" in p.review_reason
    ctx, ask = digest_for(make_extracted(po_reference="ZZ-000"), make_facts(pos=[make_po(vendor_id=99)]))
    p = plan_actions(ctx, ask, S)
    assert p.draft and not p.commit_ledger and p.review_reason is None


def test_the_review_reason_is_capped():
    ctx, d = digest_for(make_extracted(po_reference=None), make_facts())
    tiny = Settings(_env_file=None, review_reason_max_chars=60)
    text = review_reason(d, tiny)
    assert len(text) <= 60 and text.endswith("…") and text.startswith("Review: ")


# ------------------------------------------------------------------------------------------------ drafts

def test_request_info_email_lists_exactly_the_vendor_facing_items_and_nothing_internal():
    ctx, d = digest_for(make_extracted(vendor_name="Nobody Known Trading", po_reference="ZZ-000", total=None), make_facts())
    draft = template_draft(d, S)
    assert draft.kind == "vendor_email" and draft.to is None and draft.source == "template"
    assert draft.subject == "Invoice INV-1001: information needed"
    assert draft.body.startswith("Dear Nobody Known Trading,") and "the total amount" in draft.body
    assert "ZZ-000 on the invoice does not match any purchase order" in draft.body
    assert draft.body.rstrip().endswith("Kind regards,\nAccounts Payable")
    low = draft.body.lower()
    assert not any(w in low for w in INTERNAL_WORDS) and "unknown vendor" not in low and "new vendor" not in low


def test_a_duplicate_reject_email_gives_the_reason_without_internal_ids():
    from app.engine.facts import PriorInvoiceFact
    from app.enums import InvoiceStatus

    prior = PriorInvoiceFact(id=41, vendor_id=1, invoice_number="INV-1001", total="1100.00", currency="USD", status=InvoiceStatus.APPROVED,
                             file_hash="other")
    ctx, d = digest_for(make_extracted(), make_facts(priors=[prior]))
    assert d.decision is Decision.REJECT
    draft = template_draft(d, S)
    assert draft.kind == "vendor_email" and draft.subject == "Invoice INV-1001: unable to process"
    assert "We are unable to process it" in draft.body and "already received an invoice with this number and amount" in draft.body
    assert "41" not in draft.body and not any(w in draft.body.lower() for w in INTERNAL_WORDS)


def test_a_blocked_vendor_reject_is_an_internal_note_never_an_email():
    vendors = [make_vendor(1, "Vendor Alpha Ltd", status=VendorStatus.BLOCKED)]
    ctx, d = digest_for(make_extracted(), make_facts(vendors=vendors))
    assert d.decision is Decision.REJECT and draft_kind(d, S) == "notification"
    draft = template_draft(d, S)
    assert draft.kind == "notification" and draft.to is None
    assert draft.body.startswith("Internal note (no email was drafted to the vendor).") and "BLOCKED" in draft.body
    assert draft.body.rstrip().endswith("Nothing was sent.")


def test_a_reject_with_a_blocked_vendor_and_a_duplicate_is_still_internal_only():
    from app.engine.facts import PriorInvoiceFact
    from app.enums import InvoiceStatus

    prior = PriorInvoiceFact(id=1, vendor_id=1, invoice_number="INV-1001", total="1100.00", currency="USD", status=InvoiceStatus.APPROVED)
    vendors = [make_vendor(1, "Vendor Alpha Ltd", status=VendorStatus.BLOCKED)]
    ctx, d = digest_for(make_extracted(), make_facts(vendors=vendors, priors=[prior]))
    assert d.vendor_facing and draft_kind(d, S) == "notification"                 # a vendor-facing reason exists, but the vendor is blocked


def test_a_vendor_side_file_failure_asks_for_the_file_again():
    meta = ExtractionMeta(degraded=True, failure_kind="vendor_side", failure_code="password_protected", failure_reason="locked")
    ctx, d = digest_for(make_extracted(vendor_name=None, invoice_number=None, invoice_date=None, currency=None, total=None,
                                       subtotal=None, tax=None, po_reference=None, line_items=[]), make_facts(), meta)
    draft = template_draft(d, S)
    assert draft.subject == "Your invoice: information needed" and draft.body.startswith("Dear Sir or Madam,")
    assert "the file is password-protected and cannot be opened" in draft.body and "Please send it again" in draft.body


def test_request_lines_for_each_category_are_vendor_safe():
    ctx, d = digest_for(make_extracted(total="1200.00"), make_facts())
    arithmetic = next(f for f in d.triggered if f.rule_id == "r_arithmetic")
    line = request_line(arithmetic)
    assert "do not add up" in line and "1,100.00" not in line and "Please send a corrected invoice" in line
    assert not any(w in line.lower() for w in INTERNAL_WORDS)


def test_a_notification_is_used_when_nothing_is_vendor_facing():
    ctx, d = digest_for(make_extracted(), make_facts(vendors=[make_vendor(1, "Vendor Alpha Ltd", status=VendorStatus.NEW)]))
    assert d.decision is Decision.REVIEW and not d.vendor_facing and draft_kind(d, S) == "notification"


def test_the_review_reason_uses_the_engines_message_once_not_the_digest_wording_twice():
    ctx, d = digest_for(make_extracted(po_reference=None), make_facts())
    reason = review_reason(d, S)
    assert reason == ("Review: r_po_found (matched_without_reference): The invoice has no PO reference; purchase order PO-A-1 is a confident, "
                      "unambiguous match on vendor, amount and lines. A person should confirm it.")

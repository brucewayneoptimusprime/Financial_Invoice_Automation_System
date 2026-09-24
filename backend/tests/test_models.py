import logging
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.enums import Decision, Outcome
from app.models import AuditEvent, ExtractedInvoice, Rule, RunContext, StageResult
from app.models.extraction import EvidencedField
from app.models.rules import RuleResult
from app.models.run import POCandidate

# The example from SPEC 6.1, made concrete.
SPEC_EXAMPLE = {
    "vendor_name": {"value": "Acme Supplies Ltd", "page": 1, "source_text": "ACME SUPPLIES LTD", "confidence": 0.97},
    "invoice_number": {"value": None, "page": None, "source_text": None, "confidence": 0.0},
    "invoice_date": {"value": "2026-03-14", "page": 1, "source_text": "14 Mar 2026", "confidence": 0.93},
    "currency": {"value": "USD", "page": 1, "source_text": "USD", "confidence": 0.99},
    "po_reference": {"value": "PO-1001", "explicit": True, "page": 1, "source_text": "PO-1001", "confidence": 0.9},
    "subtotal": {"value": 2000.00, "page": 1, "source_text": "2,000.00", "confidence": 0.95},
    "tax": {"value": 160.00, "included_in_total": False, "page": 1, "source_text": "160.00", "confidence": 0.9},
    "total": {"value": 2160.00, "page": 1, "source_text": "2,160.00", "confidence": 0.96},
    "line_items": [
        {"description": "Widgets", "quantity": 40, "unit_price": 50.0, "amount": 2000.0, "page": 1, "confidence": 0.9}
    ],
    "document_quality": {"type": "scanned", "issues": ["skewed", "low_resolution"]},
    "extraction_notes": "free text",
}

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "currency", "po_reference", "subtotal", "tax", "total"]


# ---------------------------------------------------------------- extracted invoice: valid

def test_spec_example_is_accepted():
    inv = ExtractedInvoice.model_validate(SPEC_EXAMPLE)
    assert inv.vendor_name.value == "Acme Supplies Ltd"
    assert inv.invoice_date.value == date(2026, 3, 14)
    assert inv.total.value == Decimal("2160.0")
    assert inv.po_reference.explicit is True
    assert inv.tax.included_in_total is False
    assert inv.line_items[0].quantity == Decimal(40)
    assert inv.document_quality.type == "scanned"


def test_amounts_parse_as_exact_decimals_not_floats():
    inv = ExtractedInvoice.model_validate({"total": {"value": 0.1, "confidence": 0.9}})
    assert inv.total.value == Decimal("0.1")


@pytest.mark.parametrize("field", FIELDS)
def test_every_field_accepts_null_value_with_zero_confidence(field):
    inv = ExtractedInvoice.model_validate({field: {"value": None, "page": None, "source_text": None, "confidence": 0.0}})
    f = getattr(inv, field)
    assert f.value is None and f.confidence == 0.0


def test_all_fields_null_is_a_valid_extraction():
    nulls = {f: {"value": None, "page": None, "source_text": None, "confidence": 0.0} for f in FIELDS}
    nulls.update(line_items=[], document_quality={"type": None, "issues": []}, extraction_notes=None)
    inv = ExtractedInvoice.model_validate(nulls)
    assert all(getattr(inv, f).value is None for f in FIELDS)


@pytest.mark.parametrize("field", FIELDS)
def test_omitted_field_defaults_to_null_with_zero_confidence(field):
    f = getattr(ExtractedInvoice.model_validate({}), field)
    assert f.value is None and f.confidence == 0.0 and f.page is None and f.source_text is None


def test_null_line_items_and_null_line_values_are_valid():
    inv = ExtractedInvoice.model_validate({"line_items": None})
    assert inv.line_items == []
    inv = ExtractedInvoice.model_validate({"line_items": [{"description": None, "quantity": None, "unit_price": None,
                                                           "amount": None, "page": None, "confidence": 0.0}]})
    assert inv.line_items[0].amount is None


def test_currency_is_normalised():
    assert ExtractedInvoice.model_validate({"currency": {"value": " usd ", "confidence": 0.9}}).currency.value == "USD"


def test_round_trips_through_json():
    inv = ExtractedInvoice.model_validate(SPEC_EXAMPLE)
    assert ExtractedInvoice.model_validate_json(inv.model_dump_json()) == inv


# ---------------------------------------------------------------- extracted invoice: invalid

@pytest.mark.parametrize("bad", [-0.1, 1.01, "high"])
def test_confidence_must_be_between_0_and_1(bad):
    with pytest.raises(ValidationError):
        ExtractedInvoice.model_validate({"total": {"value": 1, "confidence": bad}})


def test_line_item_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ExtractedInvoice.model_validate({"line_items": [{"amount": 1, "confidence": 2}]})


@pytest.mark.parametrize("payload", [
    {"total": {"value": "not a number", "confidence": 0.9}},
    {"total": {"value": "NaN", "confidence": 0.9}},
    {"tax": {"value": "Infinity", "confidence": 0.9}},
    {"invoice_date": {"value": "not a date", "confidence": 0.9}},
    {"currency": {"value": "US", "confidence": 0.9}},
    {"currency": {"value": "$", "confidence": 0.9}},
    {"vendor_name": {"value": "x", "page": 0, "confidence": 0.9}},
    {"document_quality": {"type": "hologram"}},
    {"line_items": [{"quantity": "many"}]},
    {"line_items": "none"},
])
def test_invalid_extracted_invoice_is_rejected(payload):
    with pytest.raises(ValidationError):
        ExtractedInvoice.model_validate(payload)


def test_unknown_keys_are_ignored_and_logged_for_llm_output(caplog):
    payload = {"vendor_name": {"value": "A", "confidence": 0.9, "surprise": 1}, "made_up_field": "x"}
    with caplog.at_level(logging.WARNING, logger="app.models.extraction"):
        inv = ExtractedInvoice.model_validate(payload)
    assert inv.vendor_name.value == "A"
    assert not hasattr(inv, "made_up_field")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "made_up_field" in logged and "surprise" in logged


def test_evidenced_field_generic_is_ignore_mode():
    assert EvidencedField[str].model_validate({"value": "x", "junk": 1}).value == "x"


# ---------------------------------------------------------------- rule schema

def _rule(**over):
    base = {"id": "r_tolerance_pct", "name": "Invoice within tolerance of PO balance", "type": "amount_tolerance",
            "params": {"pct": 2.0, "abs": 50.0}, "severity_on_trigger": 1, "source": "builtin", "enabled": True,
            "original_text": None}
    base.update(over)
    return base


def test_spec_rule_example_is_accepted():
    r = Rule.model_validate(_rule())
    assert r.severity_on_trigger == 1 and r.params["pct"] == 2.0


@pytest.mark.parametrize("sev", [1, 2, 3])
def test_rule_severity_1_to_3_accepted(sev):
    assert Rule.model_validate(_rule(severity_on_trigger=sev)).severity_on_trigger == sev


@pytest.mark.parametrize("sev", [0, -1, 4, 99])
def test_rule_severity_zero_or_out_of_range_rejected(sev):
    with pytest.raises(ValidationError):
        Rule.model_validate(_rule(severity_on_trigger=sev))


@pytest.mark.parametrize("over", [
    {"source": "admin"},
    {"id": ""},
    {"type": ""},
    {"unknown_key": 1},          # internal model: extra="forbid"
    {"enabled": "maybe"},
    {"params": [1, 2]},
])
def test_invalid_rule_rejected(over):
    with pytest.raises(ValidationError):
        Rule.model_validate(_rule(**over))


def test_nl_rule_requires_original_text():
    with pytest.raises(ValidationError):
        Rule.model_validate(_rule(source="nl", original_text=None))
    with pytest.raises(ValidationError):
        Rule.model_validate(_rule(source="nl", original_text="   "))
    ok = Rule.model_validate(_rule(source="nl", original_text="Flag anything over $10k"))
    assert ok.original_text == "Flag anything over $10k"


def test_rule_result_validates():
    r = RuleResult(rule_id="r1", outcome="flag", severity=1, message="over", detail={"invoice": 10, "balance": 5})
    assert r.outcome is Outcome.FLAG
    with pytest.raises(ValidationError):
        RuleResult(rule_id="r1", outcome="flag", severity=-1, message="x")


# ---------------------------------------------------------------- audit event / stage result / run context

def test_audit_event_valid_and_invalid():
    e = AuditEvent(stage="validate", event_type="rule_evaluated", rule_id="r1", outcome="pass", message="ok", detail={"n": 1})
    assert e.outcome is Outcome.PASS and e.seq is None
    with pytest.raises(ValidationError):
        AuditEvent(stage="validate", event_type="x", outcome="great", message="m")
    with pytest.raises(ValidationError):
        AuditEvent(stage="", event_type="x", outcome="pass", message="m")
    with pytest.raises(ValidationError):
        AuditEvent(stage="s", event_type="x", outcome="pass", message="m", seq=-1)
    with pytest.raises(ValidationError):
        AuditEvent(stage="s", event_type="x", outcome="pass", message="m", bogus=1)


def test_stage_result_valid_and_invalid():
    ev = AuditEvent(stage="extract", event_type="done", outcome="info", message="m")
    sr = StageResult(stage="extract", status="ok", outputs={"pages": 2}, events=[ev])
    assert sr.events[0].stage == "extract"
    with pytest.raises(ValidationError):
        StageResult(stage="extract", status="exploded")
    with pytest.raises(ValidationError):
        StageResult(stage="extract", status="ok", events=[{"stage": "x"}])
    with pytest.raises(ValidationError):
        StageResult(stage="extract", status="ok", surprise=1)


def test_run_context_valid_and_mutable():
    ctx = RunContext(run_id="run-1", source_file="inv.pdf")
    assert ctx.extracted is None and ctx.candidates == [] and ctx.decision is None
    ctx.extracted = ExtractedInvoice.model_validate(SPEC_EXAMPLE)
    ctx.matched_po = POCandidate(po_id=1, po_number="PO-1001", score=0.9)
    ctx.decision = "review"
    assert ctx.decision is Decision.REVIEW


def test_run_context_invalid():
    with pytest.raises(ValidationError):
        RunContext(run_id="", source_file="inv.pdf")
    with pytest.raises(ValidationError):
        RunContext(run_id="r", source_file="inv.pdf", surprise=1)
    ctx = RunContext(run_id="r", source_file="inv.pdf")
    with pytest.raises(ValidationError):
        ctx.decision = "maybe"           # assignment is validated
    with pytest.raises(ValidationError):
        POCandidate(po_id=1, po_number="P", score=1.5)

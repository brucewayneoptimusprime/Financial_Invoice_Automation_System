import pytest

from app.enums import Outcome
from tests.engine.real import ev, ev_builtin
from tests.factories import field, make_ctx, make_extracted, make_facts

REQUIRED = ["vendor_name", "invoice_number", "invoice_date", "currency", "total"]


def test_complete_invoice_passes():
    r = ev_builtin("r_required_fields", make_ctx())
    assert (r.outcome, r.severity) == (Outcome.PASS, 0) and r.detail["missing"] == []


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("nullified", [None, "high_confidence_null"])
def test_every_required_field_null_is_missing_regardless_of_confidence(name, nullified):
    value = {None: None, "high_confidence_null": field(None, 0.99)}[nullified]
    r = ev_builtin("r_required_fields", make_ctx(extracted=make_extracted(**{name: value})))
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 2, "missing")
    assert r.detail["missing"] == [name] and name in r.message


@pytest.mark.parametrize("name", ["vendor_name", "invoice_number"])
def test_blank_text_is_missing_too(name):
    r = ev_builtin("r_required_fields", make_ctx(extracted=make_extracted(**{name: field("   ", 0.99)})))
    assert r.detail["missing"] == [name]


def test_all_missing_fields_are_listed():
    r = ev_builtin("r_required_fields", make_ctx(extracted=make_extracted(invoice_number=None, total=None)))
    assert r.detail["missing"] == ["invoice_number", "total"]
    assert r.detail["present"] == ["vendor_name", "invoice_date", "currency"]


def test_zero_is_a_value_not_a_missing_field():
    ctx = make_ctx(extracted=make_extracted(tax=field("0.00", included_in_total=False)))
    assert ev("required_fields", ctx, {"fields": ["tax"]}).outcome is Outcome.PASS


def test_a_low_confidence_value_is_present_for_completeness():
    ctx = make_ctx(extracted=make_extracted(total=field("1100.00", 0.01)))
    assert ev_builtin("r_required_fields", ctx).outcome is Outcome.PASS


def test_missing_extraction_means_every_field_is_missing():
    r = ev_builtin("r_required_fields", make_ctx(extracted=None))
    assert r.outcome is Outcome.FLAG and r.detail["missing"] == REQUIRED


def test_field_list_is_a_param_and_validated():
    ctx = make_ctx(extracted=make_extracted(po_reference=None))
    assert ev("required_fields", ctx, {"fields": ["po_reference"]}).outcome is Outcome.FLAG
    assert ev("required_fields", make_ctx(extracted=make_extracted(line_items=[])), {"fields": ["line_items"]}).outcome is Outcome.FLAG
    for bad in ({"fields": ["not_a_field"]}, {"fields": []}, {"fieldz": ["total"]}):
        assert ev("required_fields", ctx, bad).outcome_key == "invalid_params"


def test_default_required_fields_come_from_config():
    r = ev("required_fields", make_ctx(extracted=make_extracted(currency=None)), {})
    assert r.detail["required"] == REQUIRED and r.detail["missing"] == ["currency"]


# ------------------------------------------------------------------------------ extraction_confidence

def test_confident_extraction_passes_and_lists_what_was_checked():
    r = ev_builtin("r_extraction_confidence", make_ctx())
    assert r.outcome is Outcome.PASS and len(r.detail["checked"]) == 5 and r.detail["threshold"] == 0.8


def test_low_confidence_field_is_flagged_with_the_numbers():
    ctx = make_ctx(extracted=make_extracted(total=field("1100.00", 0.55), invoice_date=field("2026-03-14", 0.7)))
    r = ev_builtin("r_extraction_confidence", ctx)
    assert (r.outcome, r.severity, r.outcome_key) == (Outcome.FLAG, 1, "low_confidence")
    assert r.detail["low_confidence"] == [{"field": "invoice_date", "confidence": 0.7, "threshold": 0.8},
                                          {"field": "total", "confidence": 0.55, "threshold": 0.8}] or \
           sorted(x["field"] for x in r.detail["low_confidence"]) == ["invoice_date", "total"]
    assert "0.55 < 0.80" in r.message


def test_confidence_equal_to_the_threshold_passes():
    assert ev_builtin("r_extraction_confidence", make_ctx(extracted=make_extracted(total=field("1100.00", 0.8)))).outcome is Outcome.PASS


def test_null_fields_are_skipped_by_the_confidence_rule_not_flagged():
    r = ev_builtin("r_extraction_confidence", make_ctx(extracted=make_extracted(invoice_number=field(None, 0.0))))
    assert r.outcome is Outcome.PASS and r.detail["skipped_missing"] == ["invoice_number"]
    assert "invoice_number" not in [c["field"] for c in r.detail["checked"]]


def test_a_null_value_with_high_confidence_is_still_missing_not_confident():
    r = ev_builtin("r_extraction_confidence", make_ctx(extracted=make_extracted(total=field(None, 0.99))))
    assert r.detail["skipped_missing"] == ["total"] and "total" not in [c["field"] for c in r.detail["checked"]]


def test_all_fields_missing_is_not_evaluable():
    nulls = {k: None for k in REQUIRED}
    r = ev_builtin("r_extraction_confidence", make_ctx(extracted=make_extracted(**nulls)))
    assert r.outcome is Outcome.INFO and r.detail["reason"] == "no_present_fields"


def test_threshold_comes_from_the_facts_snapshot():
    r = ev_builtin("r_extraction_confidence", make_ctx(facts=make_facts(threshold=0.99)))
    assert r.outcome is Outcome.FLAG and r.detail["threshold"] == 0.99
    assert ev_builtin("r_extraction_confidence", make_ctx(facts=make_facts(threshold=0.1))).outcome is Outcome.PASS


def test_confidence_rule_not_evaluable_without_facts_or_extraction():
    ctx = make_ctx()
    ctx.facts = None
    assert ev_builtin("r_extraction_confidence", ctx).outcome is Outcome.INFO
    assert ev_builtin("r_extraction_confidence", make_ctx(extracted=None)).outcome is Outcome.INFO


def test_confidence_field_list_can_include_po_reference():
    ctx = make_ctx(extracted=make_extracted(po_reference={"value": "PO-A-1", "explicit": True, "confidence": 0.2}))
    assert ev("extraction_confidence", ctx, {"fields": ["po_reference"]}).outcome is Outcome.FLAG

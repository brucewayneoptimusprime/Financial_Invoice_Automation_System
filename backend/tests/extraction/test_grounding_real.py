"""Grounding against the REAL invoices: the recorded model replies (live check, ~$0.023 each) and the real PDF text layers.

The text layers list labels and values in separate blocks ("Subtotal:", "Shipping:", "Total:" after the amounts), so
"Subtotal: $5,141.76" is not a substring of the page. Those fields must come out `value_present` at the 0.85 cap, not
`not_found`. No live calls: the reply is played back through a scripted client.
"""
from decimal import Decimal

import pytest

from app.enums import GroundingStatus as G
from tests.extraction.real import NAMES, real_extraction, real_ingest

D = Decimal


def page_text(ctx) -> str:
    from pathlib import Path

    return Path(ctx.ingest.text_paths[0]).read_text(encoding="utf-8")


def test_the_real_text_layers_separate_labels_from_values(tmp_path):
    """Documents the layout the fallback exists for, so a change in the fixtures cannot silently void the tests."""
    for name in NAMES:
        ctx, _ = real_ingest(tmp_path / name, name)
        text = page_text(ctx)
        assert ctx.ingest.text_layer.usable and "Subtotal:\n" in text and "Total:\n" in text
        assert "Subtotal: $" not in text and "Total: $" not in text and "Shipping: $" not in text


@pytest.mark.parametrize("name", NAMES)
def test_nothing_on_the_real_invoices_is_not_found(tmp_path, name):
    _, _, out = real_extraction(tmp_path, name)
    assert not out.meta.degraded
    assert G.NOT_FOUND.value not in out.meta.grounding and G.VALUE_MISMATCH.value not in out.meta.grounding
    assert G.NO_SOURCE.value not in out.meta.grounding and G.UNAVAILABLE.value not in out.meta.grounding


@pytest.mark.parametrize("name,total,subtotal", [("superstore_10963", "5338.08", "5141.76"), ("superstore_24429", "1770.61", "1845.94")])
def test_label_separated_amounts_are_value_present_at_the_cap(tmp_path, name, total, subtotal):
    _, _, out = real_extraction(tmp_path, name)
    inv = out.invoice
    for field, value in ((inv.subtotal, subtotal), (inv.total, total)):
        assert field.value == D(value) and field.grounding is G.VALUE_PRESENT
        assert field.model_confidence == 0.95 and field.confidence == 0.85        # 0.95 capped at the value_present cap
    assert inv.total.confidence > 0.8                                             # still above the review threshold


def test_10963_statuses_field_by_field(tmp_path):
    _, _, out = real_extraction(tmp_path, "superstore_10963")
    inv = out.invoice
    got = {n: getattr(inv, n).grounding for n in ("vendor_name", "document_type", "invoice_number", "invoice_date", "currency")}
    assert got == {"vendor_name": G.EXACT, "document_type": G.EXACT, "invoice_number": G.EXACT,
                   "invoice_date": G.VALUE_PRESENT, "currency": G.EXACT}          # "Date: Mar 07 2013": the label is elsewhere
    assert inv.invoice_date.confidence == 0.85 and inv.invoice_date.value.isoformat() == "2013-03-07"
    assert inv.line_items[0].grounding is G.EXACT and inv.line_items[0].confidence == 0.9
    assert [(a.kind, a.grounding, a.confidence) for a in inv.adjustments] == [("shipping", G.VALUE_PRESENT, 0.85)]
    assert out.meta.grounding == {"exact": 5, "value_present": 4}
    assert inv.tax.grounding is None and inv.po_reference.grounding is None       # null fields are not graded


def test_24429_statuses_field_by_field(tmp_path):
    _, _, out = real_extraction(tmp_path, "superstore_24429")
    inv = out.invoice
    assert inv.invoice_date.grounding is G.EXACT and inv.invoice_date.confidence == 0.9       # "Mar 07 2013" is verbatim
    assert [(a.kind, a.amount, a.grounding, a.confidence) for a in inv.adjustments] == [
        ("discount", D("-184.59"), G.VALUE_PRESENT, 0.85), ("shipping", D("109.26"), G.VALUE_PRESENT, 0.85)]
    assert inv.line_items[0].grounding is G.EXACT and inv.line_items[0].item_code == "FUR-CH-4682"
    assert out.meta.grounding == {"exact": 6, "value_present": 4}


@pytest.mark.parametrize("name,raw", [("superstore_10963", 0.8), ("superstore_24429", 0.7)])
def test_the_dollar_sign_currency_gets_its_confidence_from_config(tmp_path, name, raw):
    """24429's currency came back at 0.70: without this a clean invoice would go to review."""
    _, cfg, out = real_extraction(tmp_path, name)
    cur = out.invoice.currency
    assert cur.value == "USD" and cur.model_confidence == raw and cur.confidence == cfg.currency_symbol_confidence == 0.85
    assert cur.grounding is G.EXACT                                                       # "$5,338.08" is on the page
    notes = out.invoice.extraction_notes
    assert "currency '$' was mapped to USD by configuration" in notes
    assert f"currency confidence set to 0.85 from configuration (symbol-derived; the model reported {raw:g})" in notes


def test_the_symbol_confidence_is_a_config_value(tmp_path):
    _, _, out = real_extraction(tmp_path, "superstore_24429", currency_symbol_confidence=0.6)
    assert out.invoice.currency.confidence == 0.6 and out.invoice.currency.model_confidence == 0.7


def test_the_real_replies_are_not_degraded_and_cost_what_the_live_check_did(tmp_path):
    _, _, out = real_extraction(tmp_path, "superstore_10963")
    assert out.meta.path == "text_and_vision" and out.meta.attempts == 1 and out.meta.tokens_in == 6800


def test_the_grounding_event_is_recorded_with_the_counts(tmp_path):
    _, _, out = real_extraction(tmp_path, "superstore_10963")
    event = next(e for e in out.events if e.event_type == "grounding")
    assert event.detail["counts"] == {"exact": 5, "value_present": 4} and event.detail["text_layer_usable"] is True
    assert event.message == "grounding: 9 item(s) checked: 5 exact, 4 value_present"
    assert [e.event_type for e in out.events][-2:] == ["grounding", "extraction_complete"]


def test_a_misread_amount_on_a_real_invoice_is_caught(tmp_path):
    """The model 'reads' 5,338.80 for the total. Nothing on the real page supports it."""
    import json

    from app.extraction.extractor import extract_invoice
    from tests.extraction.real import real_reply
    from tests.extraction.wire_convert import set_field
    from tests.llm.fakes import FakeLLMClient, ok_response

    ctx, cfg = real_ingest(tmp_path, "superstore_10963")
    reply = real_reply("superstore_10963")
    set_field(reply, "total", value="5338.80")
    out = extract_invoice(ctx.ingest, client=FakeLLMClient(ok_response(json.dumps(reply))), settings=cfg)
    assert out.invoice.total.grounding is G.VALUE_MISMATCH and out.invoice.total.confidence == 0.30
    assert any("total does not agree with its own source_text" in n for n in out.invoice.extraction_notes.splitlines())

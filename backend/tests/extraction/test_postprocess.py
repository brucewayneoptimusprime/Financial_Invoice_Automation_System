from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.extraction.postprocess import AMBIGUOUS_DATE_CAP, postprocess
from tests.extraction.helpers import load_reply

D = Decimal
S = Settings(_env_file=None)


def us(**patch):
    reply = load_reply("us_native_invoice")
    for key, value in patch.items():
        reply[key] = {**reply[key], **value} if isinstance(value, dict) else value
    return reply


def adjustment(kind, amount):
    return {"adjustments": [{"kind": kind, "description": "x", "amount": amount, "page": 1, "source_text": f"{kind} {amount}", "confidence": 0.9}]}


# ------------------------------------------------------------------------------ the happy path

def test_a_clean_us_reply_validates_with_exact_decimals():
    r = postprocess(load_reply("us_native_invoice"), S)
    inv = r.invoice
    assert (inv.subtotal.value, inv.tax.value, inv.total.value) == (D("1000.00"), D("80.00"), D("1105.00"))
    assert inv.currency.value == "USD" and inv.invoice_date.value.isoformat() == "2026-03-14"
    assert inv.po_reference.value == "PO-5001" and inv.po_reference.explicit is True
    assert inv.vendor_tax_id.value == "EIN 12-3456789" and inv.document_type.value == "invoice"
    assert inv.line_items[0].item_code == "WID-A" and inv.line_items[1].amount == D("400.00")
    assert r.notes == [] and inv.extraction_notes is None


def test_arithmetic_of_the_fixture_is_consistent_including_the_adjustment():
    inv = postprocess(load_reply("us_native_invoice"), S).invoice
    assert inv.subtotal.value + sum(a.amount for a in inv.adjustments) + inv.tax.value == inv.total.value


def test_model_confidence_is_recorded_and_effective_confidence_starts_equal():
    inv = postprocess(load_reply("us_native_invoice"), S).invoice
    for name in ("vendor_name", "invoice_number", "invoice_date", "currency", "total"):
        f = getattr(inv, name)
        assert f.model_confidence == f.confidence and f.grounding is None
    assert all(x.model_confidence == x.confidence for x in [*inv.line_items, *inv.adjustments])


def test_source_text_is_kept_verbatim():
    inv = postprocess(load_reply("us_native_invoice"), S).invoice
    assert inv.total.source_text == "Total Due: 1,105.00" and inv.line_items[0].source_text == "Widget A WID-A 10 60.00 600.00"


def test_the_input_dict_is_not_mutated():
    raw = load_reply("indian_gst_invoice")
    before = deepcopy(raw)
    postprocess(raw, S)
    assert raw == before


# ------------------------------------------------------------------------------ Indian format

def test_indian_reply_currency_symbol_and_digit_grouping():
    r = postprocess(load_reply("indian_gst_invoice"), S)
    inv = r.invoice
    assert inv.currency.value == "INR" and any("mapped to INR" in n for n in r.notes)
    assert inv.subtotal.value == D("100000.00") and inv.tax.value == D("18000.00") and inv.total.value == D("116000.00")
    li = inv.line_items[0]
    assert (li.quantity, li.unit_price, li.amount) == (D("100"), D("1000.00"), D("100000.00"))
    assert inv.vendor_tax_id.value == "27AAAPL1234C1Z5"


def test_indian_reply_keeps_the_models_component_tax_note_and_adds_system_notes():
    inv = postprocess(load_reply("indian_gst_invoice"), S).invoice
    assert inv.extraction_notes.startswith("Tax is the sum of CGST 9,000.00 and SGST 9,000.00 = 18,000.00.")
    assert "[system] currency '₹' was mapped to INR by configuration" in inv.extraction_notes


def test_indian_discount_is_applied_with_a_minus_sign_and_the_arithmetic_closes():
    inv = postprocess(load_reply("indian_gst_invoice"), S).invoice
    a = inv.adjustments[0]
    assert (a.kind, a.printed_amount, a.amount) == ("discount", D("2000.00"), D("-2000.00"))
    assert inv.subtotal.value + a.amount + inv.tax.value == inv.total.value          # 100000 - 2000 + 18000 = 116000


@pytest.mark.parametrize("symbol", ["Rs", "Rs.", "₹", "INR", "inr"])
def test_every_rupee_form_becomes_inr(symbol):
    assert postprocess(us(currency={"value": symbol}), S).invoice.currency.value == "INR"


# ------------------------------------------------------------------------------ European format

def test_european_reply_amounts_date_and_currency():
    inv = postprocess(load_reply("eu_format_invoice"), S).invoice
    assert (inv.subtotal.value, inv.tax.value, inv.total.value) == (D("1234.56"), D("234.57"), D("1469.13"))
    assert inv.subtotal.value + inv.tax.value == inv.total.value
    assert inv.invoice_date.value.isoformat() == "2026-03-14" and inv.currency.value == "EUR"
    assert inv.vendor_address.value is None and inv.vendor_address.confidence == 0.0


# ------------------------------------------------------------------------------ adjustment signs

@pytest.mark.parametrize("kind,printed,expected", [
    ("shipping", "25.00", "25.00"), ("shipping", "-25.00", "25.00"), ("fee", "3.00", "3.00"), ("fee", "(3.00)", "3.00"),
    ("discount", "10.00", "-10.00"), ("discount", "-10.00", "-10.00"), ("discount", "(10.00)", "-10.00"),
    ("credit", "5.00", "-5.00"), ("credit", "-5.00", "-5.00"),
    ("rounding", "-0.02", "-0.02"), ("rounding", "0.02", "0.02"), ("other", "-4.00", "-4.00"), ("other", "4.00", "4.00"),
])
def test_adjustment_sign_is_applied_by_kind(kind, printed, expected):
    a = postprocess(us(**adjustment(kind, printed)), S).invoice.adjustments[0]
    assert a.amount == D(expected)
    assert a.printed_amount == D(printed.strip("()").lstrip("-")) * (-1 if printed.startswith(("-", "(")) else 1)


def test_adjustment_with_no_kind_keeps_the_printed_amount():
    reply = us(adjustments=[{"kind": None, "description": None, "amount": "-7.00", "page": 1, "source_text": "x", "confidence": 0.5}])
    assert postprocess(reply, S).invoice.adjustments[0].amount == D("-7.00")


def test_adjustment_with_no_amount_stays_null():
    a = postprocess(us(**adjustment("fee", None)), S).invoice.adjustments[0]
    assert a.amount is None and a.printed_amount is None


def test_signs_are_idempotent_when_the_result_is_processed_again():
    first = postprocess(load_reply("indian_gst_invoice"), S).invoice
    again = postprocess(first.model_dump(mode="json"), S).invoice
    assert [a.amount for a in again.adjustments] == [a.amount for a in first.adjustments]
    assert [a.printed_amount for a in again.adjustments] == [a.printed_amount for a in first.adjustments]


def test_several_adjustments_of_different_kinds():
    reply = us(adjustments=[
        {"kind": "shipping", "description": "a", "amount": "10.00", "page": 1, "source_text": "s", "confidence": 0.9},
        {"kind": "discount", "description": "b", "amount": "4.00", "page": 1, "source_text": "d", "confidence": 0.9},
        {"kind": "rounding", "description": "c", "amount": "-0.01", "page": 1, "source_text": "r", "confidence": 0.9}])
    assert [a.amount for a in postprocess(reply, S).invoice.adjustments] == [D("10.00"), D("-4.00"), D("-0.01")]


# ------------------------------------------------------------------------------ currency

def test_an_ambiguous_symbol_becomes_null_with_a_note_and_zero_confidence():
    r = postprocess(us(currency={"value": "¥", "confidence": 0.9}), S)
    assert r.invoice.currency.value is None and r.invoice.currency.confidence == 0.0
    assert any("ambiguous or unsupported" in n for n in r.notes) and "[system]" in r.invoice.extraction_notes


def test_dollar_maps_to_usd_by_default_and_the_map_is_configurable():
    assert postprocess(us(currency={"value": "$"}), S).invoice.currency.value == "USD"
    yen = Settings(_env_file=None, currency_symbol_map={"¥": "JPY"})
    assert postprocess(us(currency={"value": "¥"}), yen).invoice.currency.value == "JPY"
    assert postprocess(us(currency={"value": "$"}), yen).invoice.currency.value is None


def test_a_plain_code_gets_no_system_note():
    r = postprocess(us(currency={"value": "usd"}), S)
    assert r.invoice.currency.value == "USD" and r.notes == []


# ------------------------------------------------------------------------------ dates

def test_a_non_iso_date_is_normalised_when_unambiguous():
    assert postprocess(us(invoice_date={"value": "14 Mar 2026"}), S).invoice.invoice_date.value.isoformat() == "2026-03-14"


def test_an_ambiguous_day_month_date_is_capped_at_half_confidence_with_a_note():
    reply = us(invoice_date={"value": "2026-04-03", "source_text": "Date: 03/04/2026", "confidence": 0.95})
    r = postprocess(reply, S)
    f = r.invoice.invoice_date
    assert f.confidence == AMBIGUOUS_DATE_CAP == 0.5 and f.model_confidence == 0.95
    assert "ambiguous day/month order" in r.invoice.extraction_notes and "2026-04-03" in r.invoice.extraction_notes


def test_low_confidence_on_an_ambiguous_date_is_not_raised():
    reply = us(invoice_date={"value": "2026-04-03", "source_text": "03/04/2026", "confidence": 0.3})
    assert postprocess(reply, S).invoice.invoice_date.confidence == 0.3


@pytest.mark.parametrize("source", ["Date: 13/04/2026", "March 14, 2026", "2026-03-14", "03/03/2026"])
def test_unambiguous_dates_keep_their_confidence(source):
    reply = us(invoice_date={"source_text": source, "confidence": 0.97})
    r = postprocess(reply, S)
    assert r.invoice.invoice_date.confidence == 0.97 and not any("ambiguous" in n for n in r.notes)


def test_an_unparseable_date_is_a_schema_failure_so_the_repair_retry_can_fix_it():
    with pytest.raises(ValidationError):
        postprocess(us(invoice_date={"value": "03/04/2026"}), S)


# ------------------------------------------------------------------------------ nulls, numbers, robustness

def test_a_null_value_is_forced_to_zero_confidence_and_loses_its_evidence():
    inv = postprocess(us(total={"value": None, "confidence": 0.9, "page": 1, "source_text": "Total: ???"}), S).invoice
    assert inv.total.value is None and inv.total.confidence == 0.0 and inv.total.page is None and inv.total.source_text is None
    assert inv.total.model_confidence == 0.0


def test_numbers_that_arrive_as_json_numbers_stay_exact():
    inv = postprocess(us(total={"value": 1105.5}, subtotal={"value": 1000}), S).invoice
    assert inv.total.value == D("1105.5") and inv.subtotal.value == D("1000")


def test_quantity_with_a_unit_word_still_yields_the_number():
    reply = us(line_items=[{"description": "Bolts", "item_code": None, "quantity": "10 pcs", "unit_price": "2.50",
                            "amount": "25.00", "page": 1, "source_text": "Bolts 10 pcs", "confidence": 0.9}])
    assert postprocess(reply, S).invoice.line_items[0].quantity == D("10")


def test_sub_cent_amounts_are_preserved_for_the_rules_to_flag():
    assert postprocess(us(total={"value": "10.005"}), S).invoice.total.value == D("10.005")


def test_an_unreadable_amount_is_a_schema_failure():
    with pytest.raises(ValidationError):
        postprocess(us(total={"value": "abc"}), S)


@pytest.mark.parametrize("raw", [[], "text", 5, None])
def test_a_non_object_reply_is_rejected(raw):
    with pytest.raises(ValueError):
        postprocess(raw, S)


def test_missing_optional_structures_default_cleanly():
    r = postprocess({}, S)
    assert r.invoice.total.value is None and r.invoice.adjustments == [] and r.invoice.line_items == []


def test_unknown_keys_from_the_model_are_ignored():
    reply = us(surprise="boo")
    reply["total"]["extra"] = 1
    assert postprocess(reply, S).invoice.total.value == D("1105.00")


def test_reader_instruction_flag_and_notes_pass_through_untouched():
    inv = postprocess(load_reply("injection_attempt"), S).invoice
    assert inv.document_quality.contains_reader_instructions is True and "IGNORE ALL PREVIOUS INSTRUCTIONS" in inv.extraction_notes
    assert inv.total.value == D("500.00")                                             # the instruction changed nothing

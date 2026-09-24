import logging
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import DEFAULT_CURRENCY_SYMBOL_MAP
from app.engine.normalize import normalize_tax_id
from app.enums import GroundingStatus
from app.extraction.parsing import (
    amount_candidates, date_candidates, find_amounts, is_ambiguous_dmy, map_currency, normalize_amount_string,
    normalize_date_string,
)
from app.models import ExtractedInvoice
from app.models.extraction import ExtractedAdjustment, ExtractedLineItem

D = Decimal
SYMBOLS = dict(DEFAULT_CURRENCY_SYMBOL_MAP)


# ------------------------------------------------------------------------------ contract additions

def test_new_fields_default_to_missing_with_zero_confidence():
    inv = ExtractedInvoice()
    for name in ("vendor_tax_id", "vendor_address", "document_type"):
        f = getattr(inv, name)
        assert f.value is None and f.confidence == 0.0 and f.model_confidence is None and f.grounding is None
    assert inv.adjustments == [] and inv.document_quality.contains_reader_instructions is None


def test_null_with_zero_confidence_is_valid_for_every_new_field():
    raw = {n: {"value": None, "page": None, "source_text": None, "confidence": 0.0}
           for n in ("vendor_tax_id", "vendor_address", "document_type")}
    assert ExtractedInvoice.model_validate(raw).vendor_tax_id.value is None


@pytest.mark.parametrize("doc_type", ["invoice", "credit_note", "proforma", "quote", "statement", "receipt", "other"])
def test_document_type_accepts_the_seven_types(doc_type):
    assert ExtractedInvoice.model_validate({"document_type": {"value": doc_type, "confidence": 0.9}}).document_type.value == doc_type


@pytest.mark.parametrize("bad", ["memo", "Invoice", "", 5])
def test_document_type_rejects_anything_else(bad):
    with pytest.raises(ValidationError):
        ExtractedInvoice.model_validate({"document_type": {"value": bad, "confidence": 0.9}})


def test_adjustments_line_item_code_and_reader_flag_round_trip():
    inv = ExtractedInvoice.model_validate({
        "adjustments": [{"kind": "shipping", "description": "Freight", "amount": "25.00", "page": 1, "confidence": 0.9}],
        "line_items": [{"description": "x", "item_code": "SKU-1", "amount": "1.00"}],
        "document_quality": {"type": "native", "issues": [], "contains_reader_instructions": True}})
    assert inv.adjustments[0].kind == "shipping" and inv.adjustments[0].amount == D("25.00")
    assert inv.line_items[0].item_code == "SKU-1" and inv.document_quality.contains_reader_instructions is True
    assert ExtractedInvoice.model_validate_json(inv.model_dump_json()) == inv


@pytest.mark.parametrize("bad", [{"kind": "bribe"}, {"kind": "shipping", "amount": "NaN"}, {"kind": "fee", "confidence": 2},
                                 {"kind": "fee", "page": 0}])
def test_invalid_adjustments_are_rejected(bad):
    with pytest.raises(ValidationError):
        ExtractedAdjustment.model_validate(bad)


def test_null_adjustment_list_and_null_kind_are_tolerated():
    assert ExtractedInvoice.model_validate({"adjustments": None}).adjustments == []
    assert ExtractedAdjustment.model_validate({"kind": None, "amount": None}).kind is None


def test_system_fields_are_validated():
    inv = ExtractedInvoice.model_validate(
        {"total": {"value": "1.00", "confidence": 0.5, "model_confidence": 0.9, "grounding": "value_present"}})
    assert inv.total.model_confidence == 0.9 and inv.total.grounding is GroundingStatus.VALUE_PRESENT
    for bad in ({"model_confidence": 1.5}, {"grounding": "vibes"}):
        with pytest.raises(ValidationError):
            ExtractedInvoice.model_validate({"total": {"value": "1", "confidence": 0.5, **bad}})
    with pytest.raises(ValidationError):
        ExtractedLineItem.model_validate({"grounding": "vibes"})


def test_grounding_status_vocabulary():
    assert {g.value for g in GroundingStatus} == {"exact", "normalized", "fuzzy", "value_present", "not_found",
                                                  "value_mismatch", "no_source", "unavailable"}


def test_unknown_keys_in_the_new_structures_are_ignored_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="app.models.extraction"):
        inv = ExtractedInvoice.model_validate({"vendor_tax_id": {"value": "X1", "confidence": 0.9, "surprise": 1},
                                               "adjustments": [{"kind": "fee", "mystery": True}]})
    assert inv.vendor_tax_id.value == "X1"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "surprise" in logged and "mystery" in logged


# ------------------------------------------------------------------------------ tax id normalisation

@pytest.mark.parametrize("a,b", [("gb 123-456.789", "GB123456789"), ("27aaapl1234c1z5", "27AAAPL1234C1Z5"),
                                 ("DE 123 456 789", "de123456789"), ("EIN 12-3456789", "ein123456789"),
                                 (" 12.345.678/0001-90 ", "12345678/000190")])
def test_tax_ids_compare_ignoring_case_spaces_hyphens_and_dots(a, b):
    assert normalize_tax_id(a) == normalize_tax_id(b)


def test_tax_ids_that_really_differ_stay_different():
    assert normalize_tax_id("GB123456789") != normalize_tax_id("GB123456788")
    assert normalize_tax_id("A/B") != normalize_tax_id("AB")           # only case, spaces, hyphens and dots are ignored


# ------------------------------------------------------------------------------ amounts: what the model returned

@pytest.mark.parametrize("raw,expected", [
    ("2160.00", "2160.00"), ("1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("1,00,000.00", "100000.00"),
    ("12,34,567.89", "1234567.89"), ("1,00,000", "100000"), ("1.234.567", "1234567"), ("1 234,56", "1234.56"),
    ("₹1,00,000.00", "100000.00"), ("Rs. 1,16,000.00", "116000.00"), ("Rs.1,00,000/-", "100000"),
    ("$ 2,160.00 USD", "2160.00"), ("€ 1.234,56", "1234.56"), ("(500.00)", "-500.00"), ("-1,234.50", "-1234.50"),
    ("−1,234.50", "-1234.50"), ("500.00-", "-500.00"), ("12,50", "12.50"), ("0,123", "0.123"), ("1,234", "1234"),
    ("1.234", "1.234"), ("0.005", "0.005"), ("10.005", "10.005"), ("INR 5,000", "5000"), ("+7.5", "7.5"),
])
def test_normalize_amount_string(raw, expected):
    assert normalize_amount_string(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "12abc", "1,2,3,x", "--5", "$", "N/A", "1..2..3,"])
def test_unreadable_amounts_return_none_never_a_guess(raw):
    assert normalize_amount_string(raw) is None


def test_normalising_never_alters_digits():
    for raw in ("1,00,000.00", "9,87,65,432.10", "123,456,789.01", "0.10"):
        digits_in = "".join(c for c in raw if c.isdigit())
        digits_out = "".join(c for c in normalize_amount_string(raw) if c.isdigit())
        assert digits_in == digits_out


# ------------------------------------------------------------------------------ amounts: numbers found in a document

@pytest.mark.parametrize("token,expected", [
    ("1,234.56", {D("1234.56")}), ("1.234,56", {D("1234.56")}), ("1,00,000.00", {D("100000.00")}), ("1,00,000", {D("100000")}),
    ("12,34,567.89", {D("1234567.89")}), ("2160.00", {D("2160.00")}), ("7", {D("7")}), ("0,123", {D("0.123")}),
    ("12,50", {D("12.50")}), ("1,2345", {D("1.2345")}),
    ("1,234", {D("1234"), D("1.234")}), ("1.234", {D("1234"), D("1.234")}),       # genuinely ambiguous: both readings
    ("1.234.567", {D("1234567")}),
])
def test_amount_candidates_cover_every_plausible_reading(token, expected):
    assert amount_candidates(token) == expected


@pytest.mark.parametrize("token", ["", "abc", "1,2x", ",5", "5,", "1.2,3.4"])
def test_amount_candidates_reject_non_numbers(token):
    assert amount_candidates(token) == set()


def test_find_amounts_reads_a_table_row_without_merging_quantity_and_price():
    found = find_amounts("Qty 10 100.00 1,000.00")
    assert D("100.00") in found and D("1000.00") in found and D("10") in found


@pytest.mark.parametrize("text,value", [
    ("Total Due: $2,160.00", D("2160.00")), ("Grand Total Rs. 1,16,000.00", D("116000.00")),
    ("Gesamt 1.469,13 €", D("1469.13")), ("Amount: 1 234,56 EUR", D("1234.56")), ("(500.00)", D("500.00")),
    ("Rs.1,00,000/-", D("100000")), ("Balance 0.50", D("0.50")),
])
def test_find_amounts_locates_values_in_realistic_text(text, value):
    assert value in find_amounts(text)


def test_find_amounts_does_not_invent_values():
    assert D("999.99") not in find_amounts("Total Due: 1,105.00 on invoice 2026-0042")


# ------------------------------------------------------------------------------ dates

@pytest.mark.parametrize("text,expected", [
    ("14 Mar 2026", {date(2026, 3, 14)}), ("March 14, 2026", {date(2026, 3, 14)}), ("14th March 2026", {date(2026, 3, 14)}),
    ("2026-03-14", {date(2026, 3, 14)}), ("2026/03/14", {date(2026, 3, 14)}), ("14-Mar-26", {date(2026, 3, 14)}),
    ("14.03.2026", {date(2026, 3, 14)}), ("31/12/2026", {date(2026, 12, 31)}), ("13/04/2026", {date(2026, 4, 13)}),
    ("03/03/2026", {date(2026, 3, 3)}), ("Sept 5, 2026", {date(2026, 9, 5)}),
    ("03/04/2026", {date(2026, 3, 4), date(2026, 4, 3)}),
])
def test_date_candidates(text, expected):
    assert date_candidates(text) == expected


@pytest.mark.parametrize("text", ["31/02/2026", "not a date", "2026-13-45", "", "99/99/9999"])
def test_impossible_or_missing_dates_yield_no_candidates(text):
    assert date_candidates(text) == set()


@pytest.mark.parametrize("text,ambiguous", [
    ("03/04/2026", True), ("Date: 04-05-2026", True), ("13/04/2026", False), ("03/03/2026", False), ("14 Mar 2026", False),
    ("2026-03-04", False), ("03/04/2026 (4 March 2026)", False), ("no date", False),
])
def test_ambiguous_day_month_detection(text, ambiguous):
    assert is_ambiguous_dmy(text) is ambiguous


def test_model_returned_dates_are_normalised_only_when_unambiguous():
    assert normalize_date_string("14 Mar 2026") == "2026-03-14"
    assert normalize_date_string("03/04/2026") is None
    assert normalize_date_string("garbage") is None


# ------------------------------------------------------------------------------ currency

@pytest.mark.parametrize("raw,code", [
    ("$", "USD"), ("₹", "INR"), ("Rs", "INR"), ("Rs.", "INR"), ("RS.", "INR"), ("rs", "INR"), ("€", "EUR"),
    ("£", "GBP"), ("USD", "USD"), ("usd", "USD"), (" inr ", "INR"), ("EUR", "EUR"),
])
def test_currency_symbols_and_codes_map_to_iso(raw, code):
    assert map_currency(raw, SYMBOLS)[0] == code


def test_symbol_mappings_are_noted_but_plain_codes_are_not():
    assert "mapped to USD" in map_currency("$", SYMBOLS)[1]
    assert map_currency("USD", SYMBOLS)[1] is None


@pytest.mark.parametrize("raw", ["¥", "kr", "R$", "??", "dollars"])
def test_ambiguous_or_unknown_symbols_stay_unmapped_with_a_note(raw):
    code, note = map_currency(raw, SYMBOLS)
    assert code is None and "ambiguous or unsupported" in note


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_currency_is_missing_not_an_error(raw):
    assert map_currency(raw, SYMBOLS) == (None, None)


def test_the_symbol_map_comes_from_config():
    assert map_currency("¥", {**SYMBOLS, "¥": "JPY"})[0] == "JPY"
    assert map_currency("$", {})[0] is None

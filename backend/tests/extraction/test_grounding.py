"""Grounding: is each value supported by the document's own text? Confidence is capped, never raised."""
from decimal import Decimal

import pytest

from app.config import GroundingCaps, Settings
from app.enums import GroundingStatus as G
from app.extraction.grounding import alnum, ground_invoice, normalize_text
from app.models.extraction import ExtractedInvoice

D = Decimal
CFG = Settings(_env_file=None)


def fld(value, source, conf=0.95, page=1, **extra):
    return {"value": value, "page": page, "source_text": source, "confidence": conf, **extra}


def invoice(**parts) -> ExtractedInvoice:
    return ExtractedInvoice.model_validate(parts)


def ground(inv, texts, usable=True, settings=CFG):
    return ground_invoice(inv, texts, usable, settings)


PAGE = "ACME Corp\nInvoice No: INV-77\nDate: March 14, 2026\nSubtotal: 1,000.00\nTax: 80.00\nTotal Due: 1,080.00\nUSD\n"


# ----------------------------------------------------------------------------------------------- statuses

def test_a_verbatim_snippet_is_exact_and_leaves_confidence_alone():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", 0.93))
    res = ground(inv, {1: PAGE})
    assert inv.total.grounding is G.EXACT and inv.total.confidence == 0.93 and res.counts == {"exact": 1}


@pytest.mark.parametrize("snippet", ["total  due:   1,080.00", "TOTAL DUE: 1,080.00", "Total\nDue: 1,080.00"])
def test_case_and_whitespace_differences_are_normalized_not_penalised(snippet):
    inv = invoice(total=fld("1080.00", snippet))
    ground(inv, {1: PAGE})
    assert inv.total.grounding is G.NORMALIZED and inv.total.confidence == 0.95


def test_ligatures_unicode_minus_and_hyphenated_line_breaks_normalize():
    assert normalize_text("Balance: −500.00\nfoo-\nbar baz") == "balance: -500.00 foobar baz"
    assert normalize_text("ofﬁce") == "office" and normalize_text("a − b") == "a - b"
    assert normalize_text("in-\n  voice") == "invoice"
    inv = invoice(invoice_number=fld("INV-77", "Invoice No: INV‑77"))         # a non-breaking hyphen
    ground(inv, {1: PAGE})
    assert inv.invoice_number.grounding is G.NORMALIZED


def test_a_close_but_not_identical_snippet_whose_value_is_on_the_page_is_value_present():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.0O", 0.97))               # one OCR-style slip in the snippet
    ground(inv, {1: PAGE})
    assert inv.total.grounding is G.VALUE_PRESENT and inv.total.confidence == 0.85 and inv.total.model_confidence == 0.97


def test_a_close_snippet_with_a_value_that_is_not_on_the_page_is_fuzzy_and_capped():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", 0.97))
    ground(inv, {1: "ACME Corp\nTotal Due: 1,08O.00\n"})                            # the text layer itself is garbled
    assert inv.total.grounding is G.FUZZY and inv.total.confidence == 0.75 and inv.total.model_confidence == 0.97


def test_a_short_snippet_is_never_matched_fuzzily_even_with_a_lenient_threshold():
    lenient = Settings(_env_file=None, grounding=GroundingCaps(fuzzy_min_similarity=0.5))
    inv = invoice(vendor_name=fld("ACMEX", "ACMEX"))
    ground(inv, {1: "ACMEY Corp\n"}, settings=lenient)
    assert inv.vendor_name.grounding is G.NOT_FOUND                                # 5 characters prove nothing
    longer = invoice(vendor_name=fld("Northwind Traders", "Northwind Traders"))
    ground(longer, {1: "Northwind Tradexs Inc\n"}, settings=lenient)               # the text layer is slightly garbled
    assert longer.vendor_name.grounding is G.FUZZY                                 # a longer snippet may be approximate


def test_value_present_when_labels_are_separated_from_values():
    """The real-PDF layout: values in one block, labels in another, so the labelled snippet is not a substring."""
    text = "INVOICE\nMar 07 2013\n$5,141.76\n$196.32\n$5,338.08\nSubtotal:\nShipping:\nTotal:\n"
    inv = invoice(subtotal=fld("5141.76", "Subtotal: $5,141.76", 0.95), total=fld("5338.08", "Total: $5,338.08", 0.95),
                  invoice_date=fld("2013-03-07", "Date: Mar 07 2013", 0.9))
    ground(inv, {1: text})
    for name in ("subtotal", "total", "invoice_date"):
        f = getattr(inv, name)
        assert f.grounding is G.VALUE_PRESENT and f.confidence <= 0.85, name
    assert inv.total.confidence == 0.85 and inv.total.model_confidence == 0.95 and inv.invoice_date.confidence == 0.85


def test_value_present_cap_is_above_the_review_threshold_by_default():
    assert CFG.grounding.value_present > CFG.confidence_threshold


def test_value_present_for_identifiers_and_strings():
    text = "Order\nINV-2026-0042\nBill To:\nInvoice No:\nNorthwind Trading Co\n"
    inv = invoice(invoice_number=fld("INV-2026-0042", "Invoice No: INV-2026-0042"),
                  vendor_name=fld("Northwind Trading Co", "Vendor: Northwind Trading Co"))
    ground(inv, {1: text})
    assert inv.invoice_number.grounding is G.VALUE_PRESENT and inv.vendor_name.grounding is G.VALUE_PRESENT


@pytest.mark.parametrize("page_number,value", [("1.234,56", "1234.56"), ("1,234.56", "1234.56"), ("1,00,000.00", "100000.00"),
                                              ("12 345,67", "12345.67")])
def test_value_present_reads_us_european_and_indian_number_formats(page_number, value):
    inv = invoice(total=fld(value, f"Total: {page_number}"))
    ground(inv, {1: f"{page_number}\nTotal:\n"})
    assert inv.total.grounding is G.VALUE_PRESENT


def test_a_value_that_is_nowhere_on_the_page_is_not_found_and_capped():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", 0.99))
    res = ground(inv, {1: "Subtotal: 1,000.00\nThank you for your business\n"})
    assert inv.total.grounding is G.NOT_FOUND and inv.total.confidence == 0.40
    assert any("neither the source_text nor the value" in n and "total" in n for n in res.notes)


def test_a_snippet_that_nearly_matches_but_whose_number_is_not_on_the_page_stays_fuzzy_and_capped():
    """The model wrote 1,080.00 and cited a snippet that is one digit away from the page's 1,050.00."""
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", 0.99))
    res = ground(inv, {1: "Subtotal: 1,000.00\nTotal Due: 1,050.00\n"})
    assert inv.total.grounding is G.FUZZY and inv.total.confidence == 0.75
    assert any("approximately and its value was not found" in n for n in res.notes)


def test_a_fuzzy_snippet_whose_value_is_on_the_page_is_value_present():
    inv = invoice(vendor_name=fld("Northwind Trading Co", "Vendor: Northwind Trading Co"))
    ground(inv, {1: "Northwind Trading Co\n"})
    assert inv.vendor_name.grounding is G.VALUE_PRESENT and inv.vendor_name.confidence == 0.85


def test_a_value_that_disagrees_with_its_own_source_text_is_a_mismatch_even_if_the_snippet_is_on_the_page():
    inv = invoice(total=fld("1000.00", "Total Due: 1,080.00", 0.99))              # the model "repaired" the number
    res = ground(inv, {1: PAGE})
    assert inv.total.grounding is G.VALUE_MISMATCH and inv.total.confidence == 0.30
    assert any("does not agree with its own source_text" in n for n in res.notes)


def test_a_date_that_disagrees_with_its_snippet_is_a_mismatch_and_ambiguous_readings_both_count():
    bad = invoice(invoice_date=fld("2026-03-15", "Date: March 14, 2026"))
    ground(bad, {1: PAGE})
    assert bad.invoice_date.grounding is G.VALUE_MISMATCH
    ok = invoice(invoice_date=fld("2026-04-03", "Date: 03/04/2026"))               # 3 April read day-first
    ground(ok, {1: "Date: 03/04/2026"})
    assert ok.invoice_date.grounding is G.EXACT
    other = invoice(invoice_date=fld("2026-03-04", "Date: 03/04/2026"))            # or 4 March read month-first
    ground(other, {1: "Date: 03/04/2026"})
    assert other.invoice_date.grounding is G.EXACT


def test_a_value_with_no_source_text_is_capped_at_no_source():
    inv = invoice(total=fld("1080.00", None, 0.99), vendor_name=fld("ACME Corp", "  ", 0.9))
    ground(inv, {1: PAGE})
    assert inv.total.grounding is G.NO_SOURCE and inv.total.confidence == 0.50
    assert inv.vendor_name.grounding is G.NO_SOURCE and inv.vendor_name.confidence == 0.50


def test_null_fields_are_not_graded():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00"))
    res = ground(inv, {1: PAGE})
    assert inv.tax.grounding is None and inv.vendor_name.grounding is None and res.checked == 1


# ------------------------------------------------------------------------------------ never raises confidence

@pytest.mark.parametrize("source,page_text,status", [
    ("Total Due: 1,080.00", PAGE, G.EXACT), ("total due: 1,080.00", PAGE, G.NORMALIZED),
    ("Total Due: 1,080.00", "Total Due: 1,08O.00\n", G.FUZZY), ("Total: 1,080.00", "1,080.00\nTotal:\n", G.VALUE_PRESENT),
    ("Total: 1,080.00", "nothing here", G.NOT_FOUND), ("Total: 9,999.00", PAGE, G.VALUE_MISMATCH), (None, PAGE, G.NO_SOURCE)])
@pytest.mark.parametrize("conf", [0.0, 0.2, 0.45, 0.6, 0.8, 0.99, 1.0])
def test_grounding_never_raises_a_confidence(source, page_text, status, conf):
    inv = invoice(total=fld("1080.00", source, conf))
    ground(inv, {1: page_text})
    assert inv.total.grounding is status and inv.total.confidence <= conf
    assert inv.total.model_confidence == conf


def test_grounding_uses_the_current_confidence_not_the_raw_model_score():
    """A confidence already capped earlier (an ambiguous date) stays capped."""
    inv = invoice(invoice_date=fld("2026-03-14", "Date: March 14, 2026", 0.4, model_confidence=0.9))
    ground(inv, {1: PAGE})
    assert inv.invoice_date.confidence == 0.4 and inv.invoice_date.model_confidence == 0.9


# ------------------------------------------------------------------------------- pages and text availability

def test_a_wrong_page_number_is_repaired_and_noted():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", page=1))
    res = ground(inv, {1: "Page one\n", 2: PAGE})
    assert inv.total.page == 2 and inv.total.grounding is G.EXACT
    assert any("on page 2, not page 1" in n for n in res.notes)


def test_a_missing_page_number_is_filled_from_where_the_evidence_is():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", page=None))
    ground(inv, {1: "x", 2: PAGE})
    assert inv.total.page == 2


def test_value_present_on_another_page_also_repairs_the_page():
    inv = invoice(total=fld("1080.00", "Total: 1,080.00", page=1))
    ground(inv, {1: "cover letter", 2: "1,080.00\nTotal:\n"})
    assert inv.total.grounding is G.VALUE_PRESENT and inv.total.page == 2


def test_no_usable_text_layer_means_unavailable_without_penalty_but_g0_and_g1_still_apply():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", 0.9), invoice_number=fld("INV-1", None, 0.9),
                  subtotal=fld("1000.00", "Subtotal: 1,050.00", 0.9))
    ground(inv, {1: PAGE}, usable=False)
    assert inv.total.grounding is G.UNAVAILABLE and inv.total.confidence == 0.9
    assert inv.invoice_number.grounding is G.NO_SOURCE and inv.invoice_number.confidence == 0.5
    assert inv.subtotal.grounding is G.VALUE_MISMATCH and inv.subtotal.confidence == 0.3


def test_a_page_without_text_is_unavailable_not_not_found():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00", page=2))
    ground(inv, {1: PAGE, 2: None})                                                 # page 2 is a scan
    assert inv.total.grounding is G.UNAVAILABLE and inv.total.confidence == 0.95


def test_no_pages_at_all_is_unavailable():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00"))
    assert ground(inv, {}).counts == {"unavailable": 1}


# --------------------------------------------------------------------------------- currency and other kinds

def test_currency_agrees_with_a_symbol_or_a_code_in_its_snippet():
    by_symbol = invoice(currency=fld("USD", "$5,338.08"))
    ground(by_symbol, {1: "$5,338.08\n"})
    by_code = invoice(currency=fld("USD", "Amounts in USD"))
    ground(by_code, {1: "Amounts in USD\n"})
    wrong = invoice(currency=fld("EUR", "$5,338.08"))
    ground(wrong, {1: "$5,338.08\n"})
    assert by_symbol.currency.grounding is G.EXACT and by_code.currency.grounding is G.EXACT
    assert wrong.currency.grounding is G.VALUE_MISMATCH


def test_currency_value_present_when_only_the_symbol_is_elsewhere_on_the_page():
    inv = invoice(currency=fld("EUR", "Currency: EUR"))
    ground(inv, {1: "€ 100,00\nCurrency\n"})
    assert inv.currency.grounding is G.VALUE_PRESENT


def test_a_negative_total_matches_its_bracketed_or_signed_printing():
    for printed in ("(500.00)", "-500.00", "−500.00"):
        inv = invoice(total=fld("-500.00", f"Total: {printed}"))
        ground(inv, {1: f"Total: {printed}\n"})
        assert inv.total.grounding in (G.EXACT, G.NORMALIZED), printed


def test_document_type_is_not_checked_against_text_it_did_not_copy():
    inv = invoice(document_type=fld("credit_note", "CREDIT NOTE"))
    ground(inv, {1: "CREDIT NOTE\n"})
    assert inv.document_type.grounding is G.EXACT
    other = invoice(document_type=fld("proforma", "Pro-forma"))
    ground(other, {1: "Pro-forma invoice\n"})
    assert other.document_type.grounding is G.EXACT


def test_tax_ids_compare_ignoring_separators_and_case():
    assert alnum("GB 123-456.789") == alnum("gb123456789")
    inv = invoice(vendor_tax_id=fld("GB123456789", "VAT: GB 123 456 789"))
    ground(inv, {1: "VAT: GB 123 456 789\n"})
    assert inv.vendor_tax_id.grounding is G.EXACT


# ----------------------------------------------------------------------------------------- lines, adjustments

LINE = {"description": "Widget A", "quantity": "10", "unit_price": "60.00", "amount": "600.00", "page": 1,
        "source_text": "Widget A 10 60.00 600.00", "confidence": 0.95}


def test_line_items_are_grounded_exact_value_present_and_mismatch():
    text = "Widget A 10 60.00 600.00\nGadget\n400.00\n80.00\n5\n"
    inv = invoice(line_items=[
        LINE,
        {**LINE, "description": "Gadget", "quantity": "5", "unit_price": "80.00", "amount": "400.00", "source_text": "Gadget 5 80.00 400.00"},
        {**LINE, "amount": "650.00"}])                                             # the model "fixed" the amount
    res = ground(inv, {1: text})
    assert [i.grounding for i in inv.line_items] == [G.EXACT, G.VALUE_PRESENT, G.VALUE_MISMATCH]
    assert [i.confidence for i in inv.line_items] == [0.95, 0.85, 0.30] and res.checked == 3


def test_a_line_with_a_missing_source_or_missing_numbers_on_the_page():
    inv = invoice(line_items=[{**LINE, "source_text": None}, {**LINE, "amount": "601.00", "source_text": "Widget A 10 60.00 601.00"}])
    ground(inv, {1: "Widget A 10 60.00 600.00"})
    assert [i.grounding for i in inv.line_items] == [G.NO_SOURCE, G.FUZZY]          # a digit away from the page: approximate
    assert [i.confidence for i in inv.line_items] == [0.5, 0.75]
    far = invoice(line_items=[{**LINE, "quantity": "3", "unit_price": "12.00", "amount": "777.00",
                               "source_text": "Sprocket 3 12.00 777.00"}])
    ground(far, {1: "Widget A 10 60.00 600.00"})
    assert far.line_items[0].grounding is G.NOT_FOUND and far.line_items[0].confidence == 0.4


def test_adjustments_are_grounded_against_the_printed_magnitude_not_the_signed_amount():
    adj = {"kind": "discount", "description": "Discount (10%)", "amount": "184.59", "page": 1,
           "source_text": "Discount (10%): $184.59", "confidence": 0.93}
    from app.extraction.postprocess import postprocess
    inv = postprocess({"adjustments": [adj, {**adj, "kind": "shipping", "amount": "109.26", "source_text": "Shipping: $109.26"}]}).invoice
    assert inv.adjustments[0].amount == D("-184.59")
    ground(inv, {1: "Subtotal:\nDiscount (10%):\nShipping:\n$184.59\n$109.26\n"})
    assert [a.grounding for a in inv.adjustments] == [G.VALUE_PRESENT, G.VALUE_PRESENT]
    assert [a.confidence for a in inv.adjustments] == [0.85, 0.85]
    assert inv.adjustments[0].amount == D("-184.59")                               # the sign is untouched


def test_an_adjustment_that_disagrees_with_its_source_is_a_mismatch():
    inv = invoice(adjustments=[{"kind": "shipping", "amount": "25.00", "page": 1, "source_text": "Shipping: 52.00", "confidence": 0.9}])
    ground(inv, {1: "Shipping: 52.00"})
    assert inv.adjustments[0].grounding is G.VALUE_MISMATCH and inv.adjustments[0].confidence == 0.3


# ---------------------------------------------------------------------------------------- summary and config

def test_a_summary_and_the_problems_are_appended_to_the_extraction_notes():
    inv = invoice(total=fld("1080.00", "Total Due: 1,080.00"), subtotal=fld("1000.00", "Subtotal: 999.00"),
                  extraction_notes="model note")
    res = ground(inv, {1: PAGE})
    assert res.notes[0] == "grounding: 2 item(s) checked: 1 exact, 1 value_mismatch"
    assert inv.extraction_notes.startswith("model note\n[system] grounding: 2 item(s) checked")
    assert "[system] grounding: subtotal does not agree" in inv.extraction_notes


def test_nothing_to_check_leaves_the_notes_alone():
    inv = invoice()
    res = ground(inv, {1: PAGE})
    assert res.checked == 0 and res.counts == {} and inv.extraction_notes is None


def test_caps_come_from_config():
    cfg = Settings(_env_file=None, grounding=GroundingCaps(value_present=0.70, not_found=0.10, no_source=0.2, value_mismatch=0.05))
    inv = invoice(subtotal=fld("1000.00", "Subtotal: 1,000.00"), total=fld("1080.00", "Total: 1,080.00"),
                  invoice_number=fld("INV-1", None), tax=fld("80.00", "Tax: 81.00"))
    ground(inv, {1: "1,000.00\nSubtotal:\n"}, settings=cfg)
    assert (inv.subtotal.confidence, inv.total.confidence, inv.invoice_number.confidence, inv.tax.confidence) == (0.70, 0.10, 0.2, 0.05)


def test_the_model_confidence_is_recorded_if_the_caller_did_not():
    inv = invoice(total=fld("1080.00", "Total: 1,080.00", 0.92))
    assert inv.total.model_confidence is None
    ground(inv, {1: "nope"})
    assert inv.total.model_confidence == 0.92 and inv.total.confidence == 0.40

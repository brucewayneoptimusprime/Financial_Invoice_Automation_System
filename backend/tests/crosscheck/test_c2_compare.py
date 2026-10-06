"""Cross-check stage C2: relevance and differences, computed by code from a document's facts and a purchase order. No model
judgement anywhere: the documents are crafted, read through the real reader with a scripted double, then compared."""
from decimal import Decimal

import pytest

from app.config import Settings
from app.crosscheck.compare import compare_document, tie_lines
from app.crosscheck.facts import Invoiced, POContext, POLine, load_po_context, open_readonly
from app.crosscheck.prompts import document_parts
from app.crosscheck.reader import read_document
from app.extraction.prompts import PagePayload
from tests.crosscheck.helpers import client_for, field, note
from tests.pipeline.helpers import demo_db

D = Decimal
CFG = Settings(_env_file=None)
LINES = (POLine(11, 1, "Widget A", D("10"), D("60.00"), 60000), POLine(12, 2, "Widget B", D("5"), D("80.00"), 40000))
TONER = POLine(13, 3, "Toner cartridge, black", D("2"), D("45.50"), 9100)


def po(lines=LINES, invoiced=None, invoices=(), **kw):
    base = dict(po_id=7, po_number="PO-7001", currency="USD", total_minor=100000, vendor_name="Northwind Trading Co.",
                vendor_aliases=("Northwind",), lines=tuple(lines), invoices=tuple(invoices), invoiced=invoiced or {})
    return POContext(**{**base, **kw})


def report(purchase_order=None, *, usable=True, **changes):
    text, reply = note(**changes)
    facts = read_document(document_parts([PagePayload(number=1, text=text)], 1), {1: text if usable else ""}, usable,
                          client=client_for(reply), settings=CFG, run_key="crosscheck-test-1")
    assert facts.status == "ok"
    return compare_document(facts, purchase_order or po(), CFG)


def kinds(r):
    return [d["type"] for d in r["differences"]]


def signals(r):
    return {s["signal"]: s["holds"] for s in r["relevance"]["signals"]}


# ---------------------------------------------------------------------------------------------- a matching document

def test_a_document_that_agrees_with_the_po_is_related_with_no_differences():
    r = report()
    assert r["relevance"]["related"] and not r["relevance"]["vendor_only"]
    assert signals(r) == {"po_number": True, "invoice_number": False, "vendor": True, "lines": True}
    assert r["differences"] == [] and r["absent_po_lines"] == [] and r["unconfirmed"] == [] and r["notices"] == []
    assert [l["tie"]["status"] for l in r["facts"]["lines"]] == ["tied", "tied"]
    assert [x["what"] for x in r["not_compared"]] == ["Quantity on PO line 1 against invoices", "Quantity on PO line 2 against invoices"]
    assert {x["reason"] for x in r["not_compared"]} == {"no invoice is matched to this PO"}
    assert not any(k in r for k in ("severity", "decision", "verdict", "recommendation", "score", "passed"))


# ------------------------------------------------------------------------------------------ each difference, alone

def test_1_quantity_differs_from_the_po_line_and_the_amount_is_not_reported_twice():
    r = report(a=("Widget A", "8", "60.00", "480.00"), total="880.00")
    (d,) = r["differences"]
    assert d["type"] == "quantity_vs_po" and d["po_line_no"] == 1 and d["po_line_description"] == "Widget A"
    assert d["document"] == {"value": "8", "page": 1, "source_text": "Widget A 8 pcs 60.00 480.00"}
    assert d["compared_with"] == {"value": "10", "source": "PO line 1, ordered quantity"}


def test_2_quantity_differs_from_what_was_invoiced_and_each_invoice_is_named_with_its_status():
    invoiced = {11: (Invoiced("INV-7", "approved", D("6"), "allocated"), Invoiced("INV-9", "in_review", D("2.0"), "line_match")),
                12: (Invoiced("INV-7", "approved", D("5"), "allocated"),)}
    r = report(po(invoiced=invoiced, invoices=(("INV-7", "approved"), ("INV-9", "in_review"))))
    (d,) = r["differences"]
    assert d["type"] == "quantity_vs_invoiced" and d["document"]["value"] == "10" and d["po_line_no"] == 1
    assert d["compared_with"] == {"value": "8", "source": "Invoiced on PO line 1: INV-7 (approved): 6, INV-9 (in review): 2"}
    assert r["not_compared"] == []


def test_2b_a_po_line_no_invoice_is_tied_to_is_listed_as_not_compared_and_never_as_zero():
    r = report(po(invoiced={11: (Invoiced("INV-7", "approved", D("10"), "allocated"),)}, invoices=(("INV-7", "approved"),)))
    assert r["differences"] == []
    assert r["not_compared"] == [{"what": "Quantity on PO line 2 against invoices", "reason": "no invoice quantity is tied to this PO line"}]


def test_3_unit_price_differs_from_the_po_line():
    r = report(a=("Widget A", "10", "62.50", ""), total="")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"], d["compared_with"]) == ("unit_price", "62.50", {"value": "60.00", "source": "PO line 1, unit price"})


def test_4_amount_differs_from_the_po_line_amount():
    r = report(a=("Widget A", "10", "60.00", "610.00"), total="")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"], d["compared_with"]) == ("amount", "610.00", {"value": "600.00", "source": "PO line 1, amount"})


def test_4b_with_a_different_quantity_the_amount_is_compared_with_quantity_times_the_po_price():
    r = report(a=("Widget A", "8", "", "500.00"), total="")
    assert kinds(r) == ["quantity_vs_po", "amount"]
    assert r["differences"][1]["compared_with"] == {"value": "480.00", "source": "Expected for 8 at the PO price 60.00 (PO line 1)"}
    assert r["differences"][1]["document"]["value"] == "500.00"


def test_5_vendor_name_differs():
    r = report(vendor="Acme Trading")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"]) == ("vendor_name", "Acme Trading")
    assert d["compared_with"] == {"value": "Northwind Trading Co.", "source": "This PO's vendor"}
    assert r["relevance"]["related"] and not signals(r)["vendor"]


def test_6_the_document_names_another_po():
    r = report(po="PO-2001")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"], d["document"]["source_text"]) == ("po_number", "PO-2001", "Purchase Order: PO-2001")
    assert d["compared_with"] == {"value": "PO-7001", "source": "This PO's number"} and not signals(r)["po_number"]


def test_7_currency_differs_and_no_money_is_compared():
    r = report(currency="EUR", a=("Widget A", "10", "99.00", "990.00"), total="1,390.00")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"], d["compared_with"]["value"]) == ("currency", "EUR", "USD")
    assert r["not_compared"][0] == {"what": "Unit prices, amounts and the total",
                                    "reason": "the document is in EUR and the PO in USD; nothing is converted"}


def test_8_an_item_that_is_not_on_the_po():
    r = report(extra=("Laminating pouches A4", "3", "12.00", "36.00"), total="")
    (d,) = r["differences"]
    assert d["type"] == "item_not_on_po" and d["po_line_no"] is None
    assert d["document"]["value"] == "Laminating pouches A4, quantity 3, unit price 12.00, amount 36.00"
    assert d["compared_with"] == {"value": "No PO line has a similar description", "source": "This PO's 2 line(s)"}


def test_9_the_document_total_differs_from_what_the_po_says_its_lines_cost():
    r = report(total="1,050.00")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"], d["document"]["source_text"]) == ("total", "1050.00", "Total: 1,050.00")
    assert d["compared_with"]["value"] == "1000.00" and "expected amounts" in d["compared_with"]["source"]


def test_9b_the_total_is_not_compared_when_a_line_is_not_on_the_po_and_the_reason_is_given():
    r = report(extra=("Laminating pouches A4", "3", "12.00", "36.00"), total="1,036.00")
    assert kinds(r) == ["item_not_on_po"]
    assert {"what": "Document total", "reason": "not every line of the document is tied to a PO line"} in r["not_compared"]


def test_10_po_lines_absent_from_the_document_are_informational_and_never_a_difference():
    r = report(po(lines=(*LINES, TONER)))
    assert r["differences"] == []
    assert r["absent_po_lines"] == [{"po_line_no": 3, "description": "Toner cartridge, black", "quantity": "2"}]


def test_a_partial_delivery_note_with_no_prices_reports_only_its_quantities():
    r = report(a=("Widget A", "4", "", ""), b=None, total="", currency="")
    assert kinds(r) == ["quantity_vs_po"] and [x["po_line_no"] for x in r["absent_po_lines"]] == [2]


def test_two_document_lines_for_one_po_line_are_added_together():
    r = report(a=("Widget A", "6", "60.00", "360.00"), extra=("Widget A", "4", "60.00", "240.00"))
    assert r["differences"] == []
    r = report(a=("Widget A", "6", "60.00", "360.00"), extra=("Widget A", "3", "60.00", "180.00"), total="")
    (d,) = r["differences"]
    assert (d["type"], d["document"]["value"]) == ("quantity_vs_po", "9")
    assert d["document"]["source_text"] == "Widget A 6 pcs 60.00 360.00 | Widget A 3 pcs 60.00 180.00"


def test_money_is_compared_in_integer_cents_and_quantities_as_decimals():
    exact = po(lines=(POLine(11, 1, "Widget A", D("4.0"), D("461.48"), 184594), LINES[1]))       # 4 x 461.48 = 1845.92, PO says 1845.94
    assert report(exact, a=("Widget A", "4", "461.48", "1845.94"), total="2,245.94")["differences"] == []
    assert kinds(report(exact, a=("Widget A", "4", "461.48", "1845.92"), total="")) == ["amount"]
    odd = report(exact, a=("Widget A", "0.333", "", "153.67"), total="")                          # 0.333 x 461.48 is not whole cents
    assert kinds(odd) == ["quantity_vs_po"]
    assert {"what": "Amount on PO line 1", "reason": "the PO line has no amount, and quantity x PO price is not exact to the cent"} in odd["not_compared"]


# ------------------------------------------------------------------------------------------------------ relevance

def test_an_unrelated_document_is_not_related_gives_four_reasons_and_no_differences():
    r = report(vendor="Acme Trading", po="PO-2001", a=("Laminating pouches A4", "3", "12.00", "36.00"),
               b=("Stapler, heavy duty", "1", "25.00", "25.00"), total="61.00")
    assert not r["relevance"]["related"] and signals(r) == {"po_number": False, "invoice_number": False, "vendor": False, "lines": False}
    assert all(s["explanation"] for s in r["relevance"]["signals"]) and len(r["relevance"]["signals"]) == 4
    assert r["differences"] == [] and r["absent_po_lines"] == [] and r["not_compared"] == []
    assert len(r["facts"]["lines"]) == 2                                                           # its facts are still shown


def test_a_document_related_by_vendor_only_is_labelled_and_shows_the_po_number_difference_first():
    r = report(po="PO-2001", a=("Laminating pouches A4", "3", "12.00", "36.00"), b=None, total="")
    assert r["relevance"]["related"] and r["relevance"]["vendor_only"]
    assert kinds(r) == ["po_number", "item_not_on_po"]


def test_each_signal_alone_makes_a_document_related():
    by_invoice = report(po(invoices=(("INV-0007", "approved"),)), vendor="Acme Trading", po="", invoice="inv 7",
                        a=("Laminating pouches A4", "3", "12.00", "36.00"), b=None, total="")
    assert signals(by_invoice) == {"po_number": False, "invoice_number": True, "vendor": False, "lines": False}
    assert by_invoice["relevance"]["related"] and not by_invoice["relevance"]["vendor_only"]
    by_lines = report(vendor="Acme Trading", po="")
    assert signals(by_lines) == {"po_number": False, "invoice_number": False, "vendor": False, "lines": True}
    by_number = report(vendor="Acme Trading", po="po 7001", a=("Laminating pouches A4", "3", "12.00", "36.00"), b=None, total="")
    assert signals(by_number)["po_number"] and by_number["relevance"]["related"]
    alias = report(vendor="NORTHWIND", po="")
    assert signals(alias)["vendor"] and "other name" in alias["relevance"]["signals"][2]["explanation"]


def test_half_of_the_lines_is_enough_and_less_is_not():
    half = report(vendor="Acme Trading", po="", b=("Stapler, heavy duty", "1", "25.00", "25.00"), total="")
    assert signals(half)["lines"] and half["relevance"]["signals"][3]["document_value"] == "1 of 2"
    third = report(vendor="Acme Trading", po="", b=("Stapler, heavy duty", "1", "25.00", "25.00"),
                   extra=("Laminating pouches A4", "3", "12.00", "36.00"), total="")
    assert not signals(third)["lines"] and not third["relevance"]["related"]


# ---------------------------------------------------------------------------------------------------- line ties

def test_ties_use_the_description_only_and_an_unclear_line_is_ambiguous_not_guessed():
    twins = po(lines=(POLine(11, 1, "Office chair, mesh back, black", D("2"), D("100"), 20000),
                      POLine(12, 2, "Office chair, mesh back, grey", D("2"), D("100"), 20000)))
    line = {"description": "Office chair, mesh back", "item_code": None, "quantity": "99", "unit_price": "1.00", "amount": None, "confirmed": True}
    (tie,) = tie_lines([line], twins, CFG)
    assert tie["status"] == "ambiguous" and [c["po_line_no"] for c in tie["candidates"]] == [1, 2]
    exact = dict(line, description="Office chair, mesh back, grey")
    assert tie_lines([exact], twins, CFG)[0]["po_line_no"] == 2                # an exact description wins over a near one
    far_off = dict(line, description="Widget A", quantity="1000000", unit_price="0.01")
    assert tie_lines([far_off], po(), CFG)[0]["po_line_no"] == 1               # quantity and price play no part in the tie
    r = report(twins, a=("Office chair, mesh back", "2", "100.00", "200.00"), b=None, total="")
    assert r["differences"] == [] and "possible: line 1, line 2" in r["not_compared"][0]["reason"]


# ------------------------------------------------------------------------------------ unconfirmed values and scans

def test_a_value_the_text_does_not_support_is_listed_as_unconfirmed_and_not_compared():
    text, reply = note()
    reply["fields"][1] = field("vendor_name", "Acme Trading", "Acme Trading")                      # not on the page
    reply["lines"][0]["quantity"] = "3"                                                            # the page says 10
    facts = read_document(document_parts([PagePayload(number=1, text=text)], 1), {1: text}, True, client=client_for(reply),
                          settings=CFG, run_key="k")
    r = compare_document(facts, po(), CFG)
    assert [(u["what"], u["grounding"]) for u in r["unconfirmed"]] == [("vendor_name", "not_found"), ("line 1", "value_mismatch")]
    assert r["differences"] == [] and not signals(r)["vendor"]                                     # no vendor or quantity difference
    assert r["facts"]["lines"][0]["tie"]["status"] == "unconfirmed" and [x["po_line_no"] for x in r["absent_po_lines"]] == [1]


def test_a_scan_is_compared_and_marked_as_read_from_the_image():
    r = report(usable=False, a=("Widget A", "8", "60.00", "480.00"), total="880.00")
    assert kinds(r) == ["quantity_vs_po"] and r["unconfirmed"] == []
    assert r["notices"] == ["Read from the image: this document has no text layer, so its values could not be checked against text."]


def test_a_hostile_document_gets_a_notice_and_the_same_differences_as_without_it():
    plain = report(a=("Widget A", "8", "60.00", "480.00"), total="880.00")
    hostile = report(a=("Widget A", "8", "60.00", "480.00"), total="880.00", hostile=True)
    assert hostile["differences"] == plain["differences"] and hostile["relevance"] == plain["relevance"]
    assert hostile["notices"] == ["This document contains text addressed to an AI reader. It was treated as data and changed nothing."]


# ------------------------------------------------------------------------------------------- facts from the database

def test_the_po_context_is_read_from_a_read_only_connection_that_cannot_write(tmp_path):
    import sqlite3

    demo_db(tmp_path).close()
    with pytest.raises(sqlite3.OperationalError):
        conn = open_readonly(tmp_path / "app.db")
        try:
            conn.execute("UPDATE purchase_orders SET total_amount = 0")
        finally:
            conn.close()
    conn = open_readonly(tmp_path / "app.db")
    try:
        ctx = load_po_context(conn, 1)
        assert load_po_context(conn, 9999) is None
    finally:
        conn.close()
    assert (ctx.po_number, ctx.vendor_name, ctx.vendor_aliases, ctx.currency, ctx.total_minor) == ("PO-SS-001", "SuperStore", ("Super Store",), "USD", 600000)
    (line,) = ctx.lines
    assert (line.line_no, line.quantity, line.unit_price, line.amount_minor) == (1, D("4.0"), D("1285.44"), 514176)

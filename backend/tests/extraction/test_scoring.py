"""Scoring an extraction against a verified answer key."""
import pytest

from app.extraction.manifest import normalise_expected
from app.extraction.postprocess import postprocess
from app.extraction.scoring import Totals, score_file


def fld(value, conf=0.9, model=None):
    return {"value": value, "page": 1, "source_text": str(value), "confidence": conf, **({"model_confidence": model} if model else {})}


def invoice(**parts):
    return postprocess(parts).invoice


def expected(**raw):
    return normalise_expected({"verified": True, **raw})[0]


def verdicts(score):
    return {(j.field, j.verdict) for j in score.judgements}


def test_every_verdict():
    inv = invoice(vendor_name=fld("Acme Ltd"), invoice_number=fld("INV-1"), total=fld("100.00"), po_reference=fld("PO-9"))
    exp = expected(vendor_name="Acme Ltd", invoice_number="INV-2", total="100", tax=None, invoice_date="2026-01-01", po_reference=None,
                   subtotal=None)
    assert verdicts(score_file("f", exp, inv)) == {
        ("vendor_name", "correct"), ("invoice_number", "wrong"), ("total", "correct"), ("tax", "correct"),
        ("invoice_date", "missed"), ("po_reference", "hallucinated"), ("subtotal", "correct")}


def test_omitted_fields_are_not_scored_but_explicit_null_is():
    inv = invoice(vendor_name=fld("Acme"), vendor_tax_id=fld("GB123"))
    score = score_file("f", expected(vendor_name="Acme"), inv)
    assert [j.field for j in score.judgements] == ["vendor_name"]                         # the hallucinated tax id is not asked about
    assert verdicts(score_file("f", expected(vendor_name="Acme", vendor_tax_id=None), inv)) == {("vendor_name", "correct"), ("vendor_tax_id", "hallucinated")}


@pytest.mark.parametrize("field,want,got,ok", [
    ("total", "1080", "1080.00", True), ("total", "1,080.00", "1080.0", True), ("total", "1080.01", "1080.00", False),
    ("total", "-500", "-500.00", True), ("subtotal", "0", "0.00", True),
    ("vendor_name", "ACME  Supplies, Ltd.", "acme supplies ltd", True), ("vendor_name", "Acme", "Acme Ltd", False),
    ("invoice_number", "inv-1001", "INV 1001", True), ("invoice_number", "INV-1001", "INV-1002", False),
    ("vendor_tax_id", "GB 123-456.789", "gb123456789", True), ("vendor_tax_id", "GB123456789", "GB123456780", False),
    ("po_reference", "PO-5001", "po 5001", True)])
def test_normalised_comparisons(field, want, got, ok):
    value = fld(got)
    inv = invoice(**{field: value})
    (j,) = score_file("f", expected(**{field: want}), inv).judgements
    assert (j.verdict == "correct") is ok, (want, got)


def test_dates_and_currency_and_document_type():
    inv = invoice(invoice_date=fld("2026-03-14"), currency=fld("$"), document_type=fld("invoice"))
    assert score_file("f", expected(invoice_date="2026-03-14", currency="usd", document_type="Invoice"), inv).all_correct
    assert not score_file("f", expected(invoice_date="2026-03-15"), inv).all_correct
    assert not score_file("f", expected(currency="EUR"), inv).all_correct


def test_line_items_match_by_exact_amount_and_description():
    inv = invoice(line_items=[{"description": "Hewlett Fax Machine, Color; Copiers, TEC-CO-4575", "quantity": "4", "unit_price": "1285.44",
                               "amount": "5141.76", "confidence": 0.9}])
    ok = score_file("f", expected(line_items=[{"description": "Hewlett Fax Machine, Color", "amount": "5141.76"}]), inv)
    assert verdicts(ok) == {("line_items", "correct")}                                   # a longer extracted description still contains it
    assert verdicts(score_file("f", expected(line_items=[{"description": "Fax", "amount": "5141.77"}]), inv)) == {
        ("line_items", "missed"), ("line_items", "hallucinated")}                         # wrong amount: missed AND an extra line
    assert verdicts(score_file("f", expected(line_items=[{"description": "Stapler", "amount": "5141.76"}]), inv)) == {
        ("line_items", "missed"), ("line_items", "hallucinated")}
    assert score_file("f", expected(line_items=[{"amount": "5141.76"}]), inv).all_correct          # no description in the key: amount only
    assert score_file("f", expected(line_items=[{"amount": "5141.76", "quantity": "4", "unit_price": "1285.44"}]), inv).all_correct
    assert not score_file("f", expected(line_items=[{"amount": "5141.76", "quantity": "5"}]), inv).all_correct


def test_extra_and_missing_lines_and_an_empty_expectation():
    inv = invoice(line_items=[{"description": "A", "amount": "1.00", "confidence": 0.9}, {"description": "B", "amount": "2.00", "confidence": 0.6}])
    s = score_file("f", expected(line_items=[{"amount": "1.00"}, {"amount": "3.00"}]), inv)
    assert sorted((j.verdict) for j in s.judgements) == ["correct", "hallucinated", "missed"]
    assert [j.verdict for j in score_file("f", expected(line_items=[]), inv).judgements] == ["hallucinated", "hallucinated"]
    assert score_file("f", expected(line_items=[]), invoice()).judgements == []


def test_duplicate_lines_are_matched_one_to_one():
    inv = invoice(line_items=[{"amount": "5.00", "confidence": 0.9}])
    s = score_file("f", expected(line_items=[{"amount": "5.00"}, {"amount": "5.00"}]), inv)
    assert sorted(j.verdict for j in s.judgements) == ["correct", "missed"]


def test_adjustments_match_by_kind_and_signed_amount():
    inv = invoice(adjustments=[{"kind": "discount", "amount": "184.59", "confidence": 0.9}, {"kind": "shipping", "amount": "109.26", "confidence": 0.9}])
    good = expected(adjustments=[{"kind": "discount", "amount": "-184.59"}, {"kind": "shipping", "amount": "109.26"}])
    assert score_file("f", good, inv).all_correct
    unsigned = expected(adjustments=[{"kind": "discount", "amount": "184.59"}, {"kind": "shipping", "amount": "109.26"}])
    assert not score_file("f", unsigned, inv).all_correct                                # the key says the discount is negative
    wrong_kind = expected(adjustments=[{"kind": "fee", "amount": "109.26"}, {"kind": "discount", "amount": "-184.59"}])
    assert verdicts(score_file("f", wrong_kind, inv)) == {("adjustments", "correct"), ("adjustments", "missed"), ("adjustments", "hallucinated")}


def test_confidence_is_recorded_for_calibration():
    inv = invoice(total=fld("100.00", 0.9, model=0.95), vendor_name=fld("Wrong Co", 0.7, model=0.8))
    s = score_file("f", expected(total="100.00", vendor_name="Right Co", tax=None), inv)
    by = {j.field: j for j in s.judgements}
    assert (by["total"].confidence, by["total"].model_confidence) == (0.9, 0.95)
    assert by["tax"].confidence is None                                                 # nothing extracted: no confidence to calibrate
    t = Totals()
    t.add(s)
    assert t.conf_correct == [0.9] and t.conf_wrong == [0.7] and t.model_conf_correct == [0.95] and t.model_conf_wrong == [0.8]


def test_totals_count_every_judgement_and_accuracy():
    t = Totals()
    t.add(score_file("a", expected(total="1", vendor_name="X"), invoice(total=fld("1"), vendor_name=fld("Y"))))
    t.add(score_file("b", expected(total="2", vendor_name="X"), invoice(total=fld("3"), vendor_name=fld("X"))))
    total, name = t.fields["total"], t.fields["vendor_name"]
    assert (total.scored, total.correct, total.wrong, total.accuracy) == (2, 1, 1, 0.5)
    assert (name.scored, name.correct, name.wrong, name.accuracy) == (2, 1, 1, 0.5)
    assert t.files == 2 and t.files_all_correct == 0


def test_an_extra_extracted_line_lowers_line_accuracy():
    t = Totals()
    t.add(score_file("a", expected(line_items=[{"amount": "1.00"}]), invoice(line_items=[{"amount": "1.00", "confidence": 0.9},
                                                                                         {"amount": "9.00", "confidence": 0.9}])))
    s = t.fields["line_items"]
    assert (s.scored, s.correct, s.hallucinated, s.accuracy) == (2, 1, 1, 0.5)

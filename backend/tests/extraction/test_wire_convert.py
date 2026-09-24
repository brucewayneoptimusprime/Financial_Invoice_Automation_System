"""wire -> contract conversion: found=false / placeholders become null, yes/no/unknown become booleans, and the
internal contract itself is unchanged."""
import copy
import logging
from decimal import Decimal

import pytest

from app.extraction.postprocess import postprocess
from app.extraction.wire import EVIDENCED_FIELDS, from_wire
from app.models import ExtractedInvoice
from tests.extraction.helpers import load_reply
from tests.extraction.wire_convert import to_wire

D = Decimal


def blank():
    """A wire reply for an empty document: everything found=false with placeholders."""
    return to_wire({})


def wire(**patch):
    reply = load_reply("us_native_invoice")
    for key, value in patch.items():
        reply[key] = {**reply[key], **value} if isinstance(value, dict) else value
    return reply


def convert(reply):
    contract, notes = from_wire(reply)
    return contract, notes


# ------------------------------------------------------------------------------ not found -> null

def test_a_blank_reply_converts_to_an_all_null_invoice():
    contract, notes = convert(blank())
    assert notes == []
    inv = ExtractedInvoice.model_validate(contract)
    for name in EVIDENCED_FIELDS:
        f = getattr(inv, name)
        assert f.value is None and f.page is None and f.source_text is None and f.confidence == 0.0, name
    assert inv.line_items == [] and inv.adjustments == [] and inv.extraction_notes is None
    assert inv.document_quality.type is None and inv.document_quality.contains_reader_instructions is None
    assert inv.po_reference.explicit is None and inv.tax.included_in_total is None


def test_the_blank_reply_goes_through_postprocess_cleanly():
    contract, notes = convert(blank())
    r = postprocess(contract, None, notes)
    assert r.invoice.total.value is None and r.notes == []


def test_found_false_ignores_whatever_the_placeholders_contain():
    reply = wire(total={"found": False, "value": "1105.00", "page": 3, "source_text": "junk", "confidence": 0.9})
    contract, _ = convert(reply)
    assert contract["total"] == {"value": None, "page": None, "source_text": None, "confidence": 0.0}


def test_found_true_with_an_empty_value_is_treated_as_not_found_with_a_note():
    for empty in ("", "   "):
        contract, notes = convert(wire(invoice_number={"value": empty}))
        assert contract["invoice_number"]["value"] is None and contract["invoice_number"]["confidence"] == 0.0
        assert notes == ["invoice_number: found=true but the value was empty; treated as not found"]


def test_empty_value_notes_reach_the_invoice_notes_through_postprocess():
    contract, notes = convert(wire(total={"value": ""}))
    inv = postprocess(contract, None, notes).invoice
    assert inv.total.value is None and "[system] total: found=true but the value was empty" in inv.extraction_notes


def test_found_true_document_type_unknown_is_not_found_without_a_note():
    contract, notes = convert(wire(document_type={"value": "unknown"}))
    assert contract["document_type"]["value"] is None and notes == []


# ------------------------------------------------------------------------------ found -> values

def test_found_fields_keep_value_page_source_and_confidence():
    contract, notes = convert(load_reply("us_native_invoice"))
    assert contract["total"] == {"value": "1105.00", "page": 1, "source_text": "Total Due: 1,105.00", "confidence": 0.98}
    assert notes == []


def test_page_zero_and_empty_source_become_none_even_when_found():
    contract, _ = convert(wire(total={"page": 0, "source_text": ""}))
    assert contract["total"]["value"] == "1105.00" and contract["total"]["page"] is None and contract["total"]["source_text"] is None


@pytest.mark.parametrize("wire_value,expected", [("yes", True), ("no", False), ("unknown", None)])
def test_yes_no_unknown_become_true_false_none(wire_value, expected):
    reply = wire(po_reference={"explicit": wire_value}, tax={"included_in_total": wire_value})
    reply["document_quality"] = {**reply["document_quality"], "contains_reader_instructions": wire_value}
    contract, _ = convert(reply)
    assert contract["po_reference"]["explicit"] is expected and contract["tax"]["included_in_total"] is expected
    assert contract["document_quality"]["contains_reader_instructions"] is expected


def test_document_quality_type_unknown_becomes_none_and_issues_pass_through():
    reply = wire()
    reply["document_quality"] = {"type": "unknown", "issues": ["skewed", "blurry"], "contains_reader_instructions": "no"}
    q = convert(reply)[0]["document_quality"]
    assert q == {"type": None, "issues": ["skewed", "blurry"], "contains_reader_instructions": False}
    reply["document_quality"]["type"] = "scanned"
    assert convert(reply)[0]["document_quality"]["type"] == "scanned"


def test_line_items_and_adjustments_placeholders_become_none():
    reply = wire(line_items=[{"description": "Bolts", "item_code": "", "quantity": "", "unit_price": "2.50", "amount": "25.00",
                              "page": 0, "source_text": "", "confidence": 0.4}],
                 adjustments=[{"kind": "fee", "description": "", "amount": "", "page": 0, "source_text": "", "confidence": 0.0}])
    contract, _ = convert(reply)
    line, adj = contract["line_items"][0], contract["adjustments"][0]
    assert (line["item_code"], line["quantity"], line["page"], line["source_text"]) == (None, None, None, None)
    assert (line["description"], line["unit_price"], line["amount"]) == ("Bolts", "2.50", "25.00")
    assert (adj["kind"], adj["description"], adj["amount"], adj["page"]) == ("fee", None, None, None)
    ExtractedInvoice.model_validate(contract)


def test_empty_extraction_notes_become_none_and_text_is_kept():
    assert convert(wire(extraction_notes=""))[0]["extraction_notes"] is None
    assert convert(wire(extraction_notes="Two taxes summed."))[0]["extraction_notes"] == "Two taxes summed."


def test_unknown_keys_in_the_reply_are_ignored():
    reply = wire()
    reply["bonus"] = 1
    reply["total"]["extra"] = "x"
    assert convert(reply)[0]["total"]["value"] == "1105.00"


def test_the_input_reply_is_not_mutated():
    reply = load_reply("indian_gst_invoice")
    before = copy.deepcopy(reply)
    from_wire(reply)
    assert reply == before


# ------------------------------------------------------------------------------ structurally wrong replies -> ValueError (repair)

@pytest.mark.parametrize("mutate,fragment", [
    (lambda r: r.pop("total"), "missing field(s): total"),
    (lambda r: r.pop("extraction_notes"), "extraction_notes"),
    (lambda r: r["total"].pop("found"), "total: missing 'found'"),
    (lambda r: r["total"].update(found="yes"), "total.found"),
    (lambda r: r["total"].update(value=None), "total.value: expected a string"),
    (lambda r: r["total"].update(value=1105), "total.value: expected a string"),
    (lambda r: r["total"].update(page=-1), "total.page"),
    (lambda r: r["total"].update(page=1.5), "total.page"),
    (lambda r: r["total"].update(page=True), "total.page"),
    (lambda r: r["total"].update(confidence="high"), "total.confidence"),
    (lambda r: r["po_reference"].update(explicit=True), "po_reference.explicit"),
    (lambda r: r["po_reference"].pop("explicit"), "po_reference: missing 'explicit'"),
    (lambda r: r["tax"].update(included_in_total="maybe"), "tax.included_in_total"),
    (lambda r: r.update(total="1105.00"), "total: expected an object"),
    (lambda r: r.update(line_items="none"), "line_items: expected an array"),
    (lambda r: r.update(line_items=["x"]), "line_items[0]: expected an object"),
    (lambda r: r["line_items"][0].pop("amount"), "line_items[0]: missing 'amount'"),
    (lambda r: r["adjustments"][0].update(page="1"), "adjustments[0].page"),
    (lambda r: r.update(document_quality=[]), "document_quality: expected an object"),
    (lambda r: r["document_quality"].update(issues="skewed"), "document_quality.issues"),
    (lambda r: r["document_quality"].update(contains_reader_instructions=None), "contains_reader_instructions"),
])
def test_structurally_wrong_replies_raise_value_error_naming_the_problem(mutate, fragment):
    reply = load_reply("us_native_invoice")
    mutate(reply)
    with pytest.raises(ValueError) as exc:
        from_wire(reply)
    assert fragment in str(exc.value)


@pytest.mark.parametrize("bad", [None, [], "text", 5])
def test_a_non_object_reply_raises(bad):
    with pytest.raises(ValueError, match="not a JSON object"):
        from_wire(bad)


def test_a_wrong_kind_or_enum_value_is_caught_by_pydantic_after_conversion():
    contract, _ = convert(wire(adjustments=[{"kind": "bribe", "description": "", "amount": "1.00", "page": 0, "source_text": "", "confidence": 0.5}]))
    with pytest.raises(Exception):
        ExtractedInvoice.model_validate(contract)
    contract, _ = convert(wire(document_type={"value": "memo"}))
    with pytest.raises(Exception):
        ExtractedInvoice.model_validate(contract)


# ------------------------------------------------------------------------------ round trip and the unchanged contract

@pytest.mark.parametrize("fixture", ["us_native_invoice", "indian_gst_invoice", "eu_format_invoice", "injection_attempt"])
def test_wire_to_contract_to_wire_round_trips(fixture):
    original = load_reply(fixture)
    assert to_wire(from_wire(original)[0]) == original


def test_round_trip_of_a_blank_and_a_mixed_reply():
    assert to_wire(from_wire(blank())[0]) == blank()
    mixed = wire(vendor_tax_id={"found": False, "value": "", "page": 0, "source_text": "", "confidence": 0.0})
    assert to_wire(from_wire(mixed)[0]) == mixed


def test_the_internal_contract_is_unchanged_nullable_and_free_of_wire_concepts():
    fields = ExtractedInvoice.model_fields
    assert "found" not in ExtractedInvoice().vendor_name.model_dump()
    for name in EVIDENCED_FIELDS:
        assert getattr(ExtractedInvoice(), name).value is None                  # nullable, defaulting to missing
    assert {"vendor_name", "invoice_number", "invoice_date", "currency", "po_reference", "subtotal", "tax", "total",
            "line_items", "document_quality", "extraction_notes"} <= set(fields)          # SPEC 6.1 fields all present
    assert ExtractedInvoice.model_validate({"po_reference": {"value": "PO-1", "explicit": None, "confidence": 0.9}}).po_reference.explicit is None


def test_wire_conversion_does_not_log_unexpected_key_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger="app.models.extraction"):
        postprocess(convert(load_reply("indian_gst_invoice"))[0])
    assert caplog.records == []                                                  # the converter emits contract keys only

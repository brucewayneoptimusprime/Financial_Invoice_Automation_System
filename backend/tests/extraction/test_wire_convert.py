"""wire -> contract conversion for the array-of-entries shape: found=false / placeholders become null, yes/no/unknown
become booleans, names are matched (missing / duplicate / unknown), and the internal contract itself is unchanged."""
import copy
import logging

import pytest

from app.extraction.postprocess import postprocess
from app.extraction.wire import EVIDENCED_FIELDS, from_wire
from app.models import ExtractedInvoice
from tests.extraction.helpers import load_reply
from tests.extraction.wire_convert import drop_field, entry, set_field, to_wire


def blank():
    """A wire reply for an empty document: 11 entries, everything found=false with placeholders."""
    return to_wire({})


def wire(**changes):
    return load_reply("us_native_invoice") if not changes else set_all(load_reply("us_native_invoice"), changes)


def set_all(reply, changes):
    for name, kw in changes.items():
        set_field(reply, name, **kw)
    return reply


# ------------------------------------------------------------------------------ not found -> null

def test_a_blank_reply_converts_to_an_all_null_invoice():
    contract, notes = from_wire(blank())
    assert notes == []
    inv = ExtractedInvoice.model_validate(contract)
    for name in EVIDENCED_FIELDS:
        f = getattr(inv, name)
        assert f.value is None and f.page is None and f.source_text is None and f.confidence == 0.0, name
    assert inv.line_items == [] and inv.adjustments == [] and inv.extraction_notes is None
    assert inv.document_quality.type is None and inv.document_quality.contains_reader_instructions is None
    assert inv.po_reference.explicit is None and inv.tax.included_in_total is None


def test_the_blank_reply_goes_through_postprocess_cleanly():
    contract, notes = from_wire(blank())
    r = postprocess(contract, None, notes)
    assert r.invoice.total.value is None and r.notes == []


def test_found_false_ignores_whatever_the_placeholders_contain():
    contract, _ = from_wire(wire(total={"found": False, "value": "1105.00", "page": 3, "source_text": "junk", "confidence": 0.9}))
    assert contract["total"] == {"value": None, "page": None, "source_text": None, "confidence": 0.0}


def test_found_true_with_an_empty_value_is_not_found_with_a_note():
    for empty in ("", "   "):
        contract, notes = from_wire(wire(invoice_number={"value": empty}))
        assert contract["invoice_number"]["value"] is None and contract["invoice_number"]["confidence"] == 0.0
        assert notes == ["invoice_number: found=true but the value was empty; treated as not found"]


def test_empty_value_notes_reach_the_invoice_notes_through_postprocess():
    contract, notes = from_wire(wire(total={"value": ""}))
    inv = postprocess(contract, None, notes).invoice
    assert inv.total.value is None and "[system] total: found=true but the value was empty" in inv.extraction_notes


def test_found_true_document_type_unknown_is_not_found_without_a_note():
    contract, notes = from_wire(wire(document_type={"value": "unknown"}))
    assert contract["document_type"]["value"] is None and notes == []


# ------------------------------------------------------------------------------ names: missing, duplicate, unknown, order

def test_a_missing_name_becomes_not_found():
    reply = drop_field(load_reply("us_native_invoice"), "vendor_tax_id")
    contract, notes = from_wire(reply)
    assert contract["vendor_tax_id"] == {"value": None, "page": None, "source_text": None, "confidence": 0.0}
    assert contract["total"]["value"] == "1105.00" and notes == []


def test_every_name_missing_gives_an_all_null_invoice():
    reply = load_reply("us_native_invoice")
    reply["fields"] = []
    contract, _ = from_wire(reply)
    assert all(contract[n]["value"] is None for n in EVIDENCED_FIELDS)
    assert contract["po_reference"]["explicit"] is None and contract["tax"]["included_in_total"] is None


def test_duplicate_names_keep_the_first_and_add_a_note():
    reply = load_reply("us_native_invoice")
    reply["fields"].append({"name": "total", "found": True, "value": "9999.00", "page": 2, "source_text": "second", "confidence": 0.99, "flag": "unknown"})
    contract, notes = from_wire(reply)
    assert contract["total"]["value"] == "1105.00"
    assert notes == ["total: appeared more than once in the reply; the first entry was kept"]


def test_the_first_duplicate_wins_even_if_it_is_the_not_found_one():
    reply = blank()
    reply["fields"].append({"name": "total", "found": True, "value": "5.00", "page": 1, "source_text": "x", "confidence": 0.9, "flag": "unknown"})
    contract, notes = from_wire(reply)
    assert contract["total"]["value"] is None and len(notes) == 1


def test_unknown_names_are_ignored_and_logged(caplog):
    reply = load_reply("us_native_invoice")
    reply["fields"].append({"name": "shoe_size", "found": True, "value": "42", "page": 1, "source_text": "x", "confidence": 0.9, "flag": "unknown"})
    with caplog.at_level(logging.WARNING, logger="app.extraction.wire"):
        contract, notes = from_wire(reply)
    assert "shoe_size" not in contract and notes == []
    assert "Ignoring unknown field name 'shoe_size'" in caplog.text


def test_entry_order_does_not_matter():
    reply = load_reply("indian_gst_invoice")
    reversed_reply = {**reply, "fields": list(reversed(reply["fields"]))}
    assert from_wire(reversed_reply) == from_wire(reply)


def test_a_non_string_name_is_a_structural_error():
    reply = load_reply("us_native_invoice")
    reply["fields"][0]["name"] = 7
    with pytest.raises(ValueError, match=r"fields\[0\].name"):
        from_wire(reply)


# ------------------------------------------------------------------------------ found -> values

def test_found_fields_keep_value_page_source_and_confidence():
    contract, notes = from_wire(load_reply("us_native_invoice"))
    assert contract["total"] == {"value": "1105.00", "page": 1, "source_text": "Total Due: 1,105.00", "confidence": 0.98}
    assert notes == []


def test_page_zero_and_empty_source_become_none_even_when_found():
    contract, _ = from_wire(wire(total={"page": 0, "source_text": ""}))
    assert contract["total"]["value"] == "1105.00" and contract["total"]["page"] is None and contract["total"]["source_text"] is None


@pytest.mark.parametrize("flag,expected", [("yes", True), ("no", False), ("unknown", None)])
def test_the_entry_flag_becomes_explicit_and_included_in_total(flag, expected):
    contract, _ = from_wire(wire(po_reference={"flag": flag}, tax={"flag": flag}))
    assert contract["po_reference"]["explicit"] is expected and contract["tax"]["included_in_total"] is expected


def test_flag_on_other_names_is_ignored():
    contract, _ = from_wire(wire(total={"flag": "yes"}, vendor_name={"flag": "no"}))
    assert set(contract["total"]) == {"value", "page", "source_text", "confidence"}
    assert set(contract["vendor_name"]) == {"value", "page", "source_text", "confidence"}


def test_reader_instruction_flag_and_quality_type():
    reply = wire()
    reply["document_quality"] = {"type": "unknown", "issues": ["skewed", "blurry"], "contains_reader_instructions": "yes"}
    q = from_wire(reply)[0]["document_quality"]
    assert q == {"type": None, "issues": ["skewed", "blurry"], "contains_reader_instructions": True}
    reply["document_quality"]["type"] = "scanned"
    assert from_wire(reply)[0]["document_quality"]["type"] == "scanned"


def test_line_items_and_adjustments_placeholders_become_none():
    reply = wire()
    reply["line_items"] = [{"description": "Bolts", "item_code": "", "quantity": "", "unit_price": "2.50", "amount": "25.00",
                            "page": 0, "source_text": "", "confidence": 0.4}]
    reply["adjustments"] = [{"kind": "fee", "description": "", "amount": "", "page": 0, "source_text": "", "confidence": 0.0}]
    contract, _ = from_wire(reply)
    line, adj = contract["line_items"][0], contract["adjustments"][0]
    assert (line["item_code"], line["quantity"], line["page"], line["source_text"]) == (None, None, None, None)
    assert (line["description"], line["unit_price"], line["amount"]) == ("Bolts", "2.50", "25.00")
    assert (adj["kind"], adj["description"], adj["amount"], adj["page"]) == ("fee", None, None, None)
    ExtractedInvoice.model_validate(contract)


def test_empty_extraction_notes_become_none_and_text_is_kept():
    assert from_wire({**wire(), "extraction_notes": ""})[0]["extraction_notes"] is None
    assert from_wire({**wire(), "extraction_notes": "Two taxes summed."})[0]["extraction_notes"] == "Two taxes summed."


def test_unknown_keys_in_the_reply_and_in_entries_are_ignored():
    reply = wire()
    reply["bonus"] = 1
    entry(reply, "total")["extra"] = "x"
    assert from_wire(reply)[0]["total"]["value"] == "1105.00"


def test_the_input_reply_is_not_mutated():
    reply = load_reply("indian_gst_invoice")
    before = copy.deepcopy(reply)
    from_wire(reply)
    assert reply == before


# ------------------------------------------------------------------------------ leniency (replies that were NOT grammar-constrained)

def test_numbers_are_accepted_for_values_and_stringified():
    reply = wire()
    set_field(reply, "total", value=1105.5)
    reply["line_items"][0]["quantity"] = 10
    reply["line_items"][0]["amount"] = 600.0
    contract, _ = from_wire(reply)
    assert contract["total"]["value"] == "1105.5" and contract["line_items"][0]["quantity"] == "10"
    assert contract["line_items"][0]["amount"] == "600.0"


def test_an_integral_float_page_is_accepted():
    assert from_wire(wire(total={"page": 1.0}))[0]["total"]["page"] == 1


def test_missing_optional_entry_keys_default_in_the_safe_direction():
    reply = wire()
    reply["fields"] = [{"name": "total", "found": True, "value": "5.00"}]                    # no page/source/confidence/flag
    contract, _ = from_wire(reply)
    assert contract["total"] == {"value": "5.00", "page": None, "source_text": None, "confidence": 0.0}    # confidence 0 -> review
    assert contract["po_reference"]["explicit"] is None


def test_missing_value_key_on_a_found_entry_is_treated_as_empty():
    reply = wire()
    reply["fields"] = [{"name": "total", "found": True}]
    contract, notes = from_wire(reply)
    assert contract["total"]["value"] is None and "total: found=true but the value was empty" in notes[0]


def test_missing_kind_defaults_to_other():
    reply = wire()
    reply["adjustments"] = [{"amount": "-0.02"}]
    assert from_wire(reply)[0]["adjustments"][0]["kind"] == "other"


# ------------------------------------------------------------------------------ structurally wrong replies -> ValueError (repair)

@pytest.mark.parametrize("mutate,fragment", [
    (lambda r: r.pop("fields"), "missing field(s): fields"),
    (lambda r: r.pop("extraction_notes"), "extraction_notes"),
    (lambda r: r.pop("line_items"), "line_items"),
    (lambda r: r.update(fields="none"), "fields: expected an array"),
    (lambda r: r.update(fields=["x"]), "fields[0]: expected an object"),
    (lambda r: r["fields"][0].pop("found"), "found: expected true or false"),
    (lambda r: r["fields"][0].update(found="yes"), "found: expected true or false"),
    (lambda r: r["fields"][0].update(value=None), "value: expected a string"),
    (lambda r: r["fields"][0].update(value=True), "value: expected a string"),
    (lambda r: r["fields"][0].update(page=-1), "page"),
    (lambda r: r["fields"][0].update(page=1.5), "page"),
    (lambda r: r["fields"][0].update(page=True), "page"),
    (lambda r: r["fields"][0].update(page="1"), "page"),
    (lambda r: r["fields"][0].update(confidence="high"), "confidence"),
    (lambda r: r["fields"][0].update(flag="maybe"), "flag: expected yes, no or unknown"),
    (lambda r: r.update(line_items="none"), "line_items: expected an array"),
    (lambda r: r.update(line_items=["x"]), "line_items[0]: expected an object"),
    (lambda r: r["adjustments"][0].update(page="1"), "adjustments[0].page"),
    (lambda r: r.update(document_quality=[]), "document_quality: expected an object"),
    (lambda r: r["document_quality"].update(issues="skewed"), "document_quality.issues"),
    (lambda r: r["document_quality"].update(contains_reader_instructions="maybe"), "contains_reader_instructions"),
    (lambda r: r.update(extraction_notes=None), "extraction_notes"),
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
    reply = wire()
    reply["adjustments"] = [{"kind": "bribe", "description": "", "amount": "1.00", "page": 0, "source_text": "", "confidence": 0.5}]
    with pytest.raises(Exception):
        ExtractedInvoice.model_validate(from_wire(reply)[0])
    with pytest.raises(Exception):
        ExtractedInvoice.model_validate(from_wire(wire(document_type={"value": "memo"}))[0])


# ------------------------------------------------------------------------------ round trip and the unchanged contract

@pytest.mark.parametrize("fixture", ["us_native_invoice", "indian_gst_invoice", "eu_format_invoice", "injection_attempt"])
def test_wire_to_contract_to_wire_round_trips(fixture):
    original = load_reply(fixture)
    assert to_wire(from_wire(original)[0]) == original


def test_round_trip_of_a_blank_and_a_mixed_reply():
    assert to_wire(from_wire(blank())[0]) == blank()
    mixed = wire(vendor_tax_id={"found": False, "value": "", "page": 0, "source_text": "", "confidence": 0.0})
    assert to_wire(from_wire(mixed)[0]) == mixed


def test_the_wire_reply_always_carries_all_eleven_names_in_contract_order():
    assert [e["name"] for e in blank()["fields"]] == list(EVIDENCED_FIELDS) and len(EVIDENCED_FIELDS) == 11


def test_the_internal_contract_is_unchanged_nullable_and_free_of_wire_concepts():
    fields = ExtractedInvoice.model_fields
    assert "found" not in ExtractedInvoice().vendor_name.model_dump() and "flag" not in ExtractedInvoice().po_reference.model_dump()
    for name in EVIDENCED_FIELDS:
        assert getattr(ExtractedInvoice(), name).value is None
    assert {"vendor_name", "invoice_number", "invoice_date", "currency", "po_reference", "subtotal", "tax", "total",
            "line_items", "document_quality", "extraction_notes"} <= set(fields)          # SPEC 6.1 fields all present
    assert ExtractedInvoice.model_validate({"po_reference": {"value": "PO-1", "explicit": None, "confidence": 0.9}}).po_reference.explicit is None


def test_wire_conversion_does_not_log_unexpected_key_warnings(caplog):
    with caplog.at_level(logging.WARNING):
        postprocess(from_wire(load_reply("indian_gst_invoice"))[0])
    assert [r for r in caplog.records if r.name.startswith("app.")] == []               # the converter emits contract keys only

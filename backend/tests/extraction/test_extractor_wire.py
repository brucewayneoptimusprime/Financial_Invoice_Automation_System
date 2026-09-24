"""The extractor with the array-of-entries wire format: conversion problems trigger the repair retry, placeholders
become nulls, and the config switch chooses json_schema or prompt_json."""
import json
from decimal import Decimal

from app.extraction.extractor import extract_invoice
from app.extraction.wire import wire_schema
from tests.extraction.helpers import ingest_of, load_reply, reply_text, settings
from tests.extraction.wire_convert import drop_field, entry, set_field, to_wire
from tests.llm.fakes import FakeLLMClient, ok_response

D = Decimal


def run(tmp_path, *replies, **kw):
    fake = FakeLLMClient(*[ok_response(r if isinstance(r, str) else reply_text(r)) for r in replies])
    info = ingest_of(tmp_path, **kw)
    return extract_invoice(info, client=fake, settings=settings(tmp_path, **kw)), fake


def test_the_request_carries_the_union_free_wire_schema(tmp_path):
    out, fake = run(tmp_path, load_reply("us_native_invoice"))
    schema = fake.requests[0].schema
    assert schema == wire_schema() and out.meta.structured_output == "json_schema"
    text = json.dumps(schema)
    assert "anyOf" not in text and "oneOf" not in text and '"null"' not in text        # structure: see test_wire_limits.py


def test_a_blank_reply_is_a_valid_all_null_extraction_not_a_failure(tmp_path):
    out, _ = run(tmp_path, to_wire({}))
    assert not out.meta.degraded and out.meta.attempts == 1
    assert out.invoice.total.value is None and out.invoice.vendor_name.value is None and out.invoice.total.confidence == 0.0


def test_found_true_with_an_empty_value_becomes_null_and_leaves_a_note(tmp_path):
    reply = set_field(load_reply("us_native_invoice"), "total", value="")
    out, _ = run(tmp_path, reply)
    assert not out.meta.degraded and out.invoice.total.value is None and out.invoice.total.confidence == 0.0
    assert "[system] total: found=true but the value was empty; treated as not found" in out.invoice.extraction_notes
    assert out.invoice.invoice_number.value == "INV-2026-0042"                     # everything else is intact


def test_a_missing_name_is_simply_not_found_and_needs_no_repair(tmp_path):
    out, fake = run(tmp_path, drop_field(load_reply("us_native_invoice"), "vendor_tax_id"))
    assert out.meta.attempts == 1 and len(fake.requests) == 1 and out.invoice.vendor_tax_id.value is None


def test_duplicate_names_keep_the_first_and_note_it(tmp_path):
    reply = load_reply("us_native_invoice")
    reply["fields"].append({**entry(reply, "total"), "value": "9999.00"})
    out, _ = run(tmp_path, reply)
    assert out.invoice.total.value == D("1105.00") and "appeared more than once" in out.invoice.extraction_notes


def test_a_reply_missing_a_top_level_key_gets_one_repair_naming_it(tmp_path):
    broken = load_reply("us_native_invoice")
    del broken["fields"]
    out, fake = run(tmp_path, broken, load_reply("us_native_invoice"))
    assert out.meta.schema_repair_used and out.invoice.total.value == D("1105.00")
    assert "missing field(s): fields" in fake.requests[1].parts[-1].text


def test_a_reply_with_a_wrong_type_gets_one_repair_naming_the_field(tmp_path):
    broken = set_field(load_reply("us_native_invoice"), "total", found="yes")
    out, fake = run(tmp_path, broken, load_reply("us_native_invoice"))
    assert out.meta.schema_repair_used and "fields[total].found" in fake.requests[1].parts[-1].text


def test_the_old_shapes_are_no_longer_accepted(tmp_path):
    """A reply in the retired per-field or nullable shapes lacks `fields`, so it is a schema failure."""
    old = {"vendor_name": {"found": True, "value": "X", "page": 1, "source_text": "X", "confidence": 0.9}}
    out, _ = run(tmp_path, old, old)
    assert out.meta.degraded and out.meta.failure_code == "schema_invalid"


def test_the_indian_fixture_survives_the_flag_round_trip(tmp_path):
    out, _ = run(tmp_path, load_reply("indian_gst_invoice"))
    assert out.invoice.currency.value == "INR" and out.invoice.po_reference.value is None and out.invoice.po_reference.explicit is None
    assert out.invoice.line_items[0].item_code is None


def test_document_flags_survive_the_yes_no_unknown_round_trip(tmp_path):
    out, _ = run(tmp_path, load_reply("injection_attempt"))
    assert out.invoice.document_quality.contains_reader_instructions is True
    assert out.invoice.tax.included_in_total is None and out.invoice.total.value == D("500.00")
    out2, _ = run(tmp_path / "second", load_reply("us_native_invoice"))
    assert out2.invoice.po_reference.explicit is True and out2.invoice.tax.included_in_total is False
    assert out2.invoice.document_quality.contains_reader_instructions is False

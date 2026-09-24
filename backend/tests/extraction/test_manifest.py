"""The answer-key parser and the draft writer."""
import json
from decimal import Decimal

import pytest

from app.extraction.manifest import HEADER, append_drafts, draft_expected, load_manifest, parse_manifest, render_entry
from app.extraction.postprocess import postprocess

GOOD = '''# My answer key

Some notes for humans. A stray block that is not an entry:

```json
{"not": "an entry"}
```

## What to do
This section has no expected block, so it is only notes.

## acme_invoice_01.pdf
- exercises: native PDF, tax excluded
- second note
```expected
{"verified": true, "vendor_name": "Acme Supplies Ltd", "invoice_number": "INV-1001", "total": "2,160.00",
 "vendor_tax_id": null, "invoice_date": "2026-03-14", "currency": "usd", "document_type": "Invoice",
 "line_items": [{"description": "Widgets", "amount": 2000}],
 "adjustments": [{"kind": "Discount", "amount": "-10.00"}]}
```

## second one.png
```expected
{"total": "50.00"}
```
'''


def test_a_full_manifest_parses_into_normalised_entries():
    m = parse_manifest(GOOD)
    assert m.problems == [] and list(m.entries) == ["acme_invoice_01.pdf", "second one.png"]
    e = m.entries["acme_invoice_01.pdf"]
    assert e.verified is True and e.notes == ["exercises: native PDF, tax excluded", "second note"]
    assert e.expected == {"vendor_name": "Acme Supplies Ltd", "invoice_number": "INV-1001", "total": "2160.00", "vendor_tax_id": None,
                          "invoice_date": "2026-03-14", "currency": "USD", "document_type": "invoice",
                          "line_items": [{"description": "Widgets", "amount": "2000"}],
                          "adjustments": [{"kind": "discount", "amount": "-10.00"}]}
    assert "vendor_tax_id" in e.expected and e.expected["vendor_tax_id"] is None          # explicit null is kept: it means "must be absent"
    assert "tax" not in e.expected                                                          # omitted stays omitted: not scored
    assert m.sections_without_block == ["What to do"]


def test_verified_defaults_to_false_and_only_true_counts():
    m = parse_manifest(GOOD)
    assert m.entries["second one.png"].verified is False
    assert list(m.verified) == ["acme_invoice_01.pdf"]


@pytest.mark.parametrize("value", ['"yes"', "1", "null", '"true"'])
def test_verified_must_be_a_real_boolean(value):
    m = parse_manifest(f'## a.pdf\n```expected\n{{"verified": {value}, "total": "1"}}\n```\n')
    assert m.entries == {} and any("verified" in p and "true or false" in p for p in m.problems)


def test_bad_json_is_a_problem_not_a_crash():
    m = parse_manifest('## a.pdf\n```expected\n{"total": 1,}\n```\n## b.pdf\n```expected\n{"verified": true, "total": "2"}\n```\n')
    assert list(m.entries) == ["b.pdf"] and len(m.problems) == 1
    assert m.problems[0].startswith("a.pdf: the expected block is not valid JSON") and "entry skipped" in m.problems[0]


@pytest.mark.parametrize("body,fragment", [
    ('{"vendor": "x"}', "unknown field(s) ['vendor']"), ('[1, 2]', "must be a JSON object"), ('{"total": "abc"}', "total: 'abc' is not a number"),
    ('{"total": true}', "total: expected a number"), ('{"invoice_date": "14/03/2026"}', "not an ISO date"), ('{"currency": "dollars"}', "3-letter code"),
    ('{"document_type": "bill"}', "document_type"), ('{"vendor_name": ""}', "non-empty string"),
    ('{"line_items": {"amount": "1"}}', "expected a list"), ('{"line_items": [{"description": "x"}]}', "'amount' is required"),
    ('{"line_items": [{"amount": "1", "sku": "x"}]}', "unknown key(s) ['sku']"), ('{"adjustments": [{"amount": "1"}]}', "'kind' and 'amount' are required"),
    ('{"adjustments": [{"kind": "tip", "amount": "1"}]}', "adjustments[1].kind"), ('{"line_items": ["x"]}', "expected an object")])
def test_invalid_expected_values_are_reported_with_the_field(body, fragment):
    m = parse_manifest(f"## a.pdf\n```expected\n{body}\n```\n")
    assert m.entries == {} and len(m.problems) == 1 and fragment in m.problems[0] and m.problems[0].startswith("a.pdf: ")


def test_a_duplicate_heading_keeps_the_first_entry_and_says_so():
    m = parse_manifest('## a.pdf\n```expected\n{"verified": true, "total": "1"}\n```\n## a.pdf\n```expected\n{"verified": true, "total": "2"}\n```\n')
    assert m.entries["a.pdf"].expected == {"total": "1"} and any("appears more than once" in p for p in m.problems)


def test_an_unclosed_fence_is_a_problem():
    m = parse_manifest('## a.pdf\n```expected\n{"total": "1"}\n')
    assert m.entries == {} and any("never closed" in p for p in m.problems)


def test_blocks_with_other_labels_or_outside_sections_are_ignored():
    m = parse_manifest('```expected\n{"total": "1"}\n```\n## a.pdf\n```json\n{"total": "1"}\n```\n')
    assert m.entries == {} and m.sections_without_block == ["a.pdf"]


def test_windows_line_endings_and_a_bom_are_fine(tmp_path):
    path = tmp_path / "manifest.md"
    path.write_bytes(("﻿" + GOOD.replace("\n", "\r\n")).encode("utf-8"))
    m = load_manifest(path)
    assert m.problems == [] and set(m.entries) == {"acme_invoice_01.pdf", "second one.png"}


def test_a_missing_manifest_is_empty_not_an_error(tmp_path):
    m = load_manifest(tmp_path / "nope.md")
    assert m.entries == {} and m.problems == []


def test_an_unreadable_manifest_is_a_problem(tmp_path):
    path = tmp_path / "manifest.md"
    path.write_bytes(b"\xff\xfe\x00bad")
    assert load_manifest(path).problems


def test_amounts_keep_their_exact_digits():
    m = parse_manifest('## a.pdf\n```expected\n{"total": "1,234.560", "tax": 0.1, "line_items": [{"amount": "0.005"}]}\n```\n')
    e = m.entries["a.pdf"].expected
    assert e["total"] == "1234.560" and e["tax"] == "0.1" and e["line_items"][0]["amount"] == "0.005"


# ------------------------------------------------------------------------------------------------ drafts

def sample_invoice():
    contract = {"vendor_name": {"value": "SuperStore", "page": 1, "source_text": "SuperStore", "confidence": 0.9},
                "total": {"value": "1770.61", "page": 1, "source_text": "$1,770.61", "confidence": 0.9},
                "invoice_date": {"value": "2013-03-07", "page": 1, "source_text": "Mar 07 2013", "confidence": 0.9},
                "currency": {"value": "USD", "page": 1, "source_text": "USD", "confidence": 0.9},
                "line_items": [{"description": "Chair", "quantity": "4", "unit_price": "461.48", "amount": "1845.94", "confidence": 0.9},
                               {"description": "no amount", "confidence": 0.5}],
                "adjustments": [{"kind": "discount", "amount": "184.59", "description": "Discount (10%)", "confidence": 0.9}]}
    return postprocess(contract).invoice


def test_a_draft_reflects_the_extraction_and_is_never_verified():
    d = draft_expected(sample_invoice())
    assert d["verified"] is False and d["vendor_name"] == "SuperStore" and d["total"] == "1770.61" and d["invoice_date"] == "2013-03-07"
    assert d["tax"] is None and d["vendor_tax_id"] is None                                  # explicit nulls for a human to confirm
    assert d["line_items"] == [{"description": "Chair", "quantity": "4", "unit_price": "461.48", "amount": "1845.94"}]
    assert d["adjustments"] == [{"kind": "discount", "amount": "-184.59", "description": "Discount (10%)"}]


def test_a_draft_written_to_the_manifest_parses_back_as_an_unverified_entry(tmp_path):
    path = tmp_path / "manifest.md"
    written, skipped = append_drafts(path, {"a b.pdf": (draft_expected(sample_invoice()), ["draft: check me"])})
    assert written == ["a b.pdf"] and skipped == []
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEADER) and "## a b.pdf\n- draft: check me\n```expected\n" in text
    m = load_manifest(path)
    assert m.problems == [] and m.entries["a b.pdf"].verified is False and m.verified == {}
    assert m.entries["a b.pdf"].expected["total"] == "1770.61" and m.entries["a b.pdf"].notes == ["draft: check me"]


def test_existing_entries_are_never_touched_verified_or_not(tmp_path):
    path = tmp_path / "manifest.md"
    path.write_text(GOOD, encoding="utf-8", newline="\n")
    before = path.read_bytes()
    written, skipped = append_drafts(path, {"acme_invoice_01.pdf": (draft_expected(sample_invoice()), []),
                                            "second one.png": (draft_expected(sample_invoice()), [])})
    assert written == [] and sorted(skipped) == ["acme_invoice_01.pdf", "second one.png"] and path.read_bytes() == before


def test_new_drafts_are_appended_after_existing_content_which_stays_byte_identical(tmp_path):
    path = tmp_path / "manifest.md"
    path.write_text(GOOD, encoding="utf-8", newline="\n")
    written, skipped = append_drafts(path, {"acme_invoice_01.pdf": (draft_expected(sample_invoice()), []),
                                            "new.pdf": (draft_expected(sample_invoice()), [])})
    assert written == ["new.pdf"] and skipped == ["acme_invoice_01.pdf"]
    assert path.read_text(encoding="utf-8").startswith(GOOD.rstrip("\n"))
    m = load_manifest(path)
    assert m.entries["acme_invoice_01.pdf"].verified is True and m.entries["new.pdf"].verified is False


def test_a_draft_cannot_be_written_as_verified(tmp_path):
    with pytest.raises(ValueError, match="verified=false"):
        append_drafts(tmp_path / "m.md", {"a.pdf": ({"verified": True, "total": "1"}, [])})
    with pytest.raises(ValueError):
        append_drafts(tmp_path / "m.md", {"a.pdf": ({"total": "1"}, [])})                   # missing means unverified but must be explicit
    assert not (tmp_path / "m.md").exists()


def test_render_entry_is_valid_json_in_an_expected_block():
    text = render_entry("x.pdf", {"verified": False, "total": "1.00"}, ["n"])
    body = text.split("```expected\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(body) == {"verified": False, "total": "1.00"}


def test_decimal_amounts_survive_the_round_trip():
    d = draft_expected(sample_invoice())
    m = parse_manifest(render_entry("x.pdf", d, []))
    assert Decimal(m.entries["x.pdf"].expected["line_items"][0]["amount"]) == Decimal("1845.94")

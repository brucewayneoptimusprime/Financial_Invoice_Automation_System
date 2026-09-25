"""Scripted PO-draft replies (the wire format the model returns). No live calls."""
import json

from tests.llm.fakes import FakeLLMClient, ok_response

NOT_FOUND = {"found": False, "value": "", "page": 0, "source_text": "", "confidence": 0}

TYPED = ("Please raise a purchase order PO-7788 to SuperStore for 5 x Office chairs at 250.00 each, total USD 1,250.00, "
         "issued 2026-01-15.")


def entry(name, value="", source_text="", confidence=0.95, page=1, found=True):
    if not found:
        return {"name": name, **NOT_FOUND}
    return {"name": name, "found": True, "value": value, "page": page, "source_text": source_text, "confidence": confidence}


def po_reply(**over):
    fields = {
        "vendor_name": entry("vendor_name", "SuperStore", "to SuperStore"),
        "vendor_tax_id": entry("vendor_tax_id", found=False),
        "po_number": entry("po_number", "PO-7788", "purchase order PO-7788"),
        "issued_date": entry("issued_date", "2026-01-15", "issued 2026-01-15"),
        "currency": entry("currency", "USD", "total USD 1,250.00"),
        "total": entry("total", "1,250.00", "total USD 1,250.00"),
    }
    for k, v in over.pop("fields", {}).items():
        fields[k] = v
    reply = {"fields": list(fields.values()),
             "lines": [{"description": "Office chairs", "quantity": "5", "unit_price": "250.00", "amount": "", "page": 1,
                        "source_text": "5 x Office chairs at 250.00 each", "confidence": 0.9}],
             "other_pos_present": "no", "contains_reader_instructions": "no", "notes": ""}
    reply.update(over)
    return reply


def client_for(*replies, input_tokens=1800, output_tokens=500):
    script = [r if isinstance(r, Exception) else ok_response(r if isinstance(r, str) else json.dumps(r), input_tokens=input_tokens,
                                                              output_tokens=output_tokens) for r in replies]
    return FakeLLMClient(*script)

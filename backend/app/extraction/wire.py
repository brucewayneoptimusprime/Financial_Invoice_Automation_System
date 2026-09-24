"""The JSON schema sent to the API (structured output). Deliberately hand-written and separate from the Pydantic
contract: the API's schema dialect forbids numeric/string constraints and requires `additionalProperties:false`
on every object, so ranges/enums are enforced afterwards by Pydantic. A drift test keeps the two in step.

System-only fields (model_confidence, grounding, printed_amount) are NOT in the wire schema; the model never
supplies them.
"""
from typing import Any

DOCUMENT_TYPES = ["invoice", "credit_note", "proforma", "quote", "statement", "receipt", "other"]
ADJUSTMENT_KINDS = ["shipping", "discount", "credit", "fee", "rounding", "other"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _evidenced(value_schema: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return _object({
        "value": _nullable(value_schema),
        "page": _nullable({"type": "integer"}),
        "source_text": _nullable({"type": "string"}),
        "confidence": {"type": "number"},
        **extra,
    })


_STR = {"type": "string"}
_BOOL_OR_NULL = _nullable({"type": "boolean"})


def wire_schema() -> dict[str, Any]:
    """A fresh copy of the response schema."""
    money = {"type": "string"}                 # a plain decimal string, e.g. "2160.00" (never a float)
    return _object({
        "vendor_name": _evidenced(_STR),
        "vendor_tax_id": _evidenced(_STR),
        "vendor_address": _evidenced(_STR),
        "document_type": _evidenced({"type": "string", "enum": DOCUMENT_TYPES}),
        "invoice_number": _evidenced(_STR),
        "invoice_date": _evidenced(_STR),      # ISO YYYY-MM-DD
        "currency": _evidenced(_STR),          # ISO code, or the symbol exactly as printed
        "po_reference": _evidenced(_STR, explicit=_BOOL_OR_NULL),
        "subtotal": _evidenced(money),
        "tax": _evidenced(money, included_in_total=_BOOL_OR_NULL),
        "total": _evidenced(money),
        "line_items": {"type": "array", "items": _object({
            "description": _nullable(_STR),
            "item_code": _nullable(_STR),
            "quantity": _nullable(money),
            "unit_price": _nullable(money),
            "amount": _nullable(money),
            "page": _nullable({"type": "integer"}),
            "source_text": _nullable(_STR),
            "confidence": {"type": "number"},
        })},
        "adjustments": {"type": "array", "items": _object({
            "kind": {"type": "string", "enum": ADJUSTMENT_KINDS},
            "description": _nullable(_STR),
            "amount": _nullable(money),
            "page": _nullable({"type": "integer"}),
            "source_text": _nullable(_STR),
            "confidence": {"type": "number"},
        })},
        "document_quality": _object({
            "type": _nullable({"type": "string", "enum": ["scanned", "native"]}),
            "issues": {"type": "array", "items": _STR},
            "contains_reader_instructions": _BOOL_OR_NULL,
        }),
        "extraction_notes": _nullable(_STR),
    })

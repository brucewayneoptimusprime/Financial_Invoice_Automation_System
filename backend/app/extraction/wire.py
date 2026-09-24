"""The JSON schema sent to the API (structured output), and the converter from the model's reply to the
internal contract.

WHY IT LOOKS LIKE THIS. The API compiles the schema into a grammar and limits its complexity: it rejected an
earlier, nullable design with "too many parameters with union types (49 ... limit: 16)". So this wire schema has
ZERO unions: no `anyOf`, no type arrays, no `null`, no optional properties. "Missing" is expressed with a
`found` boolean and placeholders (value "", page 0, source_text "", confidence 0), and nullable booleans become
the enum yes / no / unknown.

The internal contract (SPEC 6.1, nullable values) is UNCHANGED: `from_wire` turns found=false / placeholders back
into null, page 0 into None, and yes/no/unknown into True/False/None. Pydantic then validates ranges and enums.
System-only fields (model_confidence, grounding, printed_amount) are never requested from the model.
"""
from typing import Any

DOCUMENT_TYPES = ["invoice", "credit_note", "proforma", "quote", "statement", "receipt", "other"]
ADJUSTMENT_KINDS = ["shipping", "discount", "credit", "fee", "rounding", "other"]
YES_NO_UNKNOWN = ["yes", "no", "unknown"]
UNKNOWN = "unknown"

EVIDENCED_FIELDS = ("vendor_name", "vendor_tax_id", "vendor_address", "document_type", "invoice_number",
                    "invoice_date", "currency", "po_reference", "subtotal", "tax", "total")
# extra tri-state flag on a header field: wire name -> contract name (same)
_FLAGS = {"po_reference": "explicit", "tax": "included_in_total"}
_TOP_LEVEL = (*EVIDENCED_FIELDS, "line_items", "adjustments", "document_quality", "extraction_notes")


# --------------------------------------------------------------------------------------------- the schema

def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}


def _evidenced(value_schema: dict[str, Any] | None = None, **flags: Any) -> dict[str, Any]:
    return _object({
        "found": {"type": "boolean"},
        "value": value_schema or _STRING,
        "page": {"type": "integer"},
        "source_text": _STRING,
        "confidence": {"type": "number"},
        **flags,
    })


def _yes_no_unknown() -> dict[str, Any]:
    return {"type": "string", "enum": list(YES_NO_UNKNOWN)}


def wire_schema() -> dict[str, Any]:
    """A fresh copy of the response schema (no unions, no nulls, every property required)."""
    return _object({
        "vendor_name": _evidenced(),
        "vendor_tax_id": _evidenced(),
        "vendor_address": _evidenced(),
        "document_type": _evidenced({"type": "string", "enum": [*DOCUMENT_TYPES, UNKNOWN]}),
        "invoice_number": _evidenced(),
        "invoice_date": _evidenced(),                        # ISO YYYY-MM-DD
        "currency": _evidenced(),                            # ISO code, or the symbol exactly as printed
        "po_reference": _evidenced(explicit=_yes_no_unknown()),
        "subtotal": _evidenced(),                            # money: a plain decimal string, e.g. "2160.00"
        "tax": _evidenced(included_in_total=_yes_no_unknown()),
        "total": _evidenced(),
        "line_items": {"type": "array", "items": _object({
            "description": _STRING, "item_code": _STRING, "quantity": _STRING, "unit_price": _STRING,
            "amount": _STRING, "page": {"type": "integer"}, "source_text": _STRING, "confidence": {"type": "number"},
        })},
        "adjustments": {"type": "array", "items": _object({
            "kind": {"type": "string", "enum": list(ADJUSTMENT_KINDS)}, "description": _STRING, "amount": _STRING,
            "page": {"type": "integer"}, "source_text": _STRING, "confidence": {"type": "number"},
        })},
        "document_quality": _object({
            "type": {"type": "string", "enum": ["scanned", "native", UNKNOWN]},
            "issues": {"type": "array", "items": _STRING},
            "contains_reader_instructions": _yes_no_unknown(),
        }),
        "extraction_notes": _STRING,
    })


# ------------------------------------------------------------------------------------- wire -> contract

def _need(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise ValueError(f"{where}: missing '{key}'")
    return obj[key]


def _string(obj: dict, key: str, where: str) -> str:
    value = _need(obj, key, where)
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key}: expected a string")
    return value


def _page(obj: dict, where: str) -> int | None:
    page = _need(obj, "page", where)
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise ValueError(f"{where}.page: expected an integer >= 0 (0 when unknown)")
    return page or None


def _confidence(obj: dict, where: str) -> float:
    conf = _need(obj, "confidence", where)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ValueError(f"{where}.confidence: expected a number")
    return float(conf)


def _tri_state(obj: dict, key: str, where: str) -> bool | None:
    value = _need(obj, key, where)
    mapping = {"yes": True, "no": False, "unknown": None}
    if value not in mapping:
        raise ValueError(f"{where}.{key}: expected yes, no or unknown")
    return mapping[value]


def _blank_to_none(text: str) -> str | None:
    return text if text.strip() else None


def _evidenced_from_wire(name: str, raw: Any, notes: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: expected an object")
    found = _need(raw, "found", name)
    if not isinstance(found, bool):
        raise ValueError(f"{name}.found: expected true or false")
    flag = _FLAGS.get(name)
    flag_value = _tri_state(raw, flag, name) if flag else None
    value = _string(raw, "value", name)
    page, conf = _page(raw, name), _confidence(raw, name)
    source = _string(raw, "source_text", name)

    empty = {"value": None, "page": None, "source_text": None, "confidence": 0.0}
    if flag:
        empty[flag] = None
    if not found:
        return empty                                     # placeholders are ignored, whatever they contain
    if not value.strip() or (name == "document_type" and value == UNKNOWN):
        if value.strip() == "" :
            notes.append(f"{name}: found=true but the value was empty; treated as not found")
        return empty
    out = {"value": value, "page": page, "source_text": _blank_to_none(source), "confidence": conf}
    if flag:
        out[flag] = flag_value
    return out


def from_wire(reply: Any) -> tuple[dict[str, Any], list[str]]:
    """The model's reply (wire format) -> (contract-shaped dict, system notes). Raises ValueError for a reply
    that is structurally wrong, which the extractor turns into its single schema-repair retry."""
    if not isinstance(reply, dict):
        raise ValueError("the reply is not a JSON object")
    missing = [k for k in _TOP_LEVEL if k not in reply]
    if missing:
        raise ValueError(f"the reply is missing field(s): {', '.join(missing)}")
    notes: list[str] = []
    out: dict[str, Any] = {name: _evidenced_from_wire(name, reply[name], notes) for name in EVIDENCED_FIELDS}

    items = reply["line_items"]
    if not isinstance(items, list):
        raise ValueError("line_items: expected an array")
    out["line_items"] = []
    for i, item in enumerate(items):
        where = f"line_items[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected an object")
        out["line_items"].append({
            **{k: _blank_to_none(_string(item, k, where)) for k in ("description", "item_code", "quantity", "unit_price", "amount")},
            "page": _page(item, where), "source_text": _blank_to_none(_string(item, "source_text", where)),
            "confidence": _confidence(item, where)})

    adjustments = reply["adjustments"]
    if not isinstance(adjustments, list):
        raise ValueError("adjustments: expected an array")
    out["adjustments"] = []
    for i, adj in enumerate(adjustments):
        where = f"adjustments[{i}]"
        if not isinstance(adj, dict):
            raise ValueError(f"{where}: expected an object")
        out["adjustments"].append({
            "kind": _string(adj, "kind", where), "description": _blank_to_none(_string(adj, "description", where)),
            "amount": _blank_to_none(_string(adj, "amount", where)), "page": _page(adj, where),
            "source_text": _blank_to_none(_string(adj, "source_text", where)), "confidence": _confidence(adj, where)})

    quality = reply["document_quality"]
    if not isinstance(quality, dict):
        raise ValueError("document_quality: expected an object")
    q_type = _string(quality, "type", "document_quality")
    issues = _need(quality, "issues", "document_quality")
    if not isinstance(issues, list) or not all(isinstance(x, str) for x in issues):
        raise ValueError("document_quality.issues: expected an array of strings")
    out["document_quality"] = {
        "type": None if q_type == UNKNOWN else q_type, "issues": list(issues),
        "contains_reader_instructions": _tri_state(quality, "contains_reader_instructions", "document_quality")}

    out["extraction_notes"] = _blank_to_none(_string(reply, "extraction_notes", "reply"))
    return out, notes

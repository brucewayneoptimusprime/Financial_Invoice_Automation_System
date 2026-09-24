"""The JSON shape the model returns, the schema describing it, and the converter to the internal contract.

WHY IT LOOKS LIKE THIS. The API compiles a strict schema into a grammar and limits its size and complexity. Two live
calls were rejected: first "too many parameters with union types (49 ... limit: 16)", then "The compiled grammar is
too large". So the wire schema is deliberately tiny and flat:

  * ZERO unions, nulls or optional properties (missing = a `found` boolean plus placeholders "" / 0).
  * The 11 header fields are ONE array of entries {name, found, value, page, source_text, confidence, flag}, so the
    evidence block is defined once instead of once per field (5 objects / 29 properties in total, versus 15 / 89).
  * Nullable booleans are the enum yes / no / unknown. `flag` carries po_reference.explicit and
    tax.included_in_total; it is "unknown" for every other name.

The internal contract (SPEC 6.1, nullable) is UNCHANGED. `from_wire` turns found=false into null, page 0 into None,
yes/no/unknown into True/False/None. It is also used for replies that were NOT grammar-constrained
(llm_structured_output="prompt_json"), so per-entry gaps are handled leniently, always in the safe direction
(a missing confidence is 0). Pydantic validates ranges and enums afterwards. System-only fields (model_confidence,
grounding, printed_amount) are never requested from the model.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

DOCUMENT_TYPES = ["invoice", "credit_note", "proforma", "quote", "statement", "receipt", "other"]
ADJUSTMENT_KINDS = ["shipping", "discount", "credit", "fee", "rounding", "other"]
YES_NO_UNKNOWN = ["yes", "no", "unknown"]
UNKNOWN = "unknown"

EVIDENCED_FIELDS = ("vendor_name", "vendor_tax_id", "vendor_address", "document_type", "invoice_number",
                    "invoice_date", "currency", "po_reference", "subtotal", "tax", "total")
# entry `flag` -> the contract key it carries, for the two names that have a tri-state flag
_FLAGS = {"po_reference": "explicit", "tax": "included_in_total"}
_TOP_LEVEL = ("fields", "line_items", "adjustments", "document_quality", "extraction_notes")


# --------------------------------------------------------------------------------------------- the schema

def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}


def _yes_no_unknown() -> dict[str, Any]:
    return {"type": "string", "enum": list(YES_NO_UNKNOWN)}


def wire_schema() -> dict[str, Any]:
    """A fresh copy of the response schema (no unions, no nulls, every property required, deliberately small)."""
    return _object({
        "fields": {"type": "array", "items": _object({
            "name": {"type": "string", "enum": list(EVIDENCED_FIELDS)},
            "found": {"type": "boolean"},
            "value": _STRING,
            "page": _INTEGER,
            "source_text": _STRING,
            "confidence": _NUMBER,
            "flag": _yes_no_unknown(),
        })},
        "line_items": {"type": "array", "items": _object({
            "description": _STRING, "item_code": _STRING, "quantity": _STRING, "unit_price": _STRING, "amount": _STRING,
            "page": _INTEGER, "source_text": _STRING, "confidence": _NUMBER,
        })},
        "adjustments": {"type": "array", "items": _object({
            "kind": {"type": "string", "enum": list(ADJUSTMENT_KINDS)}, "description": _STRING, "amount": _STRING,
            "page": _INTEGER, "source_text": _STRING, "confidence": _NUMBER,
        })},
        "document_quality": _object({
            "type": {"type": "string", "enum": ["scanned", "native", UNKNOWN]},
            "issues": {"type": "array", "items": _STRING},
            "contains_reader_instructions": _yes_no_unknown(),
        }),
        "extraction_notes": _STRING,
    })


# ------------------------------------------------------------------------------------- wire -> contract

def _text(obj: dict, key: str, where: str, default: str | None = "") -> str:
    """A string value. Numbers are accepted (and stringified) because a non-grammar-constrained reply often has them.
    A missing key falls back to `default`; None means the key is required."""
    if key not in obj:
        if default is None:
            raise ValueError(f"{where}: missing '{key}'")
        return default
    value = obj[key]
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{where}.{key}: expected a string")
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key}: expected a string")
    return value


def _page(obj: dict, where: str) -> int | None:
    page = obj.get("page", 0)
    if isinstance(page, float) and page.is_integer():
        page = int(page)
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        raise ValueError(f"{where}.page: expected an integer >= 0 (0 when unknown)")
    return page or None


def _confidence(obj: dict, where: str) -> float:
    conf = obj.get("confidence", 0.0)                   # a missing confidence is the safe value: 0
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ValueError(f"{where}.confidence: expected a number")
    return float(conf)


def _tri_state(obj: dict, key: str, where: str) -> bool | None:
    value = obj.get(key, UNKNOWN)
    mapping = {"yes": True, "no": False, "unknown": None}
    if value not in mapping:
        raise ValueError(f"{where}.{key}: expected yes, no or unknown")
    return mapping[value]


def _blank_to_none(text: str) -> str | None:
    return text if text.strip() else None


def _not_found(name: str) -> dict[str, Any]:
    out: dict[str, Any] = {"value": None, "page": None, "source_text": None, "confidence": 0.0}
    if name in _FLAGS:
        out[_FLAGS[name]] = None
    return out


def _entry_to_contract(name: str, entry: dict, notes: list[str]) -> dict[str, Any]:
    where = f"fields[{name}]"
    found = entry.get("found")
    if not isinstance(found, bool):
        raise ValueError(f"{where}.found: expected true or false")
    flag_value = _tri_state(entry, "flag", where)
    value = _text(entry, "value", where)
    page, conf = _page(entry, where), _confidence(entry, where)
    source = _text(entry, "source_text", where)
    if not found:
        return _not_found(name)                          # placeholders are ignored, whatever they contain
    if not value.strip() or (name == "document_type" and value == UNKNOWN):
        if not value.strip():
            notes.append(f"{name}: found=true but the value was empty; treated as not found")
        return _not_found(name)
    out = {"value": value, "page": page, "source_text": _blank_to_none(source), "confidence": conf}
    if name in _FLAGS:
        out[_FLAGS[name]] = flag_value
    return out


def _fields_to_contract(entries: Any, notes: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValueError("fields: expected an array")
    seen: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"fields[{i}]: expected an object")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ValueError(f"fields[{i}].name: expected a string")
        if name not in EVIDENCED_FIELDS:
            logger.warning("Ignoring unknown field name %r in the reply", name)     # unknown names are ignored, not fatal
            continue
        if name in seen:
            notes.append(f"{name}: appeared more than once in the reply; the first entry was kept")
            continue
        seen[name] = _entry_to_contract(name, entry, notes)
    return {name: seen.get(name) or _not_found(name) for name in EVIDENCED_FIELDS}      # missing names -> not found


def from_wire(reply: Any) -> tuple[dict[str, Any], list[str]]:
    """The model's reply (wire format) -> (contract-shaped dict, system notes). Raises ValueError for a reply that
    is structurally wrong, which the extractor turns into its single schema-repair retry."""
    if not isinstance(reply, dict):
        raise ValueError("the reply is not a JSON object")
    missing = [k for k in _TOP_LEVEL if k not in reply]
    if missing:
        raise ValueError(f"the reply is missing field(s): {', '.join(missing)}")
    notes: list[str] = []
    out: dict[str, Any] = _fields_to_contract(reply["fields"], notes)

    items = reply["line_items"]
    if not isinstance(items, list):
        raise ValueError("line_items: expected an array")
    out["line_items"] = []
    for i, item in enumerate(items):
        where = f"line_items[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: expected an object")
        out["line_items"].append({
            **{k: _blank_to_none(_text(item, k, where)) for k in ("description", "item_code", "quantity", "unit_price", "amount")},
            "page": _page(item, where), "source_text": _blank_to_none(_text(item, "source_text", where)),
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
            "kind": _text(adj, "kind", where, default="other"), "description": _blank_to_none(_text(adj, "description", where)),
            "amount": _blank_to_none(_text(adj, "amount", where)), "page": _page(adj, where),
            "source_text": _blank_to_none(_text(adj, "source_text", where)), "confidence": _confidence(adj, where)})

    quality = reply["document_quality"]
    if not isinstance(quality, dict):
        raise ValueError("document_quality: expected an object")
    q_type = _text(quality, "type", "document_quality", default=UNKNOWN)
    issues = quality.get("issues", [])
    if not isinstance(issues, list) or not all(isinstance(x, str) for x in issues):
        raise ValueError("document_quality.issues: expected an array of strings")
    out["document_quality"] = {
        "type": None if q_type == UNKNOWN else q_type, "issues": list(issues),
        "contains_reader_instructions": _tri_state(quality, "contains_reader_instructions", "document_quality")}

    out["extraction_notes"] = _blank_to_none(_text(reply, "extraction_notes", "reply"))
    return out, notes

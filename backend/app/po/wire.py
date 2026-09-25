"""The PO wire schema (what the API sees) and its converter. Same design as the invoice wire v3 (SPEC section 11 items 44-45):
zero unions, zero nulls, zero optional properties; header fields as ONE array of entries; placeholders when absent."""
from typing import Any

PO_FIELDS = ("vendor_name", "vendor_tax_id", "po_number", "issued_date", "currency", "total")
_TRI = {"type": "string", "enum": ["yes", "no", "unknown"]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def po_wire_schema() -> dict[str, Any]:
    entry = _object({"name": {"type": "string", "enum": list(PO_FIELDS)}, "found": {"type": "boolean"}, "value": {"type": "string"},
                     "page": {"type": "integer"}, "source_text": {"type": "string"}, "confidence": {"type": "number"}})
    line = _object({"description": {"type": "string"}, "quantity": {"type": "string"}, "unit_price": {"type": "string"},
                    "amount": {"type": "string"}, "page": {"type": "integer"}, "source_text": {"type": "string"},
                    "confidence": {"type": "number"}})
    return _object({"fields": {"type": "array", "items": entry}, "lines": {"type": "array", "items": line},
                    "other_pos_present": _TRI, "contains_reader_instructions": _TRI, "notes": {"type": "string"}})


def _text(obj: dict, key: str) -> str:
    v = obj.get(key, "")
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if not isinstance(v, str):
        raise ValueError(f"{key} must be a string")
    return v.strip()


def _page(obj: dict) -> int | None:
    v = obj.get("page", 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("page must be an integer")
    return int(v) if int(v) >= 1 else None


def _conf(obj: dict) -> float:
    v = obj.get("confidence", 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("confidence must be a number")
    return max(0.0, min(1.0, float(v)))


def _tri(v: Any) -> bool | None:
    return {"yes": True, "no": False}.get(str(v).strip().lower()) if v is not None else None


def from_po_wire(reply: Any) -> tuple[dict[str, Any], list[str]]:
    """Wire reply -> {fields: {name: {value, page, source_text, confidence}}, lines, other_pos_present, reader_instructions, notes}.

    A missing name is not found; a duplicate keeps the first (noted); unknown names are ignored (noted); found=true with an empty
    value is not found (noted). A structurally wrong reply raises ValueError (one repair retry)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("fields"), list) or not isinstance(reply.get("lines", []), list):
        raise ValueError("the reply must be an object with a 'fields' array and a 'lines' array")
    notes: list[str] = []
    fields: dict[str, dict[str, Any]] = {name: {"value": None, "page": None, "source_text": None, "confidence": 0.0, "model_confidence": 0.0}
                                         for name in PO_FIELDS}
    seen: set[str] = set()
    for entry in reply["fields"]:
        if not isinstance(entry, dict):
            raise ValueError("each field entry must be an object")
        name = entry.get("name")
        if name not in PO_FIELDS:
            notes.append(f"[system] ignored an unknown field {name!r}")
            continue
        if name in seen:
            notes.append(f"[system] {name} was returned twice; the first was kept")
            continue
        seen.add(name)
        if entry.get("found") is not True:
            continue
        value = _text(entry, "value")
        if not value:
            notes.append(f"[system] {name} was marked found with an empty value; treated as not found")
            continue
        conf = _conf(entry)
        fields[name] = {"value": value, "page": _page(entry), "source_text": _text(entry, "source_text") or None,
                        "confidence": conf, "model_confidence": conf}
    lines = []
    for raw in reply.get("lines", []):
        if not isinstance(raw, dict):
            raise ValueError("each line must be an object")
        line = {k: _text(raw, k) or None for k in ("description", "quantity", "unit_price", "amount")}
        if not any(line.values()):
            continue
        conf = _conf(raw)
        lines.append({**line, "page": _page(raw), "source_text": _text(raw, "source_text") or None, "confidence": conf,
                      "model_confidence": conf})
    return {"fields": fields, "lines": lines, "other_pos_present": _tri(reply.get("other_pos_present")),
            "reader_instructions": _tri(reply.get("contains_reader_instructions")), "notes": _text(reply, "notes") if "notes" in reply else ""}, notes

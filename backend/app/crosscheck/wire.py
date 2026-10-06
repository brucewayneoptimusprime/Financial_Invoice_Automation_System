"""The cross-check wire schema (what the API sees) and its converter. Same design as the invoice and PO wires (SPEC section 11
items 44-45): zero unions, zero nulls, zero optional properties; header fields as ONE array of entries with a `found` flag;
placeholders ("" / 0) when something is absent. 4 objects, 24 properties. No confidence: nothing here uses a threshold, and the
grounding check (app/crosscheck/reader.py) is what says whether a value is supported by the document."""
from typing import Any

FIELDS = ("document_type", "vendor_name", "currency", "total")
DOCUMENT_KINDS = ("delivery_note", "goods_receipt", "shipping_document", "invoice", "purchase_order", "other", "unknown")
MENTION_KINDS = ("po_number", "invoice_number", "date")
_LINE_TEXTS = ("description", "item_code", "quantity", "unit", "unit_price", "amount")
_TRI = {"type": "string", "enum": ["yes", "no", "unknown"]}
_STRING, _INTEGER = {"type": "string"}, {"type": "integer"}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def crosscheck_wire_schema() -> dict[str, Any]:
    """A fresh copy of the response schema."""
    field = _object({"name": {"type": "string", "enum": list(FIELDS)}, "found": {"type": "boolean"}, "value": dict(_STRING),
                     "page": dict(_INTEGER), "source_text": dict(_STRING)})
    mention = _object({"kind": {"type": "string", "enum": list(MENTION_KINDS)}, "label": dict(_STRING), "value": dict(_STRING),
                       "page": dict(_INTEGER), "source_text": dict(_STRING)})
    line = _object({**{k: dict(_STRING) for k in _LINE_TEXTS}, "page": dict(_INTEGER), "source_text": dict(_STRING)})
    return _object({"document_kind": {"type": "string", "enum": list(DOCUMENT_KINDS)},
                    "fields": {"type": "array", "items": field}, "mentions": {"type": "array", "items": mention},
                    "lines": {"type": "array", "items": line}, "contains_reader_instructions": dict(_TRI), "notes": dict(_STRING)})


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


def from_crosscheck_wire(reply: Any) -> tuple[dict[str, Any], list[str]]:
    """Wire reply -> ({document_kind, fields: {name: {value, page, source_text} | None}, mentions, lines, reader_instructions,
    notes}, system notes).

    Only the keys of the schema are read: anything else the reply carries is ignored. A missing field name is not found; a
    duplicate keeps the first; an unknown name or kind is ignored; found=true with an empty value is not found (each noted). A
    structurally wrong reply raises ValueError (one repair retry)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("fields"), list) or not isinstance(reply.get("lines"), list) \
            or not isinstance(reply.get("mentions"), list):
        raise ValueError("the reply must be an object with 'fields', 'mentions' and 'lines' arrays")
    notes: list[str] = []
    fields: dict[str, dict[str, Any] | None] = {name: None for name in FIELDS}
    seen: set[str] = set()
    for entry in reply["fields"]:
        if not isinstance(entry, dict):
            raise ValueError("each field entry must be an object")
        name = entry.get("name")
        if name not in FIELDS:
            notes.append(f"ignored an unknown field {str(name)[:40]!r}")
            continue
        if name in seen:
            notes.append(f"{name} was returned twice; the first was kept")
            continue
        seen.add(name)
        if entry.get("found") is not True:
            continue
        value = _text(entry, "value")
        if not value:
            notes.append(f"{name} was marked found with an empty value; treated as not found")
            continue
        fields[name] = {"value": value, "page": _page(entry), "source_text": _text(entry, "source_text") or None}
    mentions = []
    for raw in reply["mentions"]:
        if not isinstance(raw, dict):
            raise ValueError("each mention must be an object")
        kind, value = raw.get("kind"), _text(raw, "value")
        if kind not in MENTION_KINDS:
            notes.append(f"ignored a mention of unknown kind {str(kind)[:40]!r}")
            continue
        if not value:
            continue
        mentions.append({"kind": kind, "label": _text(raw, "label") or None, "value": value, "page": _page(raw),
                         "source_text": _text(raw, "source_text") or None})
    lines = []
    for raw in reply["lines"]:
        if not isinstance(raw, dict):
            raise ValueError("each line must be an object")
        line = {k: _text(raw, k) or None for k in _LINE_TEXTS}
        if not any(line.values()):
            continue
        lines.append({**line, "page": _page(raw), "source_text": _text(raw, "source_text") or None})
    kind = reply.get("document_kind")
    tri = str(reply.get("contains_reader_instructions", "unknown")).strip().lower()
    return {"document_kind": kind if kind in DOCUMENT_KINDS else "unknown", "fields": fields, "mentions": mentions, "lines": lines,
            "reader_instructions": {"yes": True, "no": False}.get(tri), "notes": _text(reply, "notes")}, notes

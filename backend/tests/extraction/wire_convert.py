"""Test-only inverse of app.extraction.wire.from_wire: a contract-shaped dict -> the wire format the model returns
(header fields as ONE array of entries). Used to build recorded-style fixtures and to prove the round trip."""
from typing import Any

from app.extraction.wire import EVIDENCED_FIELDS

_TRI = {True: "yes", False: "no", None: "unknown"}
_FLAG_KEY = {"po_reference": "explicit", "tax": "included_in_total"}


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def to_wire(contract: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for name in EVIDENCED_FIELDS:
        f = contract.get(name) or {}
        found = f.get("value") is not None
        flag_key = _FLAG_KEY.get(name)
        entries.append({
            "name": name,
            "found": found,
            "value": _s(f.get("value")) if found else ("unknown" if name == "document_type" else ""),
            "page": (f.get("page") or 0) if found else 0,
            "source_text": _s(f.get("source_text")) if found else "",
            "confidence": float(f.get("confidence") or 0.0) if found else 0.0,
            "flag": _TRI[f.get(flag_key)] if (found and flag_key) else "unknown",
        })
    out: dict[str, Any] = {"fields": entries}
    out["line_items"] = [{
        "description": _s(i.get("description")), "item_code": _s(i.get("item_code")), "quantity": _s(i.get("quantity")),
        "unit_price": _s(i.get("unit_price")), "amount": _s(i.get("amount")), "page": i.get("page") or 0,
        "source_text": _s(i.get("source_text")), "confidence": float(i.get("confidence") or 0.0)}
        for i in contract.get("line_items") or []]
    out["adjustments"] = [{
        "kind": a.get("kind") or "other", "description": _s(a.get("description")), "amount": _s(a.get("amount")),
        "page": a.get("page") or 0, "source_text": _s(a.get("source_text")), "confidence": float(a.get("confidence") or 0.0)}
        for a in contract.get("adjustments") or []]
    q = contract.get("document_quality") or {}
    out["document_quality"] = {"type": q.get("type") or "unknown", "issues": list(q.get("issues") or []),
                               "contains_reader_instructions": _TRI[q.get("contains_reader_instructions")]}
    out["extraction_notes"] = _s(contract.get("extraction_notes"))
    return out


# ------------------------------------------------------------------------------ helpers for editing wire replies

def entry(reply: dict[str, Any], name: str) -> dict[str, Any]:
    """The entry for `name` in a wire reply (mutable)."""
    return next(e for e in reply["fields"] if e["name"] == name)


def set_field(reply: dict[str, Any], name: str, **changes: Any) -> dict[str, Any]:
    entry(reply, name).update(changes)
    return reply


def drop_field(reply: dict[str, Any], name: str) -> dict[str, Any]:
    reply["fields"] = [e for e in reply["fields"] if e["name"] != name]
    return reply

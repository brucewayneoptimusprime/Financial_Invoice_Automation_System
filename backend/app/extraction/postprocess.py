"""Turn the model's JSON (already parsed) into a validated ExtractedInvoice, applying the deterministic fixes the
contract promises:

  * currency symbols mapped to ISO codes by config (ambiguous symbols stay unmapped -> null + note); a symbol-derived
    currency gets its effective confidence from config (currency_symbol_confidence), the model's score is kept
  * amounts cleaned into exact decimal strings (US / European / Indian digit grouping, brackets, currency marks)
  * dates normalised to ISO when unambiguous; an ambiguous day/month order caps that field's confidence at 0.5
  * adjustment signs applied by kind (discount/credit subtract, shipping/fee add, rounding/other keep the sign)
  * null values forced to confidence 0; the model's own confidence kept in `model_confidence`

Raises pydantic.ValidationError (or ValueError) for a reply that cannot be salvaged; the extractor turns that into
the single schema-repair retry.
"""
import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.config import Settings, get_settings
from app.extraction.parsing import is_ambiguous_dmy, map_currency, normalize_amount_string, normalize_date_string
from app.models.extraction import EvidencedField, ExtractedInvoice

AMBIGUOUS_DATE_CAP = 0.5
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING_NUMBER = re.compile(r"^\s*([\d.,]+)")
_EVIDENCED = ("vendor_name", "vendor_tax_id", "vendor_address", "document_type", "invoice_number", "invoice_date",
              "currency", "po_reference", "subtotal", "tax", "total")


@dataclass
class PostprocessResult:
    invoice: ExtractedInvoice
    notes: list[str] = field(default_factory=list)          # system notes (also appended to extraction_notes)


def _to_amount_string(value: Any, *, leading_number: bool = False) -> Any:
    """A cleaned decimal string, or the original value untouched if it cannot be read (validation will complain)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(Decimal(str(value)))
    text = str(value)
    cleaned = normalize_amount_string(text)
    if cleaned is None and leading_number and (m := _LEADING_NUMBER.match(text)):
        cleaned = normalize_amount_string(m.group(1))
    return text if cleaned is None else cleaned


def _normalise_currency(data: dict, settings: Settings, notes: list[str]) -> None:
    cur = data.get("currency")
    if not isinstance(cur, dict) or cur.get("value") is None:
        return
    raw = str(cur["value"])
    code, note = map_currency(raw, settings.currency_symbol_map)
    if code is None:
        cur["value"], cur["confidence"] = None, 0.0
    else:
        cur["value"] = code
    if note:
        notes.append(note)
    if code is not None and note:                        # mapped from a symbol, not read as a code
        _set_symbol_confidence(cur, settings, notes)


def _set_symbol_confidence(cur: dict, settings: Settings, notes: list[str]) -> None:
    """A symbol-derived currency is a MAPPING (config), not a reading, so the model's doubt about a bare '$' says little:
    its effective confidence comes from config. The model's raw score stays in model_confidence. A model that reported
    0 (or omitted the score) said it does not trust the value, and that is respected."""
    raw = cur.get("confidence")
    raw = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0
    cur["model_confidence"] = raw
    if raw <= 0:
        return
    cur["confidence"] = settings.currency_symbol_confidence
    notes.append(f"currency confidence set to {settings.currency_symbol_confidence:g} from configuration "
                 f"(symbol-derived; the model reported {raw:g})")


def _normalise_money(data: dict) -> None:
    for name in ("subtotal", "tax", "total"):
        f = data.get(name)
        if isinstance(f, dict):
            f["value"] = _to_amount_string(f.get("value"))
    for line in data.get("line_items") or []:
        if isinstance(line, dict):
            line["amount"] = _to_amount_string(line.get("amount"))
            line["unit_price"] = _to_amount_string(line.get("unit_price"))
            line["quantity"] = _to_amount_string(line.get("quantity"), leading_number=True)
    for adj in data.get("adjustments") or []:
        if isinstance(adj, dict):
            adj["amount"] = _to_amount_string(adj.get("amount"))


def _normalise_date(data: dict) -> None:
    f = data.get("invoice_date")
    if isinstance(f, dict) and isinstance(f.get("value"), str) and not _ISO_DATE.match(f["value"].strip()):
        iso = normalize_date_string(f["value"])
        if iso:
            f["value"] = iso


def _apply_adjustment_signs(invoice: ExtractedInvoice) -> None:
    for adj in invoice.adjustments:
        if adj.amount is None:
            continue
        if adj.printed_amount is None:
            adj.printed_amount = adj.amount                     # keep what the model returned
        printed = adj.printed_amount
        if adj.kind in ("discount", "credit"):
            adj.amount = -abs(printed)
        elif adj.kind in ("shipping", "fee"):
            adj.amount = abs(printed)
        else:                                                   # rounding / other / unknown: the printed sign stands
            adj.amount = printed


def _record_model_confidence(invoice: ExtractedInvoice) -> None:
    for name in _EVIDENCED:
        f: EvidencedField = getattr(invoice, name)
        if f.value is None:
            f.confidence, f.page, f.source_text = 0.0, None, None      # null is missing, whatever the model claimed
        if f.model_confidence is None:
            f.model_confidence = f.confidence                          # the model's raw value (0 for null)
    for item in [*invoice.line_items, *invoice.adjustments]:
        if item.model_confidence is None:
            item.model_confidence = item.confidence


def _cap_ambiguous_date(invoice: ExtractedInvoice, notes: list[str]) -> None:
    f = invoice.invoice_date
    if f.value is not None and f.source_text and is_ambiguous_dmy(f.source_text):
        f.confidence = min(f.confidence, AMBIGUOUS_DATE_CAP)
        notes.append(f"invoice_date {f.source_text!r} has an ambiguous day/month order; read as {f.value.isoformat()} "
                     f"(confidence capped at {AMBIGUOUS_DATE_CAP})")


def postprocess(raw: Any, settings: Settings | None = None, extra_notes: list[str] | None = None) -> PostprocessResult:
    settings = settings or get_settings()
    if not isinstance(raw, dict):
        raise ValueError("the reply is not a JSON object")
    data = deepcopy(raw)
    notes: list[str] = list(extra_notes or [])            # e.g. notes from the wire converter
    _normalise_currency(data, settings, notes)
    _normalise_money(data)
    _normalise_date(data)
    invoice = ExtractedInvoice.model_validate(data)
    _apply_adjustment_signs(invoice)
    _record_model_confidence(invoice)
    _cap_ambiguous_date(invoice, notes)
    if notes:
        system = "\n".join(f"[system] {n}" for n in notes)
        invoice.extraction_notes = f"{invoice.extraction_notes}\n{system}" if invoice.extraction_notes else system
    return PostprocessResult(invoice=invoice, notes=notes)

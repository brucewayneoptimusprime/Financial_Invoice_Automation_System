"""Hand-written, generic fixtures for engine tests. Nothing here comes from data/seed.json or a real
invoice: names and numbers are invented and deliberately vendor-agnostic."""
from datetime import date
from decimal import Decimal
from typing import Any

from app.engine.facts import POFact, POLineFact, PriorInvoiceFact, RunFacts, RuntimeSettings, VendorFact
from app.enums import InvoiceStatus, MatchStatus, POStatus, VendorStatus
from app.models import ExtractedInvoice, POCandidate, RunContext
from app.models.run import VendorMatch

D = Decimal
_UNSET = object()


def field(value: Any, conf: float = 0.95, **extra: Any) -> dict:
    """An evidenced-field dict. `field(None, 0.99)` is a null value with HIGH confidence."""
    return {"value": value, "page": None if value is None else 1,
            "source_text": None if value is None else str(value), "confidence": conf, **extra}


def null_field(conf: float = 0.0) -> dict:
    return field(None, conf)


DEFAULT_LINES = [
    {"description": "Standard widget", "quantity": 10, "unit_price": "60.00", "amount": "600.00", "confidence": 0.9},
    {"description": "Premium gadget", "quantity": 5, "unit_price": "80.00", "amount": "400.00", "confidence": 0.9},
]


def make_extracted(**overrides: Any) -> ExtractedInvoice:
    """A fully valid invoice: subtotal 1000 + tax 100 = total 1100, two lines summing to 1000.

    Override a field with a scalar (-> confident field), a dict (used as-is), or None (-> null, conf 0).
    """
    raw: dict[str, Any] = {
        "vendor_name": field("Vendor Alpha Ltd"),
        "invoice_number": field("INV-1001"),
        "invoice_date": field("2026-03-14"),
        "currency": field("USD"),
        "po_reference": field("PO-A-1", explicit=True),
        "subtotal": field("1000.00"),
        "tax": field("100.00", included_in_total=False),
        "total": field("1100.00"),
        "line_items": DEFAULT_LINES,
        "document_quality": {"type": "native", "issues": []},
        "extraction_notes": None,
    }
    for key, value in overrides.items():
        if key in ("line_items", "document_quality", "extraction_notes"):
            raw[key] = value
        elif isinstance(value, dict):
            raw[key] = value
        elif value is None:
            raw[key] = null_field()
        else:
            extra = {"explicit": True} if key == "po_reference" else {}
            raw[key] = field(value, **extra)
    return ExtractedInvoice.model_validate(raw)


def make_vendor(id: int = 1, name: str = "Vendor Alpha Ltd", aliases: tuple[str, ...] = (),
                status: VendorStatus = VendorStatus.APPROVED) -> VendorFact:
    return VendorFact(id=id, name=name, aliases=aliases, status=status)


def make_po(id: int = 1, po_number: str = "PO-A-1", vendor_id: int = 1, currency: str = "USD",
            total: str = "1200.00", net_committed: str = "0.00", status: POStatus = POStatus.OPEN,
            lines: tuple[POLineFact, ...] | None = None) -> POFact:
    if lines is None:
        lines = (
            POLineFact(line_no=1, description="Standard widget", quantity=D(10), unit_price=D("60.00"), amount=D("600.00")),
            POLineFact(line_no=2, description="Premium gadget", quantity=D(5), unit_price=D("80.00"), amount=D("400.00")),
        )
    return POFact(id=id, po_number=po_number, vendor_id=vendor_id, currency=currency, total_amount=D(total),
                  net_committed=D(net_committed), status=status, lines=lines)


def make_prior(id: int = 100, vendor_id: int | None = 1, invoice_number: str | None = "INV-0999",
               invoice_date: date | None = date(2026, 2, 1), total: str | None = "500.00", po_id: int | None = 1,
               status: InvoiceStatus = InvoiceStatus.APPROVED, file_hash: str | None = "hash-prior",
               run_id: str | None = None, currency: str | None = "USD") -> PriorInvoiceFact:
    return PriorInvoiceFact(id=id, run_id=run_id, vendor_id=vendor_id, invoice_number=invoice_number,
                            invoice_date=invoice_date, currency=currency, total=None if total is None else D(total),
                            po_id=po_id, status=status, file_hash=file_hash)


def make_facts(vendors: list[VendorFact] | None = None, pos: list[POFact] | None = None,
               priors: list[PriorInvoiceFact] | None = None, threshold: float = 0.8) -> RunFacts:
    return RunFacts(
        vendors=tuple(vendors if vendors is not None else [make_vendor()]),
        purchase_orders=tuple(pos if pos is not None else [make_po()]),
        prior_invoices=tuple(priors or ()),
        settings=RuntimeSettings(confidence_threshold=threshold),
    )


def make_ctx(extracted: ExtractedInvoice | None | object = _UNSET, facts: RunFacts | None | object = _UNSET,
             matched: bool = True, match_status: MatchStatus | None | object = _UNSET,
             vendor_id: int | None = 1, run_id: str = "run-test", file_hash: str | None = "hash-current") -> RunContext:
    """A context in the 'everything matched' state by default. Pass matched=False for no PO match."""
    facts = make_facts() if facts is _UNSET else facts
    extracted = make_extracted() if extracted is _UNSET else extracted
    if match_status is _UNSET:
        match_status = MatchStatus.MATCHED if matched else MatchStatus.NO_CANDIDATES
    po = facts.purchase_orders[0] if facts is not None and facts.purchase_orders else None
    candidate = POCandidate(po_id=po.id, po_number=po.po_number, score=0.9,
                            reasons=["reference:exact"]) if (matched and po) else None
    return RunContext(
        run_id=run_id, source_file="generic-invoice.pdf", file_hash=file_hash, extracted=extracted, facts=facts,
        matched_vendor=None if vendor_id is None else VendorMatch(vendor_id=vendor_id, score=1.0, method="exact_name"),
        matched_po=candidate, candidates=[candidate] if candidate else [], match_status=match_status,
    )

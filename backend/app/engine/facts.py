"""RunFacts: the read-only snapshot of procurement state that evaluators reason over.

Taken once per run by engine/loader.py (the only module that touches SQLite) and never mutated:
every model here is frozen and uses tuples, so an evaluator cannot change it. Tests build facts by hand.
"""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.enums import InvoiceStatus, POStatus, VendorStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VendorFact(_Frozen):
    id: int
    name: str
    aliases: tuple[str, ...] = ()
    tax_id: str | None = None
    country: str | None = None
    status: VendorStatus


class POLineFact(_Frozen):
    line_no: int
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class POFact(_Frozen):
    id: int
    po_number: str
    vendor_id: int
    currency: str
    total_amount: Decimal
    status: POStatus
    net_committed: Decimal = Decimal("0")  # SUM(ledger_entries.amount): commits minus reversals
    lines: tuple[POLineFact, ...] = ()

    @property
    def balance(self) -> Decimal:
        """Remaining balance = total minus ledger sum. Derived, never stored (SPEC principle 8)."""
        return self.total_amount - self.net_committed


class PriorInvoiceFact(_Frozen):
    id: int
    run_id: str | None = None
    vendor_id: int | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = None
    total: Decimal | None = None
    po_id: int | None = None
    status: InvoiceStatus
    file_hash: str | None = None


class RuntimeSettings(_Frozen):
    """Values from the `settings` table (non-rule runtime values only)."""

    confidence_threshold: float = Field(ge=0.0, le=1.0)


class RunFacts(_Frozen):
    vendors: tuple[VendorFact, ...] = ()
    purchase_orders: tuple[POFact, ...] = ()
    prior_invoices: tuple[PriorInvoiceFact, ...] = ()
    settings: RuntimeSettings

    def po_by_id(self, po_id: int) -> POFact | None:
        return next((po for po in self.purchase_orders if po.id == po_id), None)

    def vendor_by_id(self, vendor_id: int) -> VendorFact | None:
        return next((v for v in self.vendors if v.id == vendor_id), None)

"""Load data/seed.json into an initialised database.

The file is validated with strict Pydantic models first (typos fail loudly), then written in a
single transaction. Seed records carry explicit ids and created_at so a reset reproduces the
exact same state every time.
"""
import json
import logging
import sqlite3
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.config import get_settings
from app.enums import Decision, InvoiceStatus, LedgerType, POStatus, VendorStatus
from app.money import to_minor

logger = logging.getLogger(__name__)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SeedLine(_Strict):
    line_no: int
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class SeedVendor(_Strict):
    id: int
    name: str
    aliases: list[str] = Field(default_factory=list)
    tax_id: str | None = None
    country: str | None = None
    status: VendorStatus
    created_at: AwareDatetime


class SeedPO(_Strict):
    id: int
    po_number: str
    vendor_id: int
    currency: str
    total_amount: Decimal
    issued_date: date | None = None
    status: POStatus
    meta: dict = Field(default_factory=dict)
    lines: list[SeedLine] = Field(default_factory=list)


class SeedInvoice(_Strict):
    id: int
    vendor_id: int | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    po_id: int | None = None
    decision: Decision | None = None
    status: InvoiceStatus
    source_file: str | None = None
    file_hash: str | None = None
    created_at: AwareDatetime
    lines: list[SeedLine] = Field(default_factory=list)


class SeedLedgerEntry(_Strict):
    id: int
    po_id: int
    invoice_id: int
    amount: Decimal
    type: LedgerType
    created_at: AwareDatetime


class SeedFile(_Strict):
    """A seed is EITHER the M0 placeholder (`_PLACEHOLDER` notice: not real, only for schema tests) OR a demo dataset
    (`_DATASET` description: hand-written data built around the real sample invoices). Exactly one of the two."""

    placeholder_notice: str | None = Field(default=None, alias="_PLACEHOLDER", min_length=1)
    dataset_notice: str | None = Field(default=None, alias="_DATASET", min_length=1)
    vendors: list[SeedVendor] = Field(default_factory=list)
    purchase_orders: list[SeedPO] = Field(default_factory=list)
    invoices: list[SeedInvoice] = Field(default_factory=list)
    ledger_entries: list[SeedLedgerEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_notice(self) -> "SeedFile":
        if (self.placeholder_notice is None) == (self.dataset_notice is None):
            raise ValueError("a seed file needs exactly one of _PLACEHOLDER (placeholder data) or _DATASET (demo data)")
        return self

    @property
    def kind(self) -> str:
        return "placeholder" if self.placeholder_notice is not None else "demo"

    @property
    def notice(self) -> str:
        return self.placeholder_notice or self.dataset_notice or ""


def _minor(value: Decimal | None) -> int | None:
    return None if value is None else to_minor(value)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _ts(value: AwareDatetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_seed(path: Path) -> SeedFile:
    return SeedFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_seed(conn: sqlite3.Connection, path: Path | None = None) -> SeedFile:
    """Insert the seed file into an empty, initialised DB. All-or-nothing."""
    path = Path(path) if path is not None else get_settings().seed_path
    seed = parse_seed(path)
    (logger.warning if seed.kind == "placeholder" else logger.info)("Loading %s seed %s - %s", seed.kind, path.name, seed.notice)

    with conn:
        for v in seed.vendors:
            conn.execute(
                "INSERT INTO vendors (id, name, aliases, tax_id, country, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (v.id, v.name, json.dumps(v.aliases), v.tax_id, v.country, v.status.value, _ts(v.created_at)),
            )
        for po in seed.purchase_orders:
            conn.execute(
                "INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, issued_date, status, meta) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    po.id, po.po_number, po.vendor_id, po.currency, to_minor(po.total_amount),
                    po.issued_date.isoformat() if po.issued_date else None, po.status.value, json.dumps(po.meta),
                ),
            )
            for ln in po.lines:
                conn.execute(
                    "INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) VALUES (?,?,?,?,?,?)",
                    (po.id, ln.line_no, ln.description, _text(ln.quantity), _text(ln.unit_price), _minor(ln.amount)),
                )
        for inv in seed.invoices:
            conn.execute(
                "INSERT INTO invoices (id, vendor_id, invoice_number, invoice_date, currency, subtotal, tax, total, "
                "po_id, decision, status, source_file, file_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    inv.id, inv.vendor_id, inv.invoice_number,
                    inv.invoice_date.isoformat() if inv.invoice_date else None, inv.currency,
                    _minor(inv.subtotal), _minor(inv.tax), _minor(inv.total), inv.po_id,
                    inv.decision.value if inv.decision else None, inv.status.value,
                    inv.source_file, inv.file_hash, _ts(inv.created_at),
                ),
            )
            for ln in inv.lines:
                conn.execute(
                    "INSERT INTO invoice_lines (invoice_id, line_no, description, quantity, unit_price, amount) "
                    "VALUES (?,?,?,?,?,?)",
                    (inv.id, ln.line_no, ln.description, _text(ln.quantity), _text(ln.unit_price), _minor(ln.amount)),
                )
        for e in seed.ledger_entries:
            conn.execute(
                "INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type, created_at) VALUES (?,?,?,?,?,?)",
                (e.id, e.po_id, e.invoice_id, to_minor(e.amount), e.type.value, _ts(e.created_at)),
            )
    return seed

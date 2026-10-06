"""What the cross-check knows about one purchase order. READ-ONLY SQL on a connection opened read-only: nothing here can write.

Invoiced quantity per PO line (owner decision 3): every invoice matched to the PO that is not rejected counts, and each is named
with its status. Per invoice: its line-assigned allocation in `po_consumption` when it has one (commits minus reversals; this is
what an approval, automatic or by a reviewer, actually recorded); otherwise its automatic line matches (`invoice_line_matches`
with status matched) and the quantity printed on those invoice lines. Quantities are decimal TEXT and are added here, never in SQL.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

COUNTED_OUT = ("rejected",)


@dataclass(frozen=True)
class POLine:
    id: int
    line_no: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount_minor: int | None


@dataclass(frozen=True)
class Invoiced:
    invoice_number: str | None
    status: str
    quantity: Decimal
    basis: str                                   # allocated | line_match


@dataclass(frozen=True)
class POContext:
    po_id: int
    po_number: str
    currency: str
    total_minor: int
    vendor_name: str
    vendor_aliases: tuple[str, ...]
    lines: tuple[POLine, ...]
    invoices: tuple[tuple[str | None, str], ...] = ()                  # (invoice number, status), not rejected
    invoiced: dict[int, tuple[Invoiced, ...]] = field(default_factory=dict)   # po_lines.id -> who invoiced how much


def open_readonly(db_path: Path, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """A connection that CANNOT write: SQLite opens the file read-only, and query_only refuses any write statement as well."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _dec(text) -> Decimal | None:
    if text is None:
        return None
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def load_po_context(conn: sqlite3.Connection, po_id: int) -> POContext | None:
    po = conn.execute("SELECT po.id, po.po_number, po.currency, po.total_amount, v.name AS vendor, v.aliases FROM purchase_orders po "
                      "JOIN vendors v ON v.id = po.vendor_id WHERE po.id = ?", (po_id,)).fetchone()
    if po is None:
        return None
    lines = tuple(POLine(id=r["id"], line_no=r["line_no"], description=r["description"], quantity=_dec(r["quantity"]),
                         unit_price=_dec(r["unit_price"]), amount_minor=r["amount"])
                  for r in conn.execute("SELECT * FROM po_lines WHERE po_id = ? ORDER BY line_no", (po_id,)))
    marks = ",".join("?" * len(COUNTED_OUT))
    invoices = conn.execute(f"SELECT id, invoice_number, status FROM invoices WHERE po_id = ? AND status NOT IN ({marks}) ORDER BY id",
                            (po_id, *COUNTED_OUT)).fetchall()
    invoiced: dict[int, list[Invoiced]] = {}
    for inv in invoices:
        per_line: dict[int, Decimal] = {}
        for r in conn.execute("SELECT po_line_id, quantity, amount FROM po_consumption WHERE invoice_id = ? AND po_id = ? "
                              "AND po_line_id IS NOT NULL ORDER BY id", (inv["id"], po_id)):
            qty = _dec(r["quantity"])
            if qty is not None:
                per_line[r["po_line_id"]] = per_line.get(r["po_line_id"], Decimal(0)) + (qty if r["amount"] > 0 else -abs(qty))
        basis = "allocated"
        if not per_line:
            basis = "line_match"
            for r in conn.execute("SELECT m.po_line_id, l.quantity FROM invoice_line_matches m JOIN invoice_lines l ON l.id = m.invoice_line_id "
                                  "WHERE m.invoice_id = ? AND m.po_id = ? AND m.status = 'matched' AND m.po_line_id IS NOT NULL ORDER BY m.id",
                                  (inv["id"], po_id)):
                qty = _dec(r["quantity"])
                if qty is not None:
                    per_line[r["po_line_id"]] = per_line.get(r["po_line_id"], Decimal(0)) + qty
        for line_id, qty in per_line.items():
            if qty != 0:
                invoiced.setdefault(line_id, []).append(Invoiced(inv["invoice_number"], inv["status"], qty, basis))
    return POContext(po_id=po["id"], po_number=po["po_number"], currency=po["currency"], total_minor=po["total_amount"],
                     vendor_name=po["vendor"], vendor_aliases=tuple(json.loads(po["aliases"] or "[]")), lines=lines,
                     invoices=tuple((r["invoice_number"], r["status"]) for r in invoices),
                     invoiced={k: tuple(v) for k, v in invoiced.items()})

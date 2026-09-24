"""Read helpers. PO balance is always derived here from the ledger - never stored."""
import sqlite3
from decimal import Decimal

from app.money import from_minor


def get_po_balance_minor(conn: sqlite3.Connection, po_id: int) -> int:
    """PO total minus the sum of its ledger entries (commits positive, reversals negative)."""
    row = conn.execute(
        "SELECT po.total_amount - COALESCE(SUM(l.amount), 0) AS balance "
        "FROM purchase_orders po LEFT JOIN ledger_entries l ON l.po_id = po.id "
        "WHERE po.id = ? GROUP BY po.id",
        (po_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"purchase order {po_id} not found")
    return row["balance"]


def get_po_balance(conn: sqlite3.Connection, po_id: int) -> Decimal:
    return from_minor(get_po_balance_minor(conn, po_id))

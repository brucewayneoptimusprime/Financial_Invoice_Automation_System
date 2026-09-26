"""PO consumption allocation (schema v2): how each ledger entry is split over PO lines or left against the PO total.

The ledger stays the ONLY source of a PO's balance. These helpers keep the allocation complete: every ledger entry's
consumption rows add up to exactly the entry's amount.
"""
import sqlite3
from typing import Iterable

from app.enums import MatchedBy


def backfill_consumption(conn: sqlite3.Connection, matched_by: MatchedBy = MatchedBy.LEGACY) -> int:
    """One total-only row (po_line_id NULL) for every ledger entry that has no consumption rows yet. Returns how many.

    Used by the v1 -> v2 migration and the seed loader. Old whole-PO commits become 'deducted from the PO total, no line'.
    """
    cur = conn.execute(
        "INSERT INTO po_consumption (ledger_entry_id, po_id, po_line_id, invoice_id, invoice_line_id, run_id, quantity, amount, "
        "type, matched_by, created_at) "
        "SELECT le.id, le.po_id, NULL, le.invoice_id, NULL, i.run_id, NULL, le.amount, le.type, ?, le.created_at "
        "FROM ledger_entries le JOIN invoices i ON i.id = le.invoice_id "
        "WHERE NOT EXISTS (SELECT 1 FROM po_consumption pc WHERE pc.ledger_entry_id = le.id) ORDER BY le.id",
        (matched_by.value,))
    return cur.rowcount


def consumption_problems(conn: sqlite3.Connection) -> list[str]:
    """Every way the allocation could be incomplete or wrong; empty when it is consistent."""
    problems: list[str] = []
    for r in conn.execute(
            "SELECT le.id, le.amount, le.po_id, COALESCE(SUM(pc.amount), 0) AS allocated, COUNT(pc.id) AS n, "
            "SUM(CASE WHEN pc.po_id <> le.po_id OR pc.type <> le.type THEN 1 ELSE 0 END) AS mismatched "
            "FROM ledger_entries le LEFT JOIN po_consumption pc ON pc.ledger_entry_id = le.id GROUP BY le.id"):
        if r["n"] == 0:
            problems.append(f"ledger entry {r['id']} has no consumption rows")
        elif r["allocated"] != r["amount"]:
            problems.append(f"ledger entry {r['id']}: consumption {r['allocated']} != ledger amount {r['amount']}")
        if r["mismatched"]:
            problems.append(f"ledger entry {r['id']}: a consumption row has another PO or type")
    for r in conn.execute("SELECT pc.id FROM po_consumption pc LEFT JOIN ledger_entries le ON le.id = pc.ledger_entry_id "
                          "WHERE le.id IS NULL"):
        problems.append(f"consumption row {r['id']} has no ledger entry")
    for r in conn.execute("SELECT pc.id FROM po_consumption pc JOIN po_lines pl ON pl.id = pc.po_line_id WHERE pl.po_id <> pc.po_id"):
        problems.append(f"consumption row {r['id']} points at a line of another PO")
    return problems


def record_consumption(conn: sqlite3.Connection, *, ledger_entry_id: int, po_id: int, invoice_id: int, run_id: str | None,
                       amount_minor: int, entry_type: str = "commit", matched_by: MatchedBy = MatchedBy.AUTO,
                       po_line_id: int | None = None, invoice_line_id: int | None = None, quantity: str | None = None) -> int:
    return conn.execute(
        "INSERT INTO po_consumption (ledger_entry_id, po_id, po_line_id, invoice_id, invoice_line_id, run_id, quantity, amount, type, "
        "matched_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ledger_entry_id, po_id, po_line_id, invoice_id, invoice_line_id, run_id, quantity, amount_minor, entry_type,
         matched_by.value)).lastrowid


def schema_statements(sql: str) -> Iterable[str]:
    """Split a schema file into statements (each ends with ';' at the end of a line; comments removed)."""
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            yield "\n".join(buf)
            buf = []
    if buf:
        yield "\n".join(buf)

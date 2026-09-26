"""PO list and PO detail read models (read-only SQL; no new decision path). Money leaves as exact decimal strings.

The balance is always derived from the ledger (`get_po_balance_minor`), never read from a stored column.
"""
import json
import sqlite3
from decimal import Decimal
from typing import Any

from app.db.consumption import po_consumption_summary
from app.db.queries import get_po_balance_minor
from app.money import from_minor

_STATUSES = ("open", "partially_billed", "fully_billed", "closed")


def _m(minor: int | None) -> str | None:
    return None if minor is None else str(from_minor(minor))


def vendors(conn: sqlite3.Connection) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "tax_id": r["tax_id"], "country": r["country"]}
            for r in conn.execute("SELECT id, name, status, tax_id, country FROM vendors ORDER BY name COLLATE NOCASE, id")]


def po_list(conn: sqlite3.Connection, q: str | None = None, status: str | None = None) -> list[dict]:
    sql = ("SELECT po.id, po.po_number, po.currency, po.total_amount, po.issued_date, po.status, po.vendor_id, v.name AS vendor, "
           "v.status AS vendor_status, po.total_amount - COALESCE((SELECT SUM(amount) FROM ledger_entries l WHERE l.po_id = po.id), 0) "
           "AS balance, (SELECT COUNT(*) FROM invoices i WHERE i.po_id = po.id) AS invoice_count, po.meta "
           "FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id")
    where, args = [], []
    if q:
        where.append("(po.po_number LIKE ? ESCAPE '\\' OR v.name LIKE ? ESCAPE '\\')")
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    if status in _STATUSES:
        where.append("po.status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY po.id DESC"
    out = []
    for r in conn.execute(sql, args):
        meta = json.loads(r["meta"] or "{}")
        out.append({"id": r["id"], "po_number": r["po_number"], "vendor_id": r["vendor_id"], "vendor": r["vendor"],
                    "vendor_status": r["vendor_status"], "currency": r["currency"], "total": _m(r["total_amount"]),
                    "balance": _m(r["balance"]), "issued_date": r["issued_date"], "status": r["status"],
                    "invoice_count": r["invoice_count"], "source": meta.get("source", "seed" if meta.get("demo") else None)})
    return out


def _considered(conn: sqlite3.Connection, po_id: int, po_number: str) -> list[dict]:
    """Runs where this PO was ranked as a candidate but the invoice was NOT matched to it (read from the stored events)."""
    rows = conn.execute(
        "SELECT a.run_id, json_extract(c.value, '$.score') AS score, r.source_file, r.started_at, r.final_decision, r.status "
        "FROM audit_events a, json_each(a.detail, '$.candidates') c JOIN runs r ON r.id = a.run_id "
        "WHERE a.event_type = 'po_candidates_ranked' AND json_extract(c.value, '$.po_number') = ? "
        "AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.run_id = a.run_id AND i.po_id = ?) "
        "ORDER BY r.started_at DESC, a.run_id LIMIT 50", (po_number, po_id)).fetchall()
    out = []
    for r in rows:
        dec = conn.execute("SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'po_match_decision'", (r["run_id"],)).fetchone()
        d = json.loads(dec["detail"]) if dec else {}
        out.append({"run_id": r["run_id"], "source_file": r["source_file"], "started_at": r["started_at"], "score": r["score"],
                    "decision": r["final_decision"], "run_status": r["status"], "match_status": d.get("match_status"),
                    "matched_po": d.get("matched_po")})
    return out


def po_detail(conn: sqlite3.Connection, po_id: int) -> dict[str, Any] | None:
    po = conn.execute("SELECT po.*, v.name AS vendor, v.status AS vendor_status, v.tax_id AS vendor_tax_id "
                      "FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id WHERE po.id = ?", (po_id,)).fetchone()
    if po is None:
        return None
    balance = get_po_balance_minor(conn, po_id)
    committed = po["total_amount"] - balance
    lines, split = po_lines_with_consumption(conn, po_id)
    invoices = []
    awaiting = 0
    for r in conn.execute(
            "SELECT i.id, i.run_id, i.invoice_number, i.invoice_date, i.currency, i.total, i.decision, i.status, i.source_file, "
            "i.created_at, r.status AS run_status, r.final_decision, r.cost_usd, r.started_at "
            "FROM invoices i LEFT JOIN runs r ON r.id = i.run_id WHERE i.po_id = ? ORDER BY i.created_at DESC, i.id DESC", (po_id,)):
        if r["status"] == "in_review" and r["total"] is not None:
            awaiting += r["total"]
        invoices.append({"invoice_id": r["id"], "run_id": r["run_id"], "historic": r["run_id"] is None,
                         "invoice_number": r["invoice_number"], "invoice_date": r["invoice_date"], "currency": r["currency"],
                         "total": _m(r["total"]), "decision": r["decision"], "status": r["status"], "source_file": r["source_file"],
                         "run_status": r["run_status"], "cost_usd": r["cost_usd"], "started_at": r["started_at"] or r["created_at"]})
    ledger = [{"id": r["id"], "type": r["type"], "amount": _m(r["amount"]), "invoice_id": r["invoice_id"], "created_at": r["created_at"]}
              for r in conn.execute("SELECT * FROM ledger_entries WHERE po_id = ? ORDER BY created_at, id", (po_id,))]
    allocations = [{"id": r["id"], "ledger_entry_id": r["ledger_entry_id"], "po_line_id": r["po_line_id"], "po_line_no": r["line_no"],
                    "invoice_id": r["invoice_id"], "invoice_number": r["invoice_number"], "run_id": r["run_id"], "amount": _m(r["amount"]),
                    "quantity": r["quantity"], "type": r["type"], "matched_by": r["matched_by"], "created_at": r["created_at"]}
                   for r in conn.execute(
                       "SELECT pc.*, pl.line_no, i.invoice_number FROM po_consumption pc LEFT JOIN po_lines pl ON pl.id = pc.po_line_id "
                       "LEFT JOIN invoices i ON i.id = pc.invoice_id WHERE pc.po_id = ? ORDER BY pc.ledger_entry_id, pc.id", (po_id,))]
    meta = json.loads(po["meta"] or "{}")
    return {
        "po": {"id": po["id"], "po_number": po["po_number"], "vendor_id": po["vendor_id"], "vendor": po["vendor"],
               "vendor_status": po["vendor_status"], "vendor_tax_id": po["vendor_tax_id"], "currency": po["currency"],
               "issued_date": po["issued_date"], "status": po["status"]},
        "amounts": {"total": _m(po["total_amount"]), "committed": _m(committed), "balance": _m(balance),
                    "awaiting_review": _m(awaiting), "over_billed": balance < 0, **split},
        "lines": lines,
        "invoices": invoices,
        "ledger": ledger,
        "allocations": allocations,
        "considered_in": _considered(conn, po_id, po["po_number"]),
        "provenance": meta,
    }


def po_lines_with_consumption(conn: sqlite3.Connection, po_id: int) -> tuple[list[dict], dict[str, Any]]:
    """The PO's lines with consumed and remaining quantity/amount (line-assigned consumption only; derived, never stored), and the
    PO-level split: consumed against lines vs against the PO total with no line."""
    by_line, unassigned = po_consumption_summary(conn, po_id)
    lines = []
    for r in conn.execute("SELECT * FROM po_lines WHERE po_id = ? ORDER BY line_no", (po_id,)):
        cq, ca = by_line.get(r["id"], (Decimal(0), 0))
        qty = None if r["quantity"] is None else Decimal(r["quantity"])
        lines.append({"id": r["id"], "line_no": r["line_no"], "description": r["description"], "quantity": r["quantity"],
                      "unit_price": r["unit_price"], "amount": _m(r["amount"]),
                      "consumed_quantity": str(cq), "consumed_amount": _m(ca),
                      "remaining_quantity": None if qty is None else str(qty - cq),
                      "remaining_amount": None if r["amount"] is None else _m(r["amount"] - ca)})
    return lines, {"consumed_by_lines": _m(sum(a for _, a in by_line.values())), "consumed_without_line": _m(unassigned)}

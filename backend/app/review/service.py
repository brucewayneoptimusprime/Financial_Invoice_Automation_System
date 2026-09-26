"""Reading a review-queue item and building its approve preview (read-only; the actions are in actions.py).

The preview and the approval use the same `plan_allocation`, the stored line matches and the PO lines' remaining amounts as of
now. `state_token` fingerprints what the preview relied on; the approval compares it inside its transaction.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.db.consumption import po_consumption_summary
from app.db.queries import get_po_balance_minor
from app.engine.tolerance import evaluate_tolerance
from app.money import from_minor
from app.pipeline.allocation import (REMAINDER_LABEL, AllocationPlan, Choice, InvoiceLineIn, POLineNow, StoredMatch, TolParams,
                                     plan_allocation, rows_view)

BLOCK_TEXT = {
    "not_open": "This review item is already resolved.",
    "run_not_completed": "The run did not complete.",
    "no_invoice": "The run has no saved invoice.",
    "already_committed": "This invoice already has a ledger entry.",
    "no_matched_po": "No purchase order was matched to this invoice, so there is nothing to commit against. It can be rejected.",
    "po_closed": "The purchase order is closed.",
    "currency_missing": "The invoice has no currency.",
    "currency_mismatch": "The invoice currency differs from the purchase order's.",
    "total_missing": "The invoice has no usable total (missing, not positive, or not whole cents).",
}


def _s(minor: int | None) -> str | None:
    return None if minor is None else str(from_minor(minor))


def _dec(text: str | None) -> Decimal | None:
    return None if text in (None, "") else Decimal(text)


def tolerance_params(conn: sqlite3.Connection, settings: Settings) -> TolParams:
    """r_tolerance_pct's CURRENT params from the database (the same rule, applied per line)."""
    row = conn.execute("SELECT params FROM rules WHERE id = 'r_tolerance_pct'").fetchone()
    p = json.loads(row["params"]) if row else {}
    return TolParams(pct=float(p.get("pct", settings.tolerance_pct)), abs=Decimal(str(p.get("abs", settings.tolerance_abs))),
                     mode=p.get("mode", settings.tolerance_mode))


@dataclass
class ItemContext:
    item: dict
    run: dict | None
    invoice: dict | None
    po: dict | None
    lines: list[InvoiceLineIn]
    matches: dict[int, StoredMatch]
    po_lines: list[POLineNow]


def load_item(conn: sqlite3.Connection, item_id: int) -> ItemContext | None:
    item = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (item["run_id"],)).fetchone()
    inv = conn.execute("SELECT * FROM invoices WHERE run_id = ?", (item["run_id"],)).fetchone()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (inv["po_id"],)).fetchone() if inv and inv["po_id"] else None
    lines, matches, po_lines = [], {}, []
    if inv is not None:
        lines = [InvoiceLineIn(r["id"], r["line_no"], r["description"], _dec(r["quantity"]), _dec(r["unit_price"]), r["amount"])
                 for r in conn.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no", (inv["id"],))]
        matches = {r["invoice_line_id"]: StoredMatch(r["invoice_line_id"], r["status"], r["po_line_id"], r["score"], tuple(json.loads(r["candidates"])))
                   for r in conn.execute("SELECT * FROM invoice_line_matches WHERE invoice_id = ?", (inv["id"],))}
    if po is not None:
        by_line, _ = po_consumption_summary(conn, po["id"])
        for r in conn.execute("SELECT * FROM po_lines WHERE po_id = ? ORDER BY line_no", (po["id"],)):
            cq, ca = by_line.get(r["id"], (Decimal(0), 0))
            qty = _dec(r["quantity"])
            po_lines.append(POLineNow(r["id"], r["line_no"], r["description"], qty, _dec(r["unit_price"]), r["amount"],
                                      None if r["amount"] is None else r["amount"] - ca, None if qty is None else qty - cq))
    return ItemContext(dict(item), None if run is None else dict(run), None if inv is None else dict(inv), None if po is None else dict(po),
                       lines, matches, po_lines)


def blockers(conn: sqlite3.Connection, c: ItemContext) -> list[str]:
    out = []
    if c.item["status"] != "open":
        out.append("not_open")
    if c.run is None or c.run["status"] != "completed":
        out.append("run_not_completed")
    if c.invoice is None:
        return out + ["no_invoice"]
    if conn.execute("SELECT 1 FROM ledger_entries WHERE invoice_id = ?", (c.invoice["id"],)).fetchone():
        out.append("already_committed")
    if c.po is None:
        out.append("no_matched_po")
    else:
        if c.po["status"] == "closed":
            out.append("po_closed")
        if not c.invoice["currency"]:
            out.append("currency_missing")
        elif c.invoice["currency"].upper() != c.po["currency"].upper():
            out.append("currency_mismatch")
    if c.invoice["total"] is None or c.invoice["total"] <= 0:
        out.append("total_missing")
    return out


def state_token(conn: sqlite3.Connection, c: ItemContext) -> str:
    """Everything the preview relied on that another action could change: the item and invoice status, the PO status, and the PO's
    ledger and consumption rows."""
    parts: dict[str, Any] = {"item": c.item["status"], "invoice": None if c.invoice is None else c.invoice["status"]}
    if c.po is not None:
        parts["po"] = c.po["status"]
        parts["ledger"] = [list(r) for r in conn.execute("SELECT id, amount FROM ledger_entries WHERE po_id = ? ORDER BY id", (c.po["id"],))]
        parts["consumption"] = [list(r) for r in conn.execute("SELECT id, po_line_id, amount, quantity FROM po_consumption WHERE po_id = ? "
                                                             "ORDER BY id", (c.po["id"],))]
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def plan_for(conn: sqlite3.Connection, c: ItemContext, settings: Settings, supplied: dict[int, Choice] | None = None) -> AllocationPlan | None:
    if c.invoice is None or c.po is None or c.invoice["total"] is None:
        return None
    return plan_allocation(c.lines, c.matches, c.po_lines, c.invoice["total"], supplied or {}, tolerance_params(conn, settings))


def approve_preview(conn: sqlite3.Connection, c: ItemContext, settings: Settings) -> dict:
    blocked = blockers(conn, c)
    out: dict[str, Any] = {"possible": not blocked, "blocked_by": [{"code": b, "message": BLOCK_TEXT[b]} for b in blocked],
                           "warnings": [], "po": None, "commit_amount": None, "state_token": state_token(conn, c),
                           "tolerance": None, "automatic": [], "needs_input": [], "rows": [], "remainder": None, "notes": []}
    plan = plan_for(conn, c, settings)
    if plan is None:
        return out
    tol = tolerance_params(conn, settings)
    before = get_po_balance_minor(conn, c.po["id"])
    after = before - c.invoice["total"]
    out.update(po={"id": c.po["id"], "po_number": c.po["po_number"], "status": c.po["status"], "currency": c.po["currency"],
                   "balance_before": _s(before), "balance_after": _s(after)},
               commit_amount=_s(c.invoice["total"]), tolerance={"pct": tol.pct, "abs": f"{tol.abs:.2f}", "mode": tol.mode},
               automatic=[r for r in rows_view(plan) if r["kind"] == "automatic"], needs_input=plan.needs_input,
               rows=rows_view(plan), notes=plan.notes,
               remainder=None if plan.remainder_minor == 0 else {"amount": _s(plan.remainder_minor), "label": REMAINDER_LABEL})
    if after < 0:
        t = evaluate_tolerance(c.invoice["total"], before, tol.pct, tol.abs, tol.mode)
        out["warnings"].append(f"The commit takes {c.po['po_number']} over its balance by {_s(-after)} "
                               + ("(within the tolerance the rules allow)." if t.within_tolerance else
                                  "(beyond the tolerance the rules allow; approving overrides that check)."))
    return out


def list_items(conn: sqlite3.Connection, status: str, limit: int, settings: Settings) -> list[dict]:
    order = "ASC" if status == "open" else "DESC"
    rows = conn.execute(
        "SELECT q.*, r.source_file, r.started_at, i.id AS invoice_id, i.invoice_number, i.total, i.currency, v.name AS vendor, "
        f"p.po_number FROM review_queue q JOIN runs r ON r.id = q.run_id LEFT JOIN invoices i ON i.run_id = q.run_id "
        f"LEFT JOIN vendors v ON v.id = i.vendor_id LEFT JOIN purchase_orders p ON p.id = i.po_id WHERE q.status = ? "
        f"ORDER BY r.started_at {order}, q.id {order} LIMIT ?", (status, limit)).fetchall()
    out = []
    for r in rows:
        item = {"id": r["id"], "run_id": r["run_id"], "status": r["status"], "resolution": r["resolution"], "resolved_at": r["resolved_at"],
                "source_file": r["source_file"], "invoice_number": r["invoice_number"], "vendor": r["vendor"], "total": _s(r["total"]),
                "currency": r["currency"], "po_number": r["po_number"], "reason": r["reason"], "queued_at": r["started_at"],
                "can_approve": False, "lines_needing_input": 0}
        if r["status"] == "open":
            c = load_item(conn, r["id"])
            plan = plan_for(conn, c, settings)
            item["can_approve"] = not blockers(conn, c)
            item["lines_needing_input"] = 0 if plan is None else len(plan.needs_input)
        out.append(item)
    return out


def open_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM review_queue WHERE status = 'open'").fetchone()[0]

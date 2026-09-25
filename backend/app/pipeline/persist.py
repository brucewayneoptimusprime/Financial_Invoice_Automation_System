"""ALL of M3's SQL lives here: runs, audit events, invoices and lines (Stage 1); ledger, review queue and drafts (Stage 2).

Conventions:
  * Functions execute statements and never commit; the caller owns the transaction (`transaction(conn)`), so the act stage
    can write an invoice, a ledger entry, a review item, a draft and their audit events atomically.
  * Money crosses the boundary only through app.money (integer minor units). A value that is missing or not representable
    (sub-cent) is stored as NULL and reported as a note, never rounded.
  * `runs.final_decision` is set once by finish_run() and never modified afterwards (SPEC section 5).
  * PO balance is never stored: the ledger is the source of truth (see app.db.queries).
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

from app.engine.engine import jsonable
from app.enums import Decision, InvoiceStatus, Outcome
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedInvoice
from app.models.run import RunContext
from app.money import to_minor

# the system's decision -> the invoice's effective status at run time (a human resolution may change it later)
STATUS_FOR_DECISION = {
    Decision.APPROVE: InvoiceStatus.APPROVED,
    Decision.REVIEW: InvoiceStatus.IN_REVIEW,
    Decision.REQUEST_INFO: InvoiceStatus.AWAITING_INFO,
    Decision.REJECT: InvoiceStatus.REJECTED,
}


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """BEGIN [IMMEDIATE] ... COMMIT, or ROLLBACK if the body raises. Not re-entrant (SQLite has no nested transactions)."""
    if conn.in_transaction:
        raise RuntimeError("a transaction is already open on this connection")
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# ----------------------------------------------------------------------------------------------------- runs

def start_run(conn: sqlite3.Connection, run_id: str, source_file: str) -> None:
    conn.execute("INSERT INTO runs (id, source_file, status) VALUES (?, ?, 'running')", (run_id, source_file))


def finish_run(conn: sqlite3.Connection, run_id: str, *, status: str, decision: Decision | None, tokens_in: int, tokens_out: int,
               cost_usd: Decimal | float, model: str | None) -> None:
    """Close the run. `final_decision` may be written only while it is still NULL: a run's original decision never changes."""
    cur = conn.execute(
        "UPDATE runs SET status = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), final_decision = ?, tokens_in = ?, "
        "tokens_out = ?, cost_usd = ?, model = ? WHERE id = ? AND final_decision IS NULL AND status = 'running'",
        (status, None if decision is None else decision.value, tokens_in, tokens_out, float(cost_usd), model, run_id))
    if cur.rowcount != 1:
        raise RuntimeError(f"run {run_id} is not open (already finished, or unknown)")


# ---------------------------------------------------------------------------------------------- audit events

class AuditWriter:
    """Writes a run's audit events with a monotonic `seq` (0, 1, 2, ...), continuing from what is already stored."""

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        self.conn, self.run_id = conn, run_id
        row = conn.execute("SELECT COALESCE(MAX(seq), -1) FROM audit_events WHERE run_id = ?", (run_id,)).fetchone()
        self._next = row[0] + 1

    def write(self, events: list[AuditEvent]) -> int:
        """Insert events in order; returns how many. Detail is made JSON-safe (Decimal -> exact string, date -> ISO)."""
        for e in events:
            self.conn.execute(
                "INSERT INTO audit_events (run_id, seq, stage, event_type, rule_id, outcome, message, detail) VALUES (?,?,?,?,?,?,?,?)",
                (self.run_id, self._next, e.stage, e.event_type, e.rule_id, e.outcome.value, e.message,
                 json.dumps(jsonable(e.detail), ensure_ascii=False, sort_keys=True)))
            self._next += 1
        return len(events)

    def emit(self, stage: str, event_type: str, outcome: Outcome, message: str, detail: dict[str, Any] | None = None,
             rule_id: str | None = None) -> None:
        self.write([AuditEvent(stage=stage, event_type=event_type, outcome=outcome, message=message, detail=detail or {},
                               rule_id=rule_id)])


# ------------------------------------------------------------------------------------------------- invoices

@dataclass
class SavedInvoice:
    invoice_id: int
    lines: int
    notes: list[str] = field(default_factory=list)      # values stored as NULL and why


def _minor(value: Decimal | None, label: str, notes: list[str]) -> int | None:
    if value is None:
        return None
    try:
        return to_minor(value)
    except (ValueError, ArithmeticError):
        notes.append(f"{label} {value} is not a whole number of cents; stored as NULL")
        return None


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def save_invoice(conn: sqlite3.Connection, ctx: RunContext, decision: Decision) -> SavedInvoice:
    """One `invoices` row (+ its lines) for a run that reached extraction, whatever the decision, so later duplicate checks
    see it. `extracted` keeps the full extraction JSON (adjustments live only there: there is no adjustments table)."""
    ex: ExtractedInvoice = ctx.extracted or ExtractedInvoice()
    notes: list[str] = []
    vm = ctx.matched_vendor
    vendor_id = vm.vendor_id if (vm is not None and vm.vendor_id is not None and not vm.ambiguous) else None
    po_id = ctx.matched_po.po_id if ctx.matched_po is not None else None
    date = ex.invoice_date.value
    cur = conn.execute(
        "INSERT INTO invoices (run_id, vendor_id, invoice_number, invoice_date, currency, subtotal, tax, total, po_id, decision, status, "
        "source_file, file_hash, extracted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctx.run_id, vendor_id, ex.invoice_number.value, None if date is None else date.isoformat(), ex.currency.value,
         _minor(ex.subtotal.value, "subtotal", notes), _minor(ex.tax.value, "tax", notes), _minor(ex.total.value, "total", notes),
         po_id, decision.value, STATUS_FOR_DECISION[decision].value, ctx.source_file, ctx.file_hash,
         json.dumps(ex.model_dump(mode="json"), ensure_ascii=False)))
    invoice_id = cur.lastrowid
    for n, line in enumerate(ex.line_items, start=1):
        conn.execute(
            "INSERT INTO invoice_lines (invoice_id, line_no, description, quantity, unit_price, amount) VALUES (?,?,?,?,?,?)",
            (invoice_id, n, line.description, _text(line.quantity), _text(line.unit_price), _minor(line.amount, f"line {n} amount", notes)))
    return SavedInvoice(invoice_id=invoice_id, lines=len(ex.line_items), notes=notes)


# ------------------------------------------------------------------------------- ledger, review queue, drafts

def po_balance_minor(conn: sqlite3.Connection, po_id: int) -> int:
    from app.db.queries import get_po_balance_minor

    return get_po_balance_minor(conn, po_id)


def commit_ledger(conn: sqlite3.Connection, po_id: int, invoice_id: int, amount_minor: int) -> int:
    """One `commit` entry (positive). The PO balance stays derived: total minus the ledger sum."""
    if amount_minor <= 0:
        raise ValueError("a ledger commit must be positive")
    return conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (?, ?, ?, 'commit')",
                        (po_id, invoice_id, amount_minor)).lastrowid


def set_po_status_after_commit(conn: sqlite3.Connection, po_id: int) -> str:
    """partially_billed, or fully_billed once the derived balance is used up. Never `closed` (a human action)."""
    status = "fully_billed" if po_balance_minor(conn, po_id) <= 0 else "partially_billed"
    conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (status, po_id))
    return status


def enqueue_review(conn: sqlite3.Connection, run_id: str, reason: str) -> int:
    return conn.execute("INSERT INTO review_queue (run_id, reason, status) VALUES (?, ?, 'open')", (run_id, reason)).lastrowid


def save_draft(conn: sqlite3.Connection, run_id: str, kind: str, to: str | None, subject: str, body: str) -> int:
    """Always status 'draft': nothing in this code base sends anything."""
    return conn.execute('INSERT INTO drafts (run_id, kind, "to", subject, body, status) VALUES (?, ?, ?, ?, ?, \'draft\')',
                        (run_id, kind, to, subject, body)).lastrowid


def snapshot_is_current(conn: sqlite3.Connection, run_id: str, prior_invoice_count: int, po_id: int | None, net_committed_minor: int | None) -> str | None:
    """None if nothing the decision relied on has changed since the facts snapshot; else a sentence saying what changed."""
    now = conn.execute("SELECT COUNT(*) FROM invoices WHERE run_id IS NULL OR run_id <> ?", (run_id,)).fetchone()[0]
    if now != prior_invoice_count:
        return f"other invoices were recorded while this run was in progress ({prior_invoice_count} before, {now} now)"
    if po_id is not None and net_committed_minor is not None:
        row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE po_id = ?", (po_id,)).fetchone()
        if row[0] != net_committed_minor:
            return "the purchase order's balance changed while this run was in progress"
    return None

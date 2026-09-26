"""Approve / reject ONE review-queue item (review actions + line allocation). No bulk path exists.

Approve, inside ONE `BEGIN IMMEDIATE` transaction: re-read everything; any blocker -> 409; the reviewer's `state_token` must equal
the current one -> else 409 `stale` with a fresh preview; plan the allocation with the reviewer's choices -> 422 when a choice is
missing (`allocation_required`: nothing is written or defaulted) or invalid (`allocation_invalid`); then the ledger commit (the
invoice TOTAL), one consumption row per allocation (verified to add up to the commit), the PO status from the ledger, the invoice
`approved`, the item resolved, and audit events on the run. `runs.final_decision` never changes (SPEC item 12). Any exception rolls
everything back.

Reject: the invoice `rejected`, the item resolved, audit events. Never a ledger entry, a consumption row or a vendor email.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.db.consumption import consumption_problems, record_consumption
from app.db.queries import get_po_balance_minor
from app.enums import MatchedBy, Outcome
from app.models.audit import AuditEvent
from app.money import from_minor
from app.pipeline import persist
from app.pipeline.allocation import Choice, rows_view
from app.pipeline.persist import AuditWriter, transaction
from app.review import service

REVIEW_STAGE = "review"
REVIEWER = "reviewer (local UI)"


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_line_id: int
    target: Literal["po_line", "unassigned"]
    po_line_id: int | None = None


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    state_token: str = Field(min_length=1, max_length=128)
    allocations: list[AllocationIn] = Field(default_factory=list, max_length=500)
    note: str | None = Field(default=None, max_length=500)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    reason: str | None = Field(default=None, max_length=500)


class ActionError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _s(minor: int | None) -> str | None:
    return None if minor is None else str(from_minor(minor))


def _supplied(req: ApproveRequest) -> dict[int, Choice]:
    out: dict[int, Choice] = {}
    problems = []
    for a in req.allocations:
        if a.invoice_line_id in out:
            problems.append({"invoice_line_id": a.invoice_line_id, "code": "duplicate_line",
                             "message": f"Invoice line {a.invoice_line_id} was given more than one choice."})
        elif a.target == "po_line" and a.po_line_id is None:
            problems.append({"invoice_line_id": a.invoice_line_id, "code": "not_a_line_of_this_po", "message": "A PO line target needs a po_line_id."})
        out[a.invoice_line_id] = Choice(a.target, a.po_line_id if a.target == "po_line" else None)
    if problems:
        raise ActionError(422, "allocation_invalid", "Some line choices are not valid.", problems=problems)
    return out


def approve(conn: sqlite3.Connection, item_id: int, req: ApproveRequest, settings: Settings) -> dict:
    if req.confirm is not True:
        raise ActionError(400, "confirm_required", "Approving needs an explicit confirmation (confirm: true).")
    supplied = _supplied(req)
    with transaction(conn, immediate=True):
        c = service.load_item(conn, item_id)
        if c is None:
            raise ActionError(404, "not_found", "No such review item.")
        blocked = service.blockers(conn, c)
        if "not_open" in blocked:
            raise ActionError(409, "stale", "This review item was resolved meanwhile.", preview=service.approve_preview(conn, c, settings))
        if blocked:
            raise ActionError(409, "not_approvable", "This invoice cannot be approved.",
                              blocked_by=[{"code": b, "message": service.BLOCK_TEXT[b]} for b in blocked])
        if req.state_token != service.state_token(conn, c):
            raise ActionError(409, "stale", "The purchase order changed since this preview was shown; check the new numbers and "
                              "approve again.", preview=service.approve_preview(conn, c, settings))
        plan = service.plan_for(conn, c, settings, supplied)
        if plan.problems:
            raise ActionError(422, "allocation_invalid", "Some line choices are not valid.", problems=plan.problems)
        if plan.missing:
            need = [n for n in plan.needs_input if n["invoice_line_id"] in plan.missing]
            raise ActionError(422, "allocation_required", f"{len(need)} line{'s' if len(need) != 1 else ''} need{'s' if len(need) == 1 else ''} "
                              "a choice before this invoice can be approved.", needs_input=need)

        inv, po, run_id = c.invoice, c.po, c.item["run_id"]
        before = get_po_balance_minor(conn, po["id"])
        entry_id = persist.commit_ledger(conn, po["id"], inv["id"], inv["total"])
        for r in plan.rows:
            record_consumption(conn, ledger_entry_id=entry_id, po_id=po["id"], invoice_id=inv["id"], run_id=run_id,
                               amount_minor=r.amount_minor, matched_by=MatchedBy(r.matched_by), po_line_id=r.po_line_id,
                               invoice_line_id=r.invoice_line_id, quantity=None if (r.quantity is None or r.po_line_id is None) else format(r.quantity, "f"))
        broken = [p for p in consumption_problems(conn) if f"ledger entry {entry_id}" in p]
        if broken:
            raise RuntimeError("allocation invariant violated: " + "; ".join(broken))
        po_status = persist.set_po_status_after_commit(conn, po["id"])
        after = get_po_balance_minor(conn, po["id"])
        conn.execute("UPDATE invoices SET status = 'approved' WHERE id = ?", (inv["id"],))
        resolved_at = _now()
        cur = conn.execute("UPDATE review_queue SET status = 'resolved', resolution = 'approved', resolved_at = ? WHERE id = ? AND status = 'open'",
                           (resolved_at, item_id))
        if cur.rowcount != 1:
            raise RuntimeError("the review item was not open")
        rows = rows_view(plan)
        AuditWriter(conn, run_id).write([
            AuditEvent(stage=REVIEW_STAGE, event_type="human_approved", outcome=Outcome.PASS,
                               message=f"Approved by a reviewer: {_s(inv['total'])} committed to {po['po_number']}; "
                                       f"balance {_s(before)} -> {_s(after)}.",
                               detail={"reviewer": REVIEWER, "note": req.note, "review_id": item_id, "ledger_entry_id": entry_id,
                                       "amount": _s(inv["total"]), "po_number": po["po_number"], "balance_before": _s(before),
                                       "balance_after": _s(after), "po_status": po_status}),
            AuditEvent(stage=REVIEW_STAGE, event_type="allocation", outcome=Outcome.INFO,
                               message=f"Allocated across {po['po_number']}: " + ", ".join(
                                   f"{r['amount']} " + (f"to line {r['po_line_no']}" if r["po_line_no"] else "to the PO total") + f" ({r['matched_by']})"
                                   for r in rows) + ("; lines scaled down pro rata" if plan.pro_rata else "") + ".",
                               detail={"rows": rows, "pro_rata": plan.pro_rata, "notes": plan.notes}),
            AuditEvent(stage=REVIEW_STAGE, event_type="review_resolved", outcome=Outcome.INFO, message="Review item resolved: approved.",
                               detail={"review_id": item_id, "resolution": "approved", "resolved_at": resolved_at}),
        ])
    return {"status": "approved", "ledger_entry_id": entry_id, "amount": _s(inv["total"]),
            "po": {"id": po["id"], "po_number": po["po_number"], "balance_before": _s(before), "balance_after": _s(after), "status": po_status},
            "allocations": rows, "pro_rata": plan.pro_rata, "notes": plan.notes,
            "review_item": {"id": item_id, "status": "resolved", "resolution": "approved", "resolved_at": resolved_at}}


def reject(conn: sqlite3.Connection, item_id: int, req: RejectRequest) -> dict:
    if req.confirm is not True:
        raise ActionError(400, "confirm_required", "Rejecting needs an explicit confirmation (confirm: true).")
    with transaction(conn, immediate=True):
        item = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise ActionError(404, "not_found", "No such review item.")
        if item["status"] != "open":
            raise ActionError(409, "stale", "This review item was resolved meanwhile.")
        inv = conn.execute("SELECT id FROM invoices WHERE run_id = ?", (item["run_id"],)).fetchone()
        if inv is not None and conn.execute("SELECT 1 FROM ledger_entries WHERE invoice_id = ?", (inv["id"],)).fetchone():
            raise ActionError(409, "already_committed", "This invoice already has a ledger entry.")
        if inv is not None:
            conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = ?", (inv["id"],))
        resolved_at = _now()
        conn.execute("UPDATE review_queue SET status = 'resolved', resolution = 'rejected', resolved_at = ? WHERE id = ?", (resolved_at, item_id))
        AuditWriter(conn, item["run_id"]).write([
            AuditEvent(stage=REVIEW_STAGE, event_type="human_rejected", outcome=Outcome.FAIL,
                               message="Rejected by a reviewer. No ledger entry and no allocation were written.",
                               detail={"reviewer": REVIEWER, "reason": req.reason, "review_id": item_id}),
            AuditEvent(stage=REVIEW_STAGE, event_type="review_resolved", outcome=Outcome.INFO, message="Review item resolved: rejected.",
                               detail={"review_id": item_id, "resolution": "rejected", "resolved_at": resolved_at}),
        ])
    return {"status": "rejected", "review_item": {"id": item_id, "status": "resolved", "resolution": "rejected", "resolved_at": resolved_at}}

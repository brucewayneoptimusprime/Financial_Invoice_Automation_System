"""How an approved invoice's ledger commit is allocated across the PO's lines (review actions + line allocation).

Pure: no SQLite, no clock. The SAME function builds the approve preview shown to the reviewer and the rows the approval writes.
It reads the STORED line matches of the run (no re-matching) and the PO lines' remaining amounts as of now.

Owner decisions: the decision is taken first and never changed here; allocation happens only on approve. `matched` lines that
still fit their PO line are allocated automatically (matched_by auto). Every other line needs the reviewer: a PO line of this PO
(any, the description is not checked) or "no specific line". A reviewer's line assignment must fit the PO line's remaining amount
with the SAME tolerance calculation as the whole-PO rule (`evaluate_tolerance`, r_tolerance_pct's params), applied per line; lines
of one approval that land on the same PO line use it up in order. The commit is the invoice TOTAL; what the lines do not cover
(tax, shipping, fees) is one row against the PO total; if the lines add up to MORE than the total (a large discount) they are
scaled down pro rata so the rows add up exactly to the commit. Nothing here guesses a missing choice.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.engine.tolerance import evaluate_tolerance
from app.money import from_minor

REMAINDER_LABEL = "tax, shipping and other amounts not on a line"


@dataclass(frozen=True)
class InvoiceLineIn:
    id: int
    line_no: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount_minor: int | None


@dataclass(frozen=True)
class StoredMatch:
    invoice_line_id: int
    status: str                        # matched | ambiguous | no_match | not_evaluable
    po_line_id: int | None
    score: float | None
    candidates: tuple[dict, ...]       # as stored: po_line_id, po_line_no, score, breakdown, reasons


@dataclass(frozen=True)
class POLineNow:
    id: int
    line_no: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount_minor: int | None
    remaining_amount_minor: int | None      # line amount minus its line-assigned consumption (None if the line has no amount)
    remaining_quantity: Decimal | None


@dataclass(frozen=True)
class Choice:
    target: Literal["po_line", "unassigned"]
    po_line_id: int | None = None


@dataclass(frozen=True)
class TolParams:
    pct: float
    abs: Decimal
    mode: str


@dataclass
class AllocRow:
    kind: Literal["automatic", "reviewer_line", "reviewer_unassigned", "remainder"]
    amount_minor: int
    invoice_line_id: int | None = None
    invoice_line_no: int | None = None
    po_line_id: int | None = None
    po_line_no: int | None = None
    quantity: Decimal | None = None
    matched_by: str = "auto"                 # auto | manual_reviewer


@dataclass
class AllocationPlan:
    commit_minor: int
    rows: list[AllocRow] = field(default_factory=list)
    needs_input: list[dict] = field(default_factory=list)     # every line that needs (or got) a reviewer choice, with its options
    missing: list[int] = field(default_factory=list)          # invoice_line_ids that need a choice and have none
    problems: list[dict] = field(default_factory=list)        # invalid supplied choices
    notes: list[str] = field(default_factory=list)
    remainder_minor: int = 0
    pro_rata: bool = False

    @property
    def complete(self) -> bool:
        return not self.missing and not self.problems


def _s(minor: int | None) -> str | None:
    return None if minor is None else str(from_minor(minor))


def fit(amount_minor: int, remaining_minor: int | None, used_minor: int, tol: TolParams) -> dict:
    """Does `amount` fit a PO line's remaining amount (minus what this approval already put on it)? The whole-PO tolerance
    calculation, per line: allowance = lesser/greater of pct% of the remaining and abs; B <= 0 leaves no percentage part."""
    if remaining_minor is None:
        return {"fits": False, "code": "po_line_has_no_amount", "remaining": None, "allowance": None}
    b = remaining_minor - used_minor
    t = evaluate_tolerance(amount_minor, b, tol.pct, tol.abs, tol.mode)
    return {"fits": t.excess_minor <= t.allowance_minor, "code": None if t.excess_minor <= t.allowance_minor else "exceeds_remaining",
            "remaining": _s(b), "allowance": _s(t.allowance_minor)}


def _line_view(line: InvoiceLineIn) -> dict:
    return {"invoice_line_id": line.id, "invoice_line_no": line.line_no, "description": line.description,
            "quantity": None if line.quantity is None else str(line.quantity),
            "unit_price": None if line.unit_price is None else str(line.unit_price), "amount": _s(line.amount_minor)}


WHY = {"ambiguous": "two or more PO lines are about equally likely",
       "no_match": "no PO line resembles it closely enough",
       "not_evaluable": "it could not be compared automatically",
       "no_longer_fits": "its automatic match no longer fits: the PO line was consumed meanwhile"}


def plan_allocation(lines: list[InvoiceLineIn], matches: dict[int, StoredMatch], po_lines: list[POLineNow], commit_minor: int,
                    supplied: dict[int, Choice], tol: TolParams) -> AllocationPlan:
    plan = AllocationPlan(commit_minor=commit_minor)
    by_id = {pl.id: pl for pl in po_lines}
    used: dict[int, int] = {}
    assignable = any(pl.amount_minor is not None for pl in po_lines)

    def use(pl: POLineNow, amount: int) -> None:
        used[pl.id] = used.get(pl.id, 0) + amount

    for line in sorted(lines, key=lambda x: x.line_no):
        choice = supplied.get(line.id)
        if line.amount_minor is None or line.amount_minor <= 0:
            plan.notes.append(f"Invoice line {line.line_no} has no amount; nothing to allocate (its value, if any, is in the remainder).")
            if choice is not None:
                plan.problems.append({"invoice_line_id": line.id, "code": "no_amount", "message": f"Invoice line {line.line_no} has no amount to allocate."})
            continue
        if not assignable:                                       # nothing could ever be chosen: it all goes to the PO total
            if choice is not None:
                plan.problems.append({"invoice_line_id": line.id, "code": "no_assignable_lines",
                                      "message": "The purchase order has no lines with an amount; nothing can be chosen."})
            continue
        m = matches.get(line.id)
        auto_pl = by_id.get(m.po_line_id) if (m is not None and m.status == "matched" and m.po_line_id is not None) else None
        auto_fit = fit(line.amount_minor, auto_pl.remaining_amount_minor, used.get(auto_pl.id, 0), tol) if auto_pl else None
        if auto_pl is not None and auto_fit["fits"]:
            if choice is not None:
                plan.problems.append({"invoice_line_id": line.id, "code": "automatic_line",
                                      "message": f"Invoice line {line.line_no} is allocated automatically to PO line {auto_pl.line_no}; it takes no choice."})
            plan.rows.append(AllocRow("automatic", line.amount_minor, line.id, line.line_no, auto_pl.id, auto_pl.line_no, line.quantity, "auto"))
            use(auto_pl, line.amount_minor)
            continue

        # --- the reviewer decides this line
        why = WHY["no_longer_fits"] if auto_pl is not None else WHY.get(m.status if m else "not_evaluable", WHY["not_evaluable"])
        ranked = [c for c in (m.candidates if m else ()) if c.get("po_line_id") in by_id]
        options = []
        for c in ranked:
            pl = by_id[c["po_line_id"]]
            options.append({**c, "description": pl.description, "unit_price": None if pl.unit_price is None else str(pl.unit_price),
                            "remaining_amount": _s(pl.remaining_amount_minor),
                            **fit(line.amount_minor, pl.remaining_amount_minor, used.get(pl.id, 0), tol)})
        ranked_ids = {c["po_line_id"] for c in ranked}
        others = [{"po_line_id": pl.id, "po_line_no": pl.line_no, "description": pl.description,
                   "unit_price": None if pl.unit_price is None else str(pl.unit_price), "remaining_amount": _s(pl.remaining_amount_minor),
                   **fit(line.amount_minor, pl.remaining_amount_minor, used.get(pl.id, 0), tol)}
                  for pl in sorted(po_lines, key=lambda x: x.line_no) if pl.id not in ranked_ids]
        best = next((o for o in options if o["fits"]), None) if auto_pl is None else None
        plan.needs_input.append({**_line_view(line), "status": "no_longer_fits" if auto_pl is not None else (m.status if m else "not_evaluable"),
                                 "why": why, "suggested": {"target": "po_line", "po_line_id": best["po_line_id"]} if best else {"target": "unassigned"},
                                 "candidates": options, "other_lines": others})
        if choice is None:
            plan.missing.append(line.id)
            continue
        if choice.target == "unassigned":
            plan.rows.append(AllocRow("reviewer_unassigned", line.amount_minor, line.id, line.line_no, None, None, None, "manual_reviewer"))
            continue
        pl = by_id.get(choice.po_line_id)
        if pl is None:
            plan.problems.append({"invoice_line_id": line.id, "po_line_id": choice.po_line_id, "code": "not_a_line_of_this_po",
                                  "message": f"PO line {choice.po_line_id} is not a line of this purchase order."})
            continue
        f = fit(line.amount_minor, pl.remaining_amount_minor, used.get(pl.id, 0), tol)
        if not f["fits"]:
            msg = (f"Invoice line {line.line_no} ({_s(line.amount_minor)}) does not fit PO line {pl.line_no}: "
                   + ("that PO line has no amount to check against." if f["code"] == "po_line_has_no_amount"
                      else f"{f['remaining']} remaining, allowance {f['allowance']}."))
            plan.problems.append({"invoice_line_id": line.id, "po_line_id": pl.id, "code": f["code"], "message": msg,
                                  "amount": _s(line.amount_minor), "remaining": f["remaining"], "allowance": f["allowance"]})
            continue
        plan.rows.append(AllocRow("reviewer_line", line.amount_minor, line.id, line.line_no, pl.id, pl.line_no, line.quantity, "manual_reviewer"))
        use(pl, line.amount_minor)

    for line_id in supplied:
        if line_id not in {x.id for x in lines}:
            plan.problems.append({"invoice_line_id": line_id, "code": "unknown_invoice_line", "message": f"Invoice line {line_id} is not on this invoice."})

    _close(plan)
    return plan


def _close(plan: AllocationPlan) -> None:
    """The rows must add up exactly to the commit: a positive remainder goes to the PO total; a negative one scales the lines down."""
    allocated = sum(r.amount_minor for r in plan.rows)
    remainder = plan.commit_minor - allocated
    if remainder > 0:
        plan.rows.append(AllocRow("remainder", remainder))
    elif remainder < 0 and allocated > 0:
        plan.pro_rata = True
        scaled = [r.amount_minor * plan.commit_minor // allocated for r in plan.rows]
        leftover = plan.commit_minor - sum(scaled)
        largest = max(range(len(plan.rows)), key=lambda i: plan.rows[i].amount_minor)
        scaled[largest] += leftover
        for r, a in zip(plan.rows, scaled):
            r.amount_minor = a
        plan.rows = [r for r in plan.rows if r.amount_minor > 0]
        plan.notes.append(f"The lines add up to {_s(allocated)}, more than the invoice total {_s(plan.commit_minor)} (a discount or "
                          "credit); every line allocation was reduced in proportion so the allocations equal the commit.")
    plan.remainder_minor = max(remainder, 0)


def rows_view(plan: AllocationPlan) -> list[dict]:
    return [{"kind": r.kind, "invoice_line_id": r.invoice_line_id, "invoice_line_no": r.invoice_line_no, "po_line_id": r.po_line_id,
             "po_line_no": r.po_line_no, "amount": _s(r.amount_minor), "quantity": None if r.quantity is None else str(r.quantity),
             "matched_by": r.matched_by, "label": REMAINDER_LABEL if r.kind == "remainder" else None} for r in plan.rows]

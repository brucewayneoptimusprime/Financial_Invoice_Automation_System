"""Read models for the API: SQLite rows -> JSON. Read-only SQL; nothing here imports pipeline objects or changes state.

Amounts leave as exact decimal strings (integer cents converted by app.money), never floats.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.money import from_minor
from app.po.views import po_lines_with_consumption
from app.pipeline.summary import STAGE_COMPLETED, STAGE_STARTED, STAGES

RUN_COLUMNS = ("id", "source_file", "status", "started_at", "finished_at", "final_decision", "tokens_in", "tokens_out", "cost_usd", "model")
_MONEY_COLUMNS = ("subtotal", "tax", "total")


def _money(minor: int | None) -> str | None:
    return None if minor is None else str(from_minor(minor))


def _row(r: sqlite3.Row | None) -> dict | None:
    return None if r is None else {k: r[k] for k in r.keys()}


def event_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"seq": r["seq"], "stage": r["stage"], "event_type": r["event_type"], "rule_id": r["rule_id"], "outcome": r["outcome"],
            "message": r["message"], "detail": json.loads(r["detail"]), "created_at": r["created_at"]}


def events_after(conn: sqlite3.Connection, run_id: str, after_seq: int, limit: int = 500) -> list[dict]:
    rows = conn.execute("SELECT seq, stage, event_type, rule_id, outcome, message, detail, created_at FROM audit_events "
                        "WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?", (run_id, after_seq, limit)).fetchall()
    return [event_row(r) for r in rows]


def run_row(conn: sqlite3.Connection, run_id: str) -> dict | None:
    return _row(conn.execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE id = ?", (run_id,)).fetchone())


def recent_runs(conn: sqlite3.Connection, limit: int, decision: str | None = None) -> list[dict]:
    """Newest runs first, with the saved invoice's vendor, number, total and CURRENT status (after any human review), if any.
    `decision` keeps only runs whose SYSTEM decision (runs.final_decision) is that one."""
    cols = ", ".join(f"r.{c}" for c in RUN_COLUMNS)
    rows = conn.execute(
        f"SELECT {cols}, i.invoice_number, i.currency AS invoice_currency, i.total AS invoice_total, i.status AS invoice_status, "
        "v.name AS vendor FROM runs r LEFT JOIN invoices i ON i.run_id = r.id LEFT JOIN vendors v ON v.id = i.vendor_id "
        + ("WHERE r.final_decision = ? " if decision else "") + "ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?",
        ((decision, limit) if decision else (limit,))).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["invoice_total"] = _money(d["invoice_total"])
        out.append(d)
    return out


def _stages(events: list[dict]) -> list[dict]:
    stages = {name: {"stage": name, "status": "waiting", "duration_ms": None, "summary": {}} for name in STAGES}
    for e in events:
        if e["stage"] != "pipeline" or e["event_type"] not in (STAGE_STARTED, STAGE_COMPLETED):
            continue
        s = stages.get(e["detail"].get("stage"))
        if s is None:
            continue
        if e["event_type"] == STAGE_STARTED:
            s["status"] = "running"
        else:
            s.update(status=e["detail"]["status"], duration_ms=e["detail"]["duration_ms"], summary=e["detail"]["summary"])
    return list(stages.values())


def _rules(events: list[dict], names: dict[str, str]) -> list[dict]:
    out = []
    for e in events:
        if e["event_type"] not in ("rule_evaluated", "engine_floor"):
            continue
        d = e["detail"]
        out.append({"rule_id": e["rule_id"], "name": names.get(e["rule_id"]) or e["rule_id"].replace("_", " "),
                    "kind": "floor" if e["event_type"] == "engine_floor" else "rule", "outcome": e["outcome"],
                    "severity": d.get("severity", 0), "outcome_key": d.get("outcome_key"), "message": e["message"], "detail": d})
    return out


def _first(events: list[dict], event_type: str) -> dict | None:
    return next((e for e in events if e["event_type"] == event_type), None)


def page_numbers(runs_dir: Path, run_id: str) -> list[int]:
    folder = runs_dir / run_id / "pages"
    if not folder.is_dir():
        return []
    nums = []
    for p in folder.iterdir():
        stem = p.stem
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and stem.startswith("page-") and stem[5:].isdigit():
            nums.append(int(stem[5:]))
    return sorted(nums)


def page_path(runs_dir: Path, run_id: str, n: int) -> Path | None:
    folder = (runs_dir / run_id / "pages").resolve()
    for suffix in (".png", ".jpg", ".jpeg"):
        p = (folder / f"page-{n}{suffix}").resolve()
        if p.parent == folder and p.is_file():
            return p
    return None


def line_match_view(conn: sqlite3.Connection, run_id: str, events: list[dict], invoice: dict | None) -> dict:
    """The data the reviewer's line picker needs (the picker itself is the follow-up): per invoice line its automatic status and
    its top candidate PO lines with their text, prices and remaining quantity/amount, all the PO's lines, and the PO-level split.
    Remaining values are as of NOW (other runs may have consumed since); the scores are as of the run."""
    ev = _first(events, "po_lines_matched")
    summary = {} if ev is None else ev["detail"]
    view: dict = {"mode": summary.get("mode"), "bundled_hint": summary.get("bundled_hint", False), "reason": summary.get("reason"),
                  "po": None, "lines": [], "po_lines": [], "po_consumption": None}
    po_id = summary.get("po_id")
    if po_id is None or invoice is None:
        return view
    po = conn.execute("SELECT id, po_number, currency FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    po_lines, split = po_lines_with_consumption(conn, po_id)
    by_id = {pl["id"]: pl for pl in po_lines}
    view.update(po=None if po is None else {"id": po["id"], "po_number": po["po_number"], "currency": po["currency"]},
                po_lines=po_lines, po_consumption=split)
    rows = conn.execute(
        "SELECT m.*, il.line_no, il.description, il.quantity, il.unit_price, il.amount AS line_amount FROM invoice_line_matches m "
        "JOIN invoice_lines il ON il.id = m.invoice_line_id WHERE m.run_id = ? ORDER BY il.line_no", (run_id,)).fetchall()
    for r in rows:
        candidates = []
        for c in json.loads(r["candidates"]):
            pl = by_id.get(c.get("po_line_id"), {})
            candidates.append({**c, **{k: pl.get(k) for k in ("description", "quantity", "unit_price", "amount", "consumed_quantity",
                                                              "consumed_amount", "remaining_quantity", "remaining_amount")}})
        view["lines"].append({"invoice_line_id": r["invoice_line_id"], "invoice_line_no": r["line_no"], "description": r["description"],
                              "quantity": r["quantity"], "unit_price": r["unit_price"], "amount": _money(r["line_amount"]),
                              "status": r["status"], "po_line_id": r["po_line_id"], "score": r["score"], "candidates": candidates})
    return view


def run_view(conn: sqlite3.Connection, run_id: str, runs_dir: Path) -> dict | None:
    run = run_row(conn, run_id)
    if run is None:
        return None
    events = events_after(conn, run_id, -1, limit=100_000)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM rules")}

    inv = _row(conn.execute("SELECT * FROM invoices WHERE run_id = ?", (run_id,)).fetchone())
    invoice, lines, extracted = None, [], None
    if inv is not None:
        extracted = json.loads(inv["extracted"]) if inv.get("extracted") else None
        invoice = {k: inv[k] for k in ("id", "vendor_id", "invoice_number", "invoice_date", "currency", "po_id", "decision", "status",
                                       "source_file", "file_hash", "created_at")}
        invoice.update({k: _money(inv[k]) for k in _MONEY_COLUMNS})
        lines = [{"line_no": r["line_no"], "description": r["description"], "quantity": r["quantity"], "unit_price": r["unit_price"],
                  "amount": _money(r["amount"])}
                 for r in conn.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no", (inv["id"],))]

    vendor_ev, ranked_ev, decision_ev = _first(events, "vendor_resolved"), _first(events, "po_candidates_ranked"), _first(events, "po_match_decision")
    vendor = None
    if vendor_ev is not None:
        vendor = dict(vendor_ev["detail"])
        vid = vendor.get("vendor_id")
        if vid is not None:
            rec = conn.execute("SELECT name, status, tax_id, country FROM vendors WHERE id = ?", (vid,)).fetchone()
            if rec is not None:
                vendor["record"] = _row(rec)
        vendor["message"] = vendor_ev["message"]

    explanation_ev = _first(events, "explanation")
    ledger_ev, escalated_ev = _first(events, "ledger_committed"), _first(events, "decision_escalated")
    error_ev = _first(events, "pipeline_error")
    aggregate_ev = _first(events, "severity_aggregated")
    drafts = [_row(r) for r in conn.execute('SELECT id, kind, "to", subject, body, status, created_at FROM drafts WHERE run_id = ? '
                                            "ORDER BY id", (run_id,))]
    review = [_row(r) for r in conn.execute("SELECT id, reason, status, resolution, resolved_at FROM review_queue WHERE run_id = ? "
                                            "ORDER BY id", (run_id,))]
    stage_costs = {}
    for name, e in (("extract", _first(events, "extraction_complete")), ("explain", explanation_ev), ("draft", _first(events, "draft_saved"))):
        if e is not None and "cost_usd" in e["detail"]:
            stage_costs[name] = e["detail"]["cost_usd"]

    return {
        "run": run,
        "stages": _stages(events),
        "decision": run["final_decision"],
        "explanation": None if explanation_ev is None else explanation_ev["detail"],
        "invoice": invoice,
        "lines": lines,
        "extracted": extracted,
        "vendor": vendor,
        "match": {"status": None if decision_ev is None else decision_ev["detail"].get("match_status"),
                  "matched_po": None if decision_ev is None else decision_ev["detail"].get("matched_po"),
                  "message": None if decision_ev is None else decision_ev["message"],
                  "candidates": [] if ranked_ev is None else ranked_ev["detail"].get("candidates", [])},
        "rules": _rules(events, names),
        "aggregate": None if aggregate_ev is None else aggregate_ev["detail"],
        "actions": {"ledger": None if ledger_ev is None else ledger_ev["detail"],
                    "ready_for_payment": _first(events, "ready_for_payment") is not None,
                    "escalated": None if escalated_ev is None else escalated_ev["detail"],
                    "review": review, "drafts": drafts},
        "pages": page_numbers(runs_dir, run_id),
        "stage_costs": stage_costs,
        "error": None if error_ev is None else {"message": error_ev["message"], "error_type": error_ev["detail"].get("error_type")},
        "line_matches": line_match_view(conn, run_id, events, invoice),
        "event_count": len(events),
    }

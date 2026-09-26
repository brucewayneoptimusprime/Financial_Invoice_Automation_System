"""The landing dashboard: one read-only snapshot across runs, reviews, spend and purchase orders (SPEC section 9.4).

Everything comes from existing tables through existing functions (`views.recent_runs`, `po.views.po_list`,
`db.consumption.po_consumption_summary`, `review.service.list_items` / `open_count`); the only new SQL is plain COUNT / SUM /
GROUP BY over existing columns. No decision logic, no writes. Money is summed in integer cents and never across currencies.
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.api import views
from app.config import Settings
from app.db.consumption import po_consumption_summary
from app.money import from_minor, to_minor
from app.po.views import po_list
from app.review import service as review_service

DECISIONS = ("approve", "review", "request_info", "reject")
OUTCOMES = ("approved", "in_review", "awaiting_info", "rejected", "pending")
PO_STATUSES = ("open", "partially_billed", "fully_billed", "closed")


def _runs(conn: sqlite3.Connection) -> dict:
    by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status")}
    by_decision = {d: 0 for d in DECISIONS}
    for d, n in conn.execute("SELECT final_decision, COUNT(*) FROM runs WHERE status = 'completed' AND final_decision IS NOT NULL "
                             "GROUP BY final_decision"):
        by_decision[d] = n
    return {"processed": by_status.get("completed", 0), "failed": by_status.get("failed", 0), "running": by_status.get("running", 0),
            "by_decision": by_decision}


def _outcomes(conn: sqlite3.Connection) -> dict:
    """The EFFECTIVE outcome now (after any human review), for invoices that came from a run (not the seeded history)."""
    out = {o: 0 for o in OUTCOMES}
    for status, n in conn.execute("SELECT status, COUNT(*) FROM invoices WHERE run_id IS NOT NULL GROUP BY status"):
        out[status] = n
    return out


def _spend(conn: sqlite3.Connection, settings: Settings) -> dict:
    runs_total, runs_n = conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM runs WHERE cost_usd > 0").fetchone()
    drafts, drafts_n = Decimal(0), 0
    root = Path(settings.po_drafts_dir)
    if root.is_dir():                                                    # PO drafts are not runs: their cost is in their draft file
        for f in root.glob("*/draft.json"):
            try:
                cost = Decimal(str(json.loads(f.read_text(encoding="utf-8")).get("provenance", {}).get("cost_usd") or 0))
            except (ValueError, ArithmeticError, OSError):
                continue
            if cost > 0:
                drafts += cost
                drafts_n += 1
    runs_dec = Decimal(str(runs_total)).quantize(Decimal("0.000001"))
    return {"invoice_runs_usd": f"{runs_dec:.6f}", "po_drafts_usd": f"{drafts:.6f}", "total_usd": f"{runs_dec + drafts:.6f}",
            "runs_counted": runs_n, "drafts_counted": drafts_n}


def _pos(conn: sqlite3.Connection) -> dict:
    rows = po_list(conn)                                                 # balances already derived from the ledger
    by_status = {s: 0 for s in PO_STATUSES}
    per: dict[str, dict[str, int]] = {}
    for p in rows:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
        c = per.setdefault(p["currency"], {"count": 0, "total": 0, "balance": 0, "without_line": 0})
        c["count"] += 1
        c["total"] += to_minor(Decimal(p["total"]))
        c["balance"] += to_minor(Decimal(p["balance"]))
        c["without_line"] += po_consumption_summary(conn, p["id"])[1]
    currencies = [{"currency": cur, "count": c["count"], "total_value": str(from_minor(c["total"])),
                   "consumed": str(from_minor(c["total"] - c["balance"])), "balance": str(from_minor(c["balance"])),
                   "consumed_without_line": str(from_minor(c["without_line"]))}
                  for cur, c in sorted(per.items())]
    return {"count": len(rows), "by_status": by_status, "currencies": currencies}


def snapshot(conn: sqlite3.Connection, settings: Settings, recent: int = 8, review: int = 5) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": _runs(conn),
        "outcomes": _outcomes(conn),
        "review": {"open_count": review_service.open_count(conn),
                   "oldest_open": review_service.list_items(conn, "open", review, settings)},
        "spend": _spend(conn, settings),
        "pos": _pos(conn),
        "recent_runs": views.recent_runs(conn, recent),
    }

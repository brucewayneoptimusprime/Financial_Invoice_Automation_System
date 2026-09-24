"""The ONLY module in the engine that touches SQLite: builds the per-run RunFacts snapshot.

Everything is read in a fixed order (ORDER BY id) so the snapshot is deterministic. PO balances are
derived from the ledger here (SUM in integer minor units, converted once via app.money).
"""
import json
import sqlite3
from datetime import date

from app.config import get_settings
from app.engine.facts import (
    POFact, POLineFact, PriorInvoiceFact, RunFacts, RuntimeSettings, VendorFact,
)
from app.models.rules import Rule
from app.money import from_minor


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _money(minor: int | None):
    return None if minor is None else from_minor(minor)


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    """All rules from the `rules` table (enabled or not; the engine decides what to run)."""
    return [
        Rule(
            id=r["id"], name=r["name"], type=r["type"], params=json.loads(r["params"]),
            severity_on_trigger=r["severity_on_trigger"], source=r["source"], enabled=bool(r["enabled"]),
            original_text=r["original_text"],
        )
        for r in conn.execute("SELECT * FROM rules ORDER BY id")
    ]


def load_facts(conn: sqlite3.Connection, current_run_id: str | None = None) -> RunFacts:
    """Snapshot vendors, POs (with ledger-derived consumption), prior invoices and runtime settings.

    Prior invoices belonging to `current_run_id` are excluded so a run never matches itself.
    """
    vendors = tuple(
        VendorFact(
            id=r["id"], name=r["name"], aliases=tuple(json.loads(r["aliases"])),
            tax_id=r["tax_id"], country=r["country"], status=r["status"],
        )
        for r in conn.execute("SELECT * FROM vendors ORDER BY id")
    )

    lines_by_po: dict[int, list[POLineFact]] = {}
    for r in conn.execute("SELECT * FROM po_lines ORDER BY po_id, line_no"):
        lines_by_po.setdefault(r["po_id"], []).append(
            POLineFact(
                line_no=r["line_no"], description=r["description"],
                quantity=r["quantity"], unit_price=r["unit_price"], amount=_money(r["amount"]),
            )
        )

    purchase_orders = tuple(
        POFact(
            id=r["id"], po_number=r["po_number"], vendor_id=r["vendor_id"], currency=r["currency"],
            total_amount=from_minor(r["total_amount"]), status=r["status"],
            net_committed=from_minor(r["net_committed"]), lines=tuple(lines_by_po.get(r["id"], ())),
        )
        for r in conn.execute(
            "SELECT po.*, COALESCE(SUM(l.amount), 0) AS net_committed "
            "FROM purchase_orders po LEFT JOIN ledger_entries l ON l.po_id = po.id "
            "GROUP BY po.id ORDER BY po.id"
        )
    )

    prior_invoices = tuple(
        PriorInvoiceFact(
            id=r["id"], run_id=r["run_id"], vendor_id=r["vendor_id"], invoice_number=r["invoice_number"],
            invoice_date=_date(r["invoice_date"]), currency=r["currency"], total=_money(r["total"]),
            po_id=r["po_id"], status=r["status"], file_hash=r["file_hash"],
        )
        for r in conn.execute(
            "SELECT * FROM invoices WHERE (? IS NULL OR run_id IS NULL OR run_id <> ?) ORDER BY id",
            (current_run_id, current_run_id),
        )
    )

    row = conn.execute("SELECT value FROM settings WHERE key = 'confidence_threshold'").fetchone()
    threshold = json.loads(row["value"]) if row else get_settings().confidence_threshold

    return RunFacts(
        vendors=vendors, purchase_orders=purchase_orders, prior_invoices=prior_invoices,
        settings=RuntimeSettings(confidence_threshold=threshold),
    )

"""The ONLY module in the engine that touches SQLite: builds the per-run RunFacts snapshot.

Everything is read in a fixed order (ORDER BY id) so the snapshot is deterministic. PO balances are
derived from the ledger here (SUM in integer minor units, converted once via app.money).
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

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

    # Line-ASSIGNED consumption (schema v2). Quantities are decimal TEXT, so they are summed here, never in SQL. Consumption against
    # the PO total (po_line_id NULL, e.g. every pre-v2 commit) reduces the PO balance but no line.
    consumed_qty: dict[int, Decimal] = {}
    consumed_amt: dict[int, int] = {}
    for r in conn.execute("SELECT po_line_id, quantity, amount FROM po_consumption WHERE po_line_id IS NOT NULL"):
        consumed_amt[r["po_line_id"]] = consumed_amt.get(r["po_line_id"], 0) + r["amount"]
        if r["quantity"] is not None:
            qty = Decimal(r["quantity"])
            consumed_qty[r["po_line_id"]] = consumed_qty.get(r["po_line_id"], Decimal(0)) + (qty if r["amount"] > 0 else -abs(qty))

    lines_by_po: dict[int, list[POLineFact]] = {}
    for r in conn.execute("SELECT * FROM po_lines ORDER BY po_id, line_no"):
        lines_by_po.setdefault(r["po_id"], []).append(
            POLineFact(
                line_no=r["line_no"], description=r["description"],
                quantity=r["quantity"], unit_price=r["unit_price"], amount=_money(r["amount"]),
                id=r["id"], consumed_quantity=consumed_qty.get(r["id"], Decimal(0)),
                consumed_amount=from_minor(consumed_amt.get(r["id"], 0)),
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


# ------------------------------------------------------------------------------------------ effective settings (SPEC section 11 item 91)

@dataclass(frozen=True)
class Effective:
    """The rules and runtime settings a run is judged under, and the record of them (written once per run as `settings_applied`)."""
    rules: list[Rule]
    runtime: RuntimeSettings
    record: dict


def load_effective(conn: sqlite3.Connection, po_id: int | None) -> Effective:
    """Global defaults, then (when the invoice was confidently matched to `po_id`) that PO's overrides: the most specific wins.

    With no override the rules and threshold are EXACTLY what `load_rules` / `load_facts` return today: parameters are only replaced
    for keys the PO actually overrides. Locked rules stay on whatever is stored (the engine runs them regardless)."""
    from app.rulesettings import catalog

    rules = load_rules(conn)
    globals_ = catalog.global_values(conn)
    overrides = catalog.po_override_values(conn, po_id) if po_id is not None else {}
    switches = catalog.po_override_switches(conn, po_id) if po_id is not None else {}
    values = {**globals_, **overrides}
    sources = {k: ("override" if k in overrides else "default") for k in globals_}

    effective_rules: list[Rule] = []
    for rule in rules:
        params, enabled = dict(rule.params), rule.enabled
        for d in catalog.DEFS:
            if d.rule_id == rule.id and d.key in overrides:
                params[d.param] = float(overrides[d.key]) if d.kind in ("money", "percent") else overrides[d.key]
        if rule.id in switches and not catalog.is_locked(rule.id):
            enabled = switches[rule.id]
        effective_rules.append(rule if (params == rule.params and enabled == rule.enabled)
                               else rule.model_copy(update={"params": params, "enabled": enabled}))

    po_number = None
    if po_id is not None:
        row = conn.execute("SELECT po_number FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        po_number = row["po_number"] if row else None
    rule_switches = {r.id: r.enabled for r in effective_rules}
    record = {"scope": "po" if po_id is not None else "global", "po_id": po_id, "po_number": po_number, "values": values,
              "sources": sources, "rules_enabled": rule_switches,
              "rule_sources": {r: ("override" if r in switches and not catalog.is_locked(r) else "default") for r in rule_switches}}
    return Effective(rules=effective_rules, runtime=RuntimeSettings(confidence_threshold=float(values["confidence_threshold"])),
                     record=record)

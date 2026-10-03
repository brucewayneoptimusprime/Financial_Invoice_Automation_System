"""Shared setup for the rules-settings tests."""
import json
import sqlite3

from app.money import to_minor
from decimal import Decimal


def set_po(conn: sqlite3.Connection, po_id: int, **cols) -> None:
    """Write a po_settings row directly (the API arrives in S2). Money keys are given in major units."""
    for k in ("tolerance_abs", "duplicate_amount"):
        if k in cols:
            cols[f"{k}_minor"] = to_minor(Decimal(str(cols.pop(k))))
    if not cols:
        return
    names = ", ".join(cols)
    with conn:
        conn.execute(f"INSERT INTO po_settings (po_id, {names}) VALUES (?, {', '.join('?' * len(cols))}) "
                     f"ON CONFLICT (po_id) DO UPDATE SET " + ", ".join(f"{k} = excluded.{k}" for k in cols), (po_id, *cols.values()))


def switch_po(conn: sqlite3.Connection, po_id: int, rule_id: str, enabled: bool) -> None:
    with conn:
        conn.execute("INSERT INTO po_rule_switches (po_id, rule_id, enabled) VALUES (?, ?, ?) "
                     "ON CONFLICT (po_id, rule_id) DO UPDATE SET enabled = excluded.enabled", (po_id, rule_id, int(enabled)))


def set_global_param(conn: sqlite3.Connection, rule_id: str, **params) -> None:
    row = conn.execute("SELECT params FROM rules WHERE id = ?", (rule_id,)).fetchone()
    p = {**json.loads(row["params"]), **params}
    with conn:
        conn.execute("UPDATE rules SET params = ? WHERE id = ?", (json.dumps(p), rule_id))


def po_id(conn: sqlite3.Connection, number: str) -> int:
    return conn.execute("SELECT id FROM purchase_orders WHERE po_number = ?", (number,)).fetchone()[0]


def events(conn: sqlite3.Connection, run_id: str, event_type: str) -> list[dict]:
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in conn.execute(
        "SELECT * FROM audit_events WHERE run_id = ? AND event_type = ? ORDER BY seq", (run_id, event_type))]

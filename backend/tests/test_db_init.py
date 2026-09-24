import json
import sqlite3

import pytest

from app.config import get_settings
from app.db.connection import connect
from app.db.init_db import SCHEMA_VERSION, init_db

EXPECTED_TABLES = {
    "vendors", "purchase_orders", "po_lines", "invoices", "invoice_lines", "ledger_entries",
    "runs", "audit_events", "rules", "review_queue", "drafts", "settings",
}


def _tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_init_creates_every_section5_table(conn):
    assert _tables(conn) == EXPECTED_TABLES


def test_init_sets_schema_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_init_is_idempotent(conn):
    before = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    init_db(conn)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == before
    assert _tables(conn) == EXPECTED_TABLES


def test_init_rejects_unknown_schema_version(db_path):
    c = connect(db_path)
    c.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError):
        init_db(c)
    c.close()


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status) "
                     "VALUES ('X', 999, 'USD', 100, 'open')")


def test_builtin_rules_and_settings_are_seeded_from_config(conn):
    s = get_settings()
    rules = {r["id"]: r for r in conn.execute("SELECT * FROM rules")}
    assert all(r["source"] == "builtin" for r in rules.values())
    assert all(r["severity_on_trigger"] >= 1 for r in rules.values())
    tol = json.loads(rules["r_tolerance_pct"]["params"])
    assert tol == {"pct": s.tolerance_pct, "abs": s.tolerance_abs}  # tolerance lives in rule params

    settings = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT * FROM settings")}
    assert settings["confidence_threshold"] == s.confidence_threshold
    assert "model_override" in settings
    assert not any("toler" in k for k in settings)  # settings holds non-rule values only


def test_rule_severity_zero_rejected_by_db(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rules (id, name, type, severity_on_trigger, source) VALUES ('r', 'n', 't', 0, 'user')")


def test_nl_rule_requires_original_text_in_db(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rules (id, name, type, severity_on_trigger, source) VALUES ('r', 'n', 't', 1, 'nl')")


@pytest.mark.parametrize("table,column,bad", [
    ("vendors", "status", "pending"),
    ("purchase_orders", "status", "paid"),
    ("invoices", "status", "approve"),
    ("invoices", "decision", "maybe"),
    ("runs", "status", "done"),
])
def test_enum_check_constraints(conn, table, column, bad):
    conn.execute("INSERT INTO vendors (id, name, status) VALUES (1, 'v', 'approved')")
    rows = {
        "vendors": "INSERT INTO vendors (name, status) VALUES ('v2', ?)",
        "purchase_orders": "INSERT INTO purchase_orders (po_number, vendor_id, currency, total_amount, status) VALUES ('P', 1, 'USD', 1, ?)",
        "invoices": f"INSERT INTO invoices ({column}) VALUES (?)",
        "runs": "INSERT INTO runs (id, source_file, status) VALUES ('r', 'f', ?)",
    }
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(rows[table], (bad,))

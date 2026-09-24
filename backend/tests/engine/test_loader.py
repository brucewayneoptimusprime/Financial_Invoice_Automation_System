"""The loader is the only DB-touching engine module. Rows are built by hand (not the placeholder seed)."""
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.engine.loader import load_facts
from app.money import to_minor


@pytest.fixture
def db(conn):
    with conn:
        conn.execute("INSERT INTO vendors (id, name, aliases, status) VALUES (1, 'Vendor A', ?, 'approved')", (json.dumps(["VA"]),))
        conn.execute("INSERT INTO vendors (id, name, status) VALUES (2, 'Vendor B', 'blocked')")
        conn.execute("INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) "
                     "VALUES (1, 'PO-X-1', 1, 'USD', ?, 'partially_billed')", (to_minor("1000.00"),))
        conn.execute("INSERT INTO purchase_orders (id, po_number, vendor_id, currency, total_amount, status) "
                     "VALUES (2, 'PO-X-2', 2, 'EUR', ?, 'open')", (to_minor("50.50"),))
        conn.execute("INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) "
                     "VALUES (1, 2, 'second', '2.5', '10.00', ?)", (to_minor("25.00"),))
        conn.execute("INSERT INTO po_lines (po_id, line_no, description, quantity, unit_price, amount) "
                     "VALUES (1, 1, 'first', '1', '5.00', ?)", (to_minor("5.00"),))
        conn.execute("INSERT INTO runs (id, source_file, status) VALUES ('run-cur', 'a.pdf', 'running')")
        conn.execute("INSERT INTO runs (id, source_file, status) VALUES ('run-old', 'b.pdf', 'completed')")
        conn.execute("INSERT INTO invoices (id, run_id, vendor_id, invoice_number, invoice_date, currency, total, po_id, "
                     "decision, status, file_hash) VALUES (10, NULL, 1, 'H-1', '2026-01-05', 'USD', ?, 1, 'approve', 'approved', 'h1')",
                     (to_minor("300.00"),))
        conn.execute("INSERT INTO invoices (id, run_id, vendor_id, invoice_number, status) VALUES (11, 'run-old', 1, 'H-2', 'rejected')")
        conn.execute("INSERT INTO invoices (id, run_id, vendor_id, invoice_number, status) VALUES (12, 'run-cur', 1, 'CUR', 'pending')")
        conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, 10, ?, 'commit')", (to_minor("300.00"),))
        conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, 11, ?, 'commit')", (to_minor("100.10"),))
        conn.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (1, 11, ?, 'reversal')", (to_minor("-100.10"),))
    return conn


def test_vendors_and_aliases(db):
    facts = load_facts(db)
    assert [v.id for v in facts.vendors] == [1, 2]
    assert facts.vendors[0].aliases == ("VA",) and facts.vendors[1].status.value == "blocked"


def test_po_balance_is_derived_from_the_ledger(db):
    facts = load_facts(db)
    po1, po2 = facts.purchase_orders
    assert po1.total_amount == Decimal("1000.00")
    assert po1.net_committed == Decimal("300.00")           # commit 300 + commit 100.10 - reversal 100.10
    assert po1.balance == Decimal("700.00")
    assert po2.balance == Decimal("50.50")                  # nothing committed
    db.execute("INSERT INTO ledger_entries (po_id, invoice_id, amount, type) VALUES (2, 10, ?, 'commit')", (to_minor("0.50"),))
    assert load_facts(db).purchase_orders[1].balance == Decimal("50.00")   # a fresh snapshot sees the change


def test_po_lines_are_ordered_and_typed(db):
    lines = load_facts(db).purchase_orders[0].lines
    assert [ln.line_no for ln in lines] == [1, 2]
    assert lines[1].quantity == Decimal("2.5") and lines[1].amount == Decimal("25.00")


def test_current_run_is_excluded_from_prior_invoices(db):
    priors = load_facts(db, current_run_id="run-cur").prior_invoices
    assert [p.id for p in priors] == [10, 11]
    assert [p.id for p in load_facts(db, current_run_id=None).prior_invoices] == [10, 11, 12]
    assert priors[0].invoice_date == date(2026, 1, 5) and priors[0].total == Decimal("300.00")
    assert priors[1].status.value == "rejected"


def test_settings_come_from_the_settings_table(db):
    assert load_facts(db).settings.confidence_threshold == 0.8
    with db:
        db.execute("UPDATE settings SET value = '0.55' WHERE key = 'confidence_threshold'")
    assert load_facts(db).settings.confidence_threshold == 0.55


def test_snapshot_is_deterministic_and_immutable(db):
    assert load_facts(db, "run-cur") == load_facts(db, "run-cur")
    facts = load_facts(db)
    with pytest.raises(ValidationError):
        facts.vendors[0].status = "blocked"
    with pytest.raises(ValidationError):
        facts.settings.confidence_threshold = 0.0
    with pytest.raises((TypeError, AttributeError)):
        facts.vendors[0].aliases.append("x")  # tuples, not lists
    with pytest.raises((TypeError, AttributeError)):
        facts.purchase_orders.append(None)
